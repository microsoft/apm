"""Exercise real downloader validation, replacing transport only."""

import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

from apm_cli.contracts.models import ContractError
from apm_cli.contracts.workspace import local_git
from apm_cli.deps.artifactory_orchestrator import ArtifactoryOrchestrator
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.transport_selection import ProtocolPreference
from apm_cli.install.contract_source import _download
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference
from apm_cli.models.validation import validate_apm_package

pytestmark = pytest.mark.component


@pytest.fixture
def source_package(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "apm.yml").write_text("name: contract-source\nversion: 1.0.0\n", encoding="ascii")
    (source / "job.contract.md").write_text(
        "---\nproduces: result.txt\nverify: {check: 'true'}\n---\nWrite a result.\n",
        encoding="ascii",
    )
    return source


def test_source_profile_does_not_relax_default_install_validation(source_package: Path) -> None:
    ordinary = validate_apm_package(source_package)
    selected = validate_apm_package(source_package, contract_path="job.contract.md")
    assert not ordinary.is_valid
    assert ordinary.legacy_metadata_only
    assert selected.is_valid
    assert selected.package.name == "contract-source"
    assert not (source_package / ".apm").exists()


@pytest.mark.parametrize(
    "selected", ["missing.contract.md", "../job.contract.md", "/job.contract.md", "apm.yml"]
)
def test_source_profile_requires_exact_confined_contract(
    source_package: Path, selected: str
) -> None:
    result = validate_apm_package(source_package, contract_path=selected)
    assert not result.is_valid
    assert result.package is None


def test_source_profile_rejects_malformed_manifest(source_package: Path) -> None:
    (source_package / "apm.yml").write_text("[invalid\n")
    result = validate_apm_package(source_package, contract_path="job.contract.md")
    assert not result.is_valid
    assert "manifest" in result.errors[0]


@pytest.mark.parametrize("contract_path", [None, "job.contract.md"])
@pytest.mark.parametrize("subdirectory", [False, True])
def test_real_downloader_retains_strict_or_source_validation(
    source_package: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contract_path: str | None,
    subdirectory: bool,
) -> None:
    auth = Mock()
    auth._token_manager.setup_environment.return_value = {}
    downloader = GitHubPackageDownloader(
        auth_resolver=auth,
        protocol_pref=ProtocolPreference.from_str(None),
        allow_fallback=False,
        contract_path=contract_path,
    )
    reference = DependencyReference.parse(
        "owner/package/examples/job#main" if subdirectory else "owner/package#main"
    )
    resolved = ResolvedReference(
        original_ref="main",
        ref_name="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="a" * 40,
    )
    monkeypatch.setattr(downloader, "resolve_git_reference", lambda _: resolved)
    monkeypatch.setattr(downloader, "_parse_artifactory_base_url", lambda: None)
    monkeypatch.setattr(downloader, "_is_artifactory_only", lambda: False)

    def clone(repo: str, target: Path, **kwargs: object) -> None:
        shutil.copytree(source_package, target, dirs_exist_ok=True)

    observed_commits = ["a" * 40]

    def sparse(selected: DependencyReference, checkout: Path, path: str, ref: str | None) -> bool:
        shutil.copytree(source_package, checkout / path)
        local_git(checkout, "init", "--quiet")
        local_git(checkout, "add", ".")
        local_git(checkout, "commit", "--quiet", "-m", "Contract source")
        observed_commits[0] = local_git(checkout, "rev-parse", "HEAD").decode().strip()
        return True

    transport = tmp_path / "transport"
    transport.mkdir()
    monkeypatch.setattr("apm_cli.config.get_apm_temp_dir", lambda: str(transport))
    monkeypatch.setattr(downloader, "_clone_with_fallback", clone)
    monkeypatch.setattr(downloader, "_try_sparse_checkout", sparse)
    target = tmp_path / "downloaded"
    if contract_path is None:
        with pytest.raises(RuntimeError, match="missing the required"):
            downloader.download_package(reference, target)
        assert not target.exists()
    else:
        result = downloader.download_package(reference, target)
        assert result.package.name == "contract-source"
        assert result.resolved_reference.resolved_commit == observed_commits[0]
        assert (target / "job.contract.md").read_bytes() == (
            source_package / "job.contract.md"
        ).read_bytes()
    assert list(transport.iterdir()) == []


def test_proxy_validation_receives_same_source_profile(source_package: Path) -> None:
    ordinary = ArtifactoryOrchestrator(Mock())
    source = ArtifactoryOrchestrator(Mock(), contract_path="job.contract.md")
    assert not ordinary._validate_downloaded_package(source_package).is_valid
    assert source._validate_downloaded_package(source_package).is_valid


def test_acquisition_failure_preserves_reason_without_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "ghp_" + "a" * 30
    downloader = Mock()
    downloader.download_package.side_effect = RuntimeError(
        f"Missing apm.yml; https://user:password@example.com/repo?token={secret} "
        f"Authorization: Bearer {secret}\n\x1b[31m package invalid"
    )
    constructor = Mock(return_value=downloader)
    monkeypatch.setattr("apm_cli.deps.github_downloader.GitHubPackageDownloader", constructor)
    with pytest.raises(ContractError) as error:
        _download(
            DependencyReference.parse("owner/package"),
            tmp_path / "target",
            contract_path="job.contract.md",
        )
    message = str(error.value)
    assert "Missing apm.yml" in message
    assert secret not in message
    assert "user:password" not in message
    assert all(" " <= character <= "~" for character in message)
    assert constructor.call_args.kwargs["contract_path"] == "job.contract.md"
