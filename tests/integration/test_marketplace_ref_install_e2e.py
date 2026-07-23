"""Integration coverage for marketplace registered ``--ref`` install flow.

Issue #1918 tracks the gap left after #1880: unit tests covered
``resolve_marketplace_plugin`` ref propagation, but no integration-tier test
exercised the install pipeline through the downloader boundary.

These tests keep the run hermetic by stubbing only external I/O seams:

* marketplace registry/fetch returns in-memory fixtures;
* package accessibility probing is accepted without network;
* ``GitHubPackageDownloader.download_package`` writes a tiny package and
  records the dependency reference it received.

Everything between ``apm install plugin@marketplace`` and the downloader call
runs on production code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.marketplace import registry
from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)
from apm_cli.models.apm_package import (
    APMPackage,
    PackageInfo,
    clear_apm_yml_cache,
)
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

pytestmark = pytest.mark.integration

_MARKETPLACE_NAME = "acme-market"
_OWNER = "acme"
_REPO = "plugin-marketplace"
_PLUGIN_NAME = "reviewer"
_PLUGIN_PATH = "plugins/reviewer"
_REGISTERED_REF = "release/feature-ref"
_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"
_PATCH_VALIDATE_EXISTS = "apm_cli.commands.install._validate_package_exists"


@dataclass
class _DownloadCall:
    dep_ref: DependencyReference
    clone_url: str


class _DownloadRecorder:
    """Records install-pipeline downloader inputs without touching the network."""

    def __init__(self) -> None:
        self.calls: list[_DownloadCall] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = self

        def _fake_download(self, repo_ref, target_path, *args, **kwargs):
            dep_ref = (
                repo_ref
                if isinstance(repo_ref, DependencyReference)
                else DependencyReference.parse(str(repo_ref))
            )
            clone_url = self._build_repo_url(
                dep_ref.repo_url,
                dep_ref=dep_ref,
                token="",
            )
            recorder.calls.append(_DownloadCall(dep_ref=dep_ref, clone_url=clone_url))

            target = Path(target_path)
            target.mkdir(parents=True, exist_ok=True)
            (target / "apm.yml").write_text(
                yaml.safe_dump(
                    {
                        "name": _PLUGIN_NAME,
                        "version": "0.1.0",
                        "description": "marketplace ref propagation fixture",
                    }
                ),
                encoding="utf-8",
            )

            package = APMPackage.from_apm_yml(target / "apm.yml")
            ref = dep_ref.reference or "main"
            return PackageInfo(
                package=package,
                install_path=target,
                installed_at=datetime.now().isoformat(),
                dependency_ref=dep_ref,
                resolved_reference=ResolvedReference(
                    original_ref=ref,
                    ref_type=GitReferenceType.BRANCH,
                    resolved_commit="a" * 40,
                    ref_name=ref,
                ),
            )

        from apm_cli.deps import github_downloader as _ghd

        monkeypatch.setattr(_ghd.GitHubPackageDownloader, "download_package", _fake_download)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_dir = tmp_path / ".apm"
    config_dir.mkdir()
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", str(config_dir))
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(config_dir / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    monkeypatch.setattr(registry, "_registry_cache", None)
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _source(host: str) -> MarketplaceSource:
    return MarketplaceSource(
        name=_MARKETPLACE_NAME,
        owner=_OWNER,
        repo=_REPO,
        host=host,
        ref=_REGISTERED_REF,
    )


def _manifest() -> MarketplaceManifest:
    return MarketplaceManifest(
        name=_MARKETPLACE_NAME,
        plugins=(
            MarketplacePlugin(
                name=_PLUGIN_NAME,
                source=f"./{_PLUGIN_PATH}",
                version="0.1.0",
            ),
        ),
        plugin_root="",
    )


def _write_consumer_project(project: Path) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "consumer",
                "version": "1.0.0",
                "target": "copilot",
                "dependencies": {"apm": []},
            }
        ),
        encoding="utf-8",
    )
    (project / ".github").mkdir()
    (project / ".github" / "copilot-instructions.md").write_text("# Consumer\n", encoding="utf-8")


def _run_install(
    runner: CliRunner,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: MarketplaceSource,
    recorder: _DownloadRecorder,
):
    monkeypatch.chdir(project)
    recorder.install(monkeypatch)
    with (
        patch(_PATCH_UPDATES, return_value=None),
        patch(_PATCH_VALIDATE_EXISTS, return_value=True),
        patch("apm_cli.marketplace.resolver.get_marketplace_by_name", return_value=source),
        patch("apm_cli.marketplace.resolver.fetch_or_cache", return_value=_manifest()),
    ):
        return runner.invoke(
            cli,
            ["install", f"{_PLUGIN_NAME}@{_MARKETPLACE_NAME}"],
            catch_exceptions=False,
        )


def _read_apm_deps(project: Path) -> list:
    data = yaml.safe_load((project / "apm.yml").read_text(encoding="utf-8"))
    return data["dependencies"]["apm"]


def _read_locked_dep(project: Path) -> dict:
    data = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    deps = data.get("dependencies")
    entries = deps.values() if isinstance(deps, dict) else deps or []
    for entry in entries:
        if entry.get("virtual_path") == _PLUGIN_PATH:
            return entry
    raise AssertionError(f"locked dependency for {_PLUGIN_PATH!r} not found: {data}")


def _assert_clone_url(call: _DownloadCall, *, host: str) -> None:
    parsed = urlparse(call.clone_url)
    assert parsed.scheme == "https"
    assert parsed.hostname == host
    assert parsed.path == f"/{_OWNER}/{_REPO}"


def test_github_family_marketplace_registered_ref_reaches_downloader(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "github-consumer"
    _write_consumer_project(project)
    recorder = _DownloadRecorder()

    result = _run_install(runner, project, monkeypatch, _source("github.com"), recorder)

    assert result.exit_code == 0, result.output
    assert len(recorder.calls) == 1

    call = recorder.calls[0]
    assert call.dep_ref.host == "github.com"
    assert call.dep_ref.repo_url == f"{_OWNER}/{_REPO}"
    assert call.dep_ref.virtual_path == _PLUGIN_PATH
    assert call.dep_ref.reference == _REGISTERED_REF
    _assert_clone_url(call, host="github.com")

    deps = _read_apm_deps(project)
    assert deps == [f"{_OWNER}/{_REPO}/{_PLUGIN_PATH}#{_REGISTERED_REF}"]

    locked = _read_locked_dep(project)
    assert locked["repo_url"] == f"{_OWNER}/{_REPO}"
    assert locked["resolved_ref"] == _REGISTERED_REF
    assert locked["resolved_commit"] == "a" * 40


def test_gitlab_marketplace_registered_ref_reaches_structured_downloader(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "gitlab-consumer"
    _write_consumer_project(project)
    recorder = _DownloadRecorder()

    result = _run_install(runner, project, monkeypatch, _source("gitlab.com"), recorder)

    assert result.exit_code == 0, result.output
    assert len(recorder.calls) == 1

    call = recorder.calls[0]
    assert call.dep_ref.host == "gitlab.com"
    assert call.dep_ref.repo_url == f"{_OWNER}/{_REPO}"
    assert call.dep_ref.virtual_path == _PLUGIN_PATH
    assert call.dep_ref.reference == _REGISTERED_REF
    _assert_clone_url(call, host="gitlab.com")

    deps = _read_apm_deps(project)
    assert deps == [
        {
            "git": f"https://gitlab.com/{_OWNER}/{_REPO}",
            "path": _PLUGIN_PATH,
            "ref": _REGISTERED_REF,
        }
    ]

    locked = _read_locked_dep(project)
    assert locked["host"] == "gitlab.com"
    assert locked["repo_url"] == f"{_OWNER}/{_REPO}"
    assert locked["virtual_path"] == _PLUGIN_PATH
    assert locked["resolved_ref"] == _REGISTERED_REF
    assert locked["resolved_commit"] == "a" * 40
