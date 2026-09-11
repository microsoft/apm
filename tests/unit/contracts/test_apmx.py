"""Companion parsing, source roots and bounded one-shot preparation."""

import hashlib
import json
import shutil
import sys
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from apm_cli.apmx import main
from apm_cli.contracts import frontend, workspace
from apm_cli.contracts.models import ContractError, ContractLimits, ContractSource, Outcome
from apm_cli.contracts.records import AttemptStore
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install import contract_source
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference
from apm_cli.utils.content_hash import compute_package_hash

pytestmark = pytest.mark.component


@pytest.fixture
def caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from apm_cli import config

    root = tmp_path / "caller"
    root.mkdir()
    workspace.local_git(root, "init", "--quiet")
    (root / "notes.md").write_text("CALLER INPUT\n", encoding="ascii")
    monkeypatch.chdir(root)
    monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
    monkeypatch.delenv("APM_POLICY_DISABLE", raising=False)
    monkeypatch.setattr("apm_cli.contracts.frontend.sys.platform", "linux")
    monkeypatch.setattr("apm_cli.runtime.utils.find_runtime_binary", lambda _: sys.executable)
    monkeypatch.setattr(config, "_config_cache", {"experimental": {"contracts": True}})
    return root


def _package(root: Path, *, imports: bool = False) -> Path:
    root.mkdir()
    manifest = "name: packaged-job\nversion: 1.0.0\n"
    if imports:
        manifest += "dependencies:\n  apm:\n    - ../style\n"
    (root / "apm.yml").write_text(manifest, encoding="ascii")
    (root / "notes.md").write_text("PACKAGE INPUT MUST NOT WIN\n", encoding="ascii")
    (root / "checks").mkdir()
    (root / "checks" / "check.py").write_text(
        "from pathlib import Path\n"
        "assert Path('result.txt').read_bytes() == Path('notes.md').read_bytes()\n",
        encoding="ascii",
    )
    header = (
        "needs: notes.md\nproduces: result.txt\nverify:\n"
        f"  content: '{sys.executable} checks/check.py'\n"
    )
    if imports:
        header += "imports: [style]\n"
    (root / "job.contract.md").write_text(
        "---\n" + header + "---\nWrite result.txt from notes.md.\n", encoding="ascii"
    )
    return root


def _skill(root: Path) -> None:
    root.mkdir()
    (root / "apm.yml").write_text("name: style\nversion: 1.0.0\n", encoding="ascii")
    (root / "SKILL.md").write_text(
        "---\nname: style\ndescription: concise style\n---\nWrite concisely.\n",
        encoding="ascii",
    )


def _prepare(
    package: Path, caller: Path, *, planning: bool = False
) -> AbstractContextManager[ContractSource]:
    return contract_source.prepare_contract_source(
        str(package),
        "job.contract.md",
        caller_root=caller,
        planning=planning,
        limits=ContractLimits(),
    )


@pytest.mark.parametrize(
    "args,code",
    [
        (["--help"], 0),
        (["--version"], 0),
        ([], 2),
        (["job", "--on", "copilot"], 2),
        (["job.contract.md"], 2),
        (["job.contract.md", "extra", "--on", "copilot"], 2),
    ],
)
def test_cli_explicit_selection(args: list[str], code: int) -> None:
    result = CliRunner().invoke(main, args)
    assert result.exit_code == code, result.output
    assert not isinstance(result.exception, (ImportError, AttributeError))


@pytest.mark.parametrize("harness", ["codex", "unknown"])
@pytest.mark.parametrize("verbose", [False, True])
def test_cli_harness_admission_belongs_to_frontend(
    caller: Path, monkeypatch: pytest.MonkeyPatch, harness: str, verbose: bool
) -> None:
    (caller / "job.contract.md").write_text(
        "---\nproduces: result.txt\nverify: {content: 'true'}\n---\nWrite.\n",
        encoding="ascii",
    )
    resolve_binary = Mock(side_effect=AssertionError("Unsupported harness cannot launch"))
    monkeypatch.setattr("apm_cli.runtime.utils.find_runtime_binary", resolve_binary)
    arguments = ["job.contract.md", "--on", harness, "--plan"]
    if verbose:
        arguments.append("--verbose")
    result = CliRunner().invoke(main, arguments)
    assert result.exit_code == int(Outcome.UNPROVEN), result.output
    assert harness in result.output
    assert ("unsupported_harness" in result.output) is verbose
    assert (
        "does not support native contracts" if harness == "codex" else "Unknown runtime"
    ) in result.output
    resolve_binary.assert_not_called()
    assert not (caller / ".apm").exists()


def test_manifestless_local_plan_is_read_only(caller: Path) -> None:
    (caller / "job.contract.md").write_text(
        "---\nneeds: notes.md\nproduces: result.txt\nverify: {content: 'true'}\n---\nWrite.\n",
        encoding="ascii",
    )
    before = sorted(path.relative_to(caller).as_posix() for path in caller.rglob("*"))
    result = CliRunner().invoke(
        main, ["job.contract.md", "--on", "copilot", "--plan", "--model", "test-model"]
    )
    assert result.exit_code == 0, result.output
    assert before == sorted(path.relative_to(caller).as_posix() for path in caller.rglob("*"))
    assert not (caller / "apm.yml").exists()


def test_malformed_caller_manifest_blocks_package(caller: Path, tmp_path: Path) -> None:
    package = _package(tmp_path / "job")
    (caller / "apm.yml").write_text("[invalid\n", encoding="ascii")
    result = CliRunner().invoke(
        main, ["--from", str(package), "job.contract.md", "--on", "copilot", "--plan"]
    )
    assert result.exit_code == 22
    assert "manifest" in result.output.lower()
    assert not (caller / ".apm").exists()


def test_package_plan_maps_caller_and_resources(caller: Path, tmp_path: Path) -> None:
    package = _package(tmp_path / "job")
    before = compute_package_hash(package)
    with _prepare(package, caller, planning=True) as source:
        plan = frontend.plan_contract(
            Path("job.contract.md"), caller, harness="copilot", source=source
        )
        entries = {item.relative_path: item for item in workspace.inspect_workspace(plan)}
        assert plan.project_root == caller
        assert plan.evidence_root == caller / ".apm" / "runs"
        assert entries["notes.md"].sha256 == hashlib.sha256(b"CALLER INPUT\n").hexdigest()
        assert "_apmx_source/contract.contract.md" in entries
        assert "checks/check.py" in entries
        assert "apm.yml" not in entries
    assert compute_package_hash(package) == before
    assert not (caller / ".apm").exists()


@pytest.mark.parametrize(
    "collision", ["checks/extra.py", "Checks/extra.py", "_apmx_source/anything"]
)
def test_caller_source_collisions_refused(caller: Path, tmp_path: Path, collision: str) -> None:
    package = _package(tmp_path / "job")
    target = caller / collision
    target.parent.mkdir()
    target.write_text("caller", encoding="ascii")
    with _prepare(package, caller) as source:
        plan = frontend.plan_contract(
            Path("job.contract.md"), caller, harness="copilot", source=source
        )
        with pytest.raises(ContractError, match="collide"):
            workspace.inspect_workspace(plan)


@pytest.mark.parametrize(
    "path", ["../job.contract.md", "/job.contract.md", "checks/../job.contract.md"]
)
def test_package_source_escape_refused(caller: Path, tmp_path: Path, path: str) -> None:
    package = _package(tmp_path / "job")
    with pytest.raises(ContractError):
        with contract_source.prepare_contract_source(
            str(package), path, caller_root=caller, planning=True, limits=ContractLimits()
        ):
            pytest.fail("Escaping source was admitted")


def test_package_symlink_refused(caller: Path, tmp_path: Path) -> None:
    package = _package(tmp_path / "job")
    (package / "escape").symlink_to(caller / "notes.md")
    with pytest.raises(ContractError, match="symlink"):
        with _prepare(package, caller):
            pytest.fail("Symlink source was admitted")


def test_no_policy_gate_precedes_remote_acquisition(
    caller: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquire = Mock(side_effect=AssertionError("network acquisition"))
    monkeypatch.setattr("apm_cli.apmx.prepare_contract_source", acquire)
    monkeypatch.setenv("APM_POLICY_DISABLE", "1")
    result = CliRunner().invoke(
        main, ["--from", "org/job", "job.contract.md", "--on", "copilot", "--allow-host-access"]
    )
    assert result.exit_code == 21, result.output
    acquire.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        ["job.contract.md", "--on", "copilot", "--plan"],
        ["--from", "org/job", "job.contract.md", "--on", "copilot", "--plan"],
    ],
)
def test_experimental_gate_precedes_contract_admission(
    caller: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    from apm_cli import config

    acquire = Mock(side_effect=AssertionError("package acquisition"))
    monkeypatch.setattr("apm_cli.apmx.prepare_contract_source", acquire)
    monkeypatch.setattr(config, "_config_cache", {"experimental": {}})
    result = CliRunner().invoke(main, args)
    assert result.exit_code == int(Outcome.UNPROVEN), result.output
    assert "apm experimental enable contracts" in result.output
    acquire.assert_not_called()
    assert not (caller / ".apm").exists()


def test_remote_plan_never_initializes_downloader(
    caller: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    download = Mock(side_effect=AssertionError("offline network"))
    monkeypatch.setattr("apm_cli.deps.github_downloader.GitHubPackageDownloader", download)
    result = CliRunner().invoke(
        main, ["--from", "org/job#v1", "job.contract.md", "--on", "copilot", "--plan"]
    )
    assert result.exit_code == 21, result.output
    assert "unresolved" in result.output.lower()
    download.assert_not_called()
    assert not list(caller.glob(".apmx-source-*"))


def test_missing_direct_local_skill_materializes_private_copy(caller: Path, tmp_path: Path) -> None:
    package = _package(tmp_path / "job", imports=True)
    _skill(tmp_path / "style")
    original = compute_package_hash(package)
    with _prepare(package, caller) as source:
        assert source.root != package
        plan = frontend.plan_contract(
            Path("job.contract.md"), caller, harness="copilot", source=source
        )
        assert len(plan.imported_skills) == 1
        assert plan.imported_skills[0].name == "style"
        lock = LockFile.from_yaml((source.root / "apm.lock.yaml").read_text())
        assert len(lock.dependencies) == 1
        assert next(iter(lock.dependencies.values())).depth == 1
        store = AttemptStore.create(plan)
        record = json.loads(store.record_path.read_text())
        retained = record["source"]["retained"]
        assert Path(retained["apm.yml"]).read_bytes() == (package / "apm.yml").read_bytes()
        assert (
            Path(retained["apm.lock.yaml"]).read_bytes()
            == (source.root / "apm.lock.yaml").read_bytes()
        )
        prepared = source.root
    assert not prepared.exists()
    assert Path(retained["contract.contract.md"]).is_file()
    assert compute_package_hash(package) == original
    assert not (package / "apm_modules").exists()
    assert not (caller / "apm.yml").exists()


def test_missing_direct_skill_plan_is_unresolved(caller: Path, tmp_path: Path) -> None:
    package = _package(tmp_path / "job", imports=True)
    _skill(tmp_path / "style")
    with pytest.raises(ContractError) as error:
        with _prepare(package, caller, planning=True):
            pytest.fail("Missing import planned as resolved")
    assert error.value.outcome == Outcome.UNPROVEN
    assert not list(caller.glob(".apmx-source-*"))


@pytest.mark.parametrize("extra", ["companion.txt", "nested/SKILL.md"])
def test_skill_companions_rejected(caller: Path, tmp_path: Path, extra: str) -> None:
    package = _package(tmp_path / "job", imports=True)
    skill = tmp_path / "style"
    _skill(skill)
    path = skill / extra
    path.parent.mkdir(exist_ok=True)
    path.write_text("unsupported", encoding="ascii")
    with pytest.raises(ContractError, match="companion"):
        with _prepare(package, caller):
            pytest.fail("Companion imported")
    assert not list(caller.glob(".apmx-source-*"))
    assert not (package / "apm.lock.yaml").exists()


def test_package_drift_revalidated_before_capture(caller: Path, tmp_path: Path) -> None:
    package = _package(tmp_path / "job")
    with _prepare(package, caller) as source:
        plan = frontend.plan_contract(
            Path("job.contract.md"), caller, harness="copilot", source=source
        )
        (package / "checks" / "check.py").write_text("raise SystemExit(0)\n")
        with pytest.raises(ContractError, match="changed"):
            workspace.inspect_workspace(plan)


def test_evidence_root_cannot_be_rebound(caller: Path, tmp_path: Path) -> None:
    package = _package(tmp_path / "job")
    with _prepare(package, caller) as source:
        plan = frontend.plan_contract(
            Path("job.contract.md"), caller, harness="copilot", source=source
        )
        with pytest.raises(ContractError, match="caller-owned"):
            AttemptStore.create(replace(plan, evidence_root=package / ".apm" / "runs"))
    assert not (package / ".apm").exists()


def test_remote_acquisition_preserves_reference_object(
    caller: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _package(tmp_path / "download")
    downloader = Mock()

    def download(reference: DependencyReference, target: Path) -> Mock:
        assert reference.virtual_path == "jobs"
        assert reference.reference == "v1"
        shutil.copytree(package, target)
        return Mock(
            resolved_reference=ResolvedReference(
                original_ref="v1", ref_type=GitReferenceType.TAG, resolved_commit="a" * 40
            )
        )

    downloader.download_package.side_effect = download
    monkeypatch.setattr(
        "apm_cli.deps.github_downloader.GitHubPackageDownloader", lambda **kwargs: downloader
    )
    with contract_source.prepare_contract_source(
        "org/repo/jobs#v1",
        "job.contract.md",
        caller_root=caller,
        planning=False,
        limits=ContractLimits(),
    ) as source:
        assert source.resolved_commit == "a" * 40
        assert source.package_hash == compute_package_hash(source.root)
    assert not source.root.exists()
    assert downloader.download_package.call_count == 1


@pytest.mark.parametrize("declaration", ["../style", "{git: parent, path: jobs/style}"])
def test_remote_relative_import_uses_parent_revision(
    caller: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declaration: str
) -> None:
    package = _package(tmp_path / "download", imports=True)
    manifest = (package / "apm.yml").read_text().replace("../style", declaration)
    (package / "apm.yml").write_text(manifest)
    skill = tmp_path / "style"
    _skill(skill)
    calls = []
    downloader = Mock()

    def download(reference: DependencyReference, target: Path) -> Mock:
        calls.append(reference)
        shutil.copytree(package if len(calls) == 1 else skill, target)
        return Mock(
            resolved_reference=ResolvedReference(
                original_ref="v1", ref_type=GitReferenceType.TAG, resolved_commit="a" * 40
            )
        )

    downloader.download_package.side_effect = download
    monkeypatch.setattr(
        "apm_cli.deps.github_downloader.GitHubPackageDownloader", lambda **kwargs: downloader
    )
    with contract_source.prepare_contract_source(
        "org/repo/jobs/job#v1",
        "job.contract.md",
        caller_root=caller,
        planning=False,
        limits=ContractLimits(),
    ) as source:
        plan = frontend.plan_contract(
            Path("job.contract.md"), caller, harness="copilot", source=source
        )
        assert len(plan.imported_skills) == 1
        assert calls[1].repo_url == "org/repo"
        assert calls[1].virtual_path == "jobs/style"
        assert calls[1].reference == "a" * 40
        assert source.original_manifest == manifest.encode()
        assert source.package_hash == compute_package_hash(package)
        assert source.prepared_hash == compute_package_hash(source.root)
    assert len(calls) == 2


@pytest.mark.parametrize("declaration", ["/host/style", "../../../outside"])
def test_remote_relative_escape_never_reads_local_skill(
    caller: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declaration: str
) -> None:
    package = _package(tmp_path / "download", imports=True)
    (package / "apm.yml").write_text(
        (package / "apm.yml").read_text().replace("../style", declaration)
    )
    downloader = Mock()

    def download(reference: DependencyReference, target: Path) -> Mock:
        shutil.copytree(package, target)
        return Mock(
            resolved_reference=ResolvedReference(
                original_ref="v1", ref_type=GitReferenceType.TAG, resolved_commit="a" * 40
            )
        )

    downloader.download_package.side_effect = download
    monkeypatch.setattr(
        "apm_cli.deps.github_downloader.GitHubPackageDownloader", lambda **kwargs: downloader
    )
    with pytest.raises(ContractError):
        with contract_source.prepare_contract_source(
            "org/repo/jobs/job#v1",
            "job.contract.md",
            caller_root=caller,
            planning=False,
            limits=ContractLimits(),
        ):
            pytest.fail("Remote path escaped")
    assert downloader.download_package.call_count == 1
    assert not list(caller.glob(".apmx-source-*"))


@pytest.fixture
def locked_remote_source(caller: Path, tmp_path: Path) -> tuple[Path, Path, LockFile]:
    package = _package(tmp_path / "download")
    dependency = DependencyReference.parse("org/repo#v1")
    installed = dependency.get_install_path(caller / "apm_modules")
    shutil.copytree(package, installed)
    (caller / "apm.yml").write_text(
        "name: caller\nversion: 1.0.0\ndependencies:\n  apm: [org/repo#v1]\n"
    )
    lock = LockFile()
    locked = LockedDependency.from_dependency_ref(dependency, "a" * 40, depth=1, resolved_by=None)
    locked.content_hash = compute_package_hash(installed)
    lock.add_dependency(locked)
    lock.write(caller / "apm.lock.yaml")
    return package, installed, lock


def test_installed_remote_plan_is_exact_and_read_only(
    caller: Path,
    locked_remote_source: tuple[Path, Path, LockFile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, installed, _ = locked_remote_source
    downloader = Mock(side_effect=AssertionError("offline acquisition"))
    monkeypatch.setattr("apm_cli.deps.github_downloader.GitHubPackageDownloader", downloader)
    with contract_source.prepare_contract_source(
        "org/repo#v1", "job.contract.md", caller_root=caller, planning=True, limits=ContractLimits()
    ) as source:
        assert source.root == installed
        assert source.assurance == "locked-package-hash"
    with pytest.raises(ContractError):
        with contract_source.prepare_contract_source(
            "org/repo#v2",
            "job.contract.md",
            caller_root=caller,
            planning=True,
            limits=ContractLimits(),
        ):
            pytest.fail("Different requested revision admitted")
    downloader.assert_not_called()


@pytest.mark.parametrize("installed_present", [True, False])
def test_remote_execution_preserves_planned_pin_after_tag_moves(
    caller: Path,
    locked_remote_source: tuple[Path, Path, LockFile],
    monkeypatch: pytest.MonkeyPatch,
    installed_present: bool,
) -> None:
    package, installed, _ = locked_remote_source
    with contract_source.prepare_contract_source(
        "org/repo#v1", "job.contract.md", caller_root=caller, planning=True, limits=ContractLimits()
    ) as planned:
        assert planned.resolved_commit == "a" * 40
    if not installed_present:
        shutil.rmtree(installed)
    before = compute_package_hash(caller)
    downloader = Mock()

    def download(reference: DependencyReference, target: Path) -> Mock:
        revision = "a" * 40 if reference.reference == "a" * 40 else "b" * 40
        shutil.copytree(package, target)
        if revision == "b" * 40:
            (target / "notes.md").write_text("MOVED TAG CONTENT\n", encoding="ascii")
        return Mock(resolved_reference=ResolvedReference("v1", GitReferenceType.TAG, revision))

    downloader.download_package.side_effect = download
    monkeypatch.setattr(
        "apm_cli.deps.github_downloader.GitHubPackageDownloader", lambda **kwargs: downloader
    )
    with contract_source.prepare_contract_source(
        "org/repo#v1",
        "job.contract.md",
        caller_root=caller,
        planning=False,
        limits=ContractLimits(),
    ) as executed:
        assert executed.resolved_commit == planned.resolved_commit
        assert executed.package_hash == planned.package_hash
        assert executed.assurance == planned.assurance == "locked-package-hash"
        if installed_present:
            assert executed.root == planned.root == installed
            downloader.download_package.assert_not_called()
        else:
            assert downloader.download_package.call_count == 1
            assert downloader.download_package.call_args.args[0].reference == "a" * 40
    assert compute_package_hash(caller) == before
    assert not list(caller.glob(".apmx-source-*"))


@pytest.mark.parametrize("tamper", ["hash", "revision"])
def test_remote_locked_replay_rejects_transport_drift(
    caller: Path,
    locked_remote_source: tuple[Path, Path, LockFile],
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    package, installed, _ = locked_remote_source
    shutil.rmtree(installed)
    before = compute_package_hash(caller)
    downloader = Mock()

    def download(reference: DependencyReference, target: Path) -> Mock:
        assert reference.reference == "a" * 40
        shutil.copytree(package, target)
        if tamper == "hash":
            (target / "notes.md").write_text("DIFFERENT BYTES\n", encoding="ascii")
        revision = ("b" if tamper == "revision" else "a") * 40
        return Mock(resolved_reference=ResolvedReference("v1", GitReferenceType.TAG, revision))

    downloader.download_package.side_effect = download
    monkeypatch.setattr(
        "apm_cli.deps.github_downloader.GitHubPackageDownloader", lambda **kwargs: downloader
    )
    with pytest.raises(ContractError) as error:
        with contract_source.prepare_contract_source(
            "org/repo#v1",
            "job.contract.md",
            caller_root=caller,
            planning=False,
            limits=ContractLimits(),
        ):
            pytest.fail("Caller lock integrity was ignored")
    assert error.value.code == "source_changed"
    assert downloader.download_package.call_count == 1
    assert compute_package_hash(caller) == before
    assert not list(caller.glob(".apmx-source-*"))


def test_reused_caller_pin_still_prepares_missing_skill_privately(
    caller: Path,
    tmp_path: Path,
    locked_remote_source: tuple[Path, Path, LockFile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, installed, lock = locked_remote_source
    manifest = installed / "apm.yml"
    manifest.write_text(
        manifest.read_text() + "dependencies:\n  apm: [org/style#v1]\n", encoding="ascii"
    )
    contract = installed / "job.contract.md"
    contract.write_text(
        contract.read_text().replace("needs:", "imports: [style]\nneeds:"), encoding="ascii"
    )
    locked = lock.get_dependency("org/repo")
    locked.content_hash = compute_package_hash(installed)
    lock.write(caller / "apm.lock.yaml")
    skill = tmp_path / "style"
    _skill(skill)
    downloader = Mock()

    def download(reference: DependencyReference, target: Path) -> Mock:
        assert reference.repo_url == "org/style"
        shutil.copytree(skill, target)
        return Mock(resolved_reference=ResolvedReference("v1", GitReferenceType.TAG, "c" * 40))

    downloader.download_package.side_effect = download
    monkeypatch.setattr(
        "apm_cli.deps.github_downloader.GitHubPackageDownloader", lambda **kwargs: downloader
    )
    before = compute_package_hash(caller)
    with contract_source.prepare_contract_source(
        "org/repo#v1",
        "job.contract.md",
        caller_root=caller,
        planning=False,
        limits=ContractLimits(),
    ) as source:
        prepared = source.root
        assert source.original_root == installed
        assert prepared != installed
        assert source.resolved_commit == locked.resolved_commit
        assert source.package_hash == locked.content_hash
        skill_lock = LockFile.read(prepared / "apm.lock.yaml")
        assert skill_lock.get_dependency("org/style").resolved_commit == "c" * 40
    assert downloader.download_package.call_count == 1
    assert not prepared.exists()
    assert compute_package_hash(caller) == before


@pytest.mark.parametrize("planning", [True, False])
@pytest.mark.parametrize(
    "tamper", ["installed_hash", "declared_ref", "requested_ref", "lock_commit", "lock_missing"]
)
def test_remote_caller_pin_drift_never_falls_back_to_mutable_reference(
    caller: Path,
    locked_remote_source: tuple[Path, Path, LockFile],
    monkeypatch: pytest.MonkeyPatch,
    planning: bool,
    tamper: str,
) -> None:
    _, installed, lock = locked_remote_source
    requested = "org/repo#v1"
    if tamper == "installed_hash":
        (installed / "notes.md").write_text("TAMPERED\n", encoding="ascii")
    elif tamper == "declared_ref":
        manifest = caller / "apm.yml"
        manifest.write_text(manifest.read_text().replace("#v1", "#v2"), encoding="ascii")
    elif tamper == "requested_ref":
        requested = "org/repo#v2"
    elif tamper == "lock_commit":
        lock.get_dependency("org/repo").resolved_commit = "unknown"
        lock.write(caller / "apm.lock.yaml")
    else:
        LockFile().write(caller / "apm.lock.yaml")
    downloader = Mock(side_effect=AssertionError("Drift cannot trigger fresh acquisition"))
    monkeypatch.setattr("apm_cli.deps.github_downloader.GitHubPackageDownloader", downloader)
    before = compute_package_hash(caller)
    with pytest.raises(ContractError):
        with contract_source.prepare_contract_source(
            requested,
            "job.contract.md",
            caller_root=caller,
            planning=planning,
            limits=ContractLimits(),
        ):
            pytest.fail("Caller source drift admitted")
    downloader.assert_not_called()
    assert compute_package_hash(caller) == before
    assert not list(caller.glob(".apmx-source-*"))


@pytest.mark.parametrize("extra", ["scripts: {start: echo nope}", "dependencies: {mcp: [server]}"])
def test_unsupported_package_activation_refuses(caller: Path, tmp_path: Path, extra: str) -> None:
    package = _package(tmp_path / "job")
    with (package / "apm.yml").open("a") as stream:
        stream.write(extra + "\n")
    with pytest.raises(ContractError):
        with _prepare(package, caller):
            pytest.fail("Activation package accepted")
    assert not (caller / ".apm").exists()


def test_missing_package_manifest_refuses(caller: Path, tmp_path: Path) -> None:
    package = _package(tmp_path / "job")
    (package / "apm.yml").unlink()
    with pytest.raises(ContractError, match=r"apm\.yml"):
        with _prepare(package, caller):
            pytest.fail("Manifestless package accepted")


def test_local_import_symlink_refuses(caller: Path, tmp_path: Path) -> None:
    package = _package(tmp_path / "job", imports=True)
    _skill(tmp_path / "real-style")
    (tmp_path / "style").symlink_to(tmp_path / "real-style", target_is_directory=True)
    with pytest.raises(ContractError, match="symlink"):
        with _prepare(package, caller):
            pytest.fail("Symlink import accepted")
    assert not (package / "apm_modules").exists()


def test_transitive_lock_is_not_repaired_by_missing_import(caller: Path, tmp_path: Path) -> None:
    package = _package(tmp_path / "job", imports=True)
    _skill(tmp_path / "style")
    dependency = DependencyReference.parse("../style")
    lock = LockFile()
    lock.add_dependency(
        LockedDependency.from_dependency_ref(dependency, None, depth=2, resolved_by="parent/repo")
    )
    lock.write(package / "apm.lock.yaml")
    original = compute_package_hash(package)
    with pytest.raises(ContractError, match="exact direct"):
        with _prepare(package, caller):
            pytest.fail("Transitive lock rewritten as direct")
    assert compute_package_hash(package) == original


def test_missing_git_skill_uses_locked_revision_and_hash(
    caller: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _package(tmp_path / "job", imports=True)
    (package / "apm.yml").write_text(
        (package / "apm.yml").read_text().replace("../style", "org/style#v1")
    )
    skill = tmp_path / "style"
    _skill(skill)
    dependency = DependencyReference.parse("org/style#v1")
    locked = LockedDependency.from_dependency_ref(dependency, "b" * 40, depth=1, resolved_by=None)
    locked.content_hash = compute_package_hash(skill)
    lock = LockFile()
    lock.add_dependency(locked)
    lock.write(package / "apm.lock.yaml")
    downloader = Mock()

    def download(reference: DependencyReference, target: Path) -> Mock:
        assert reference.reference == "b" * 40
        shutil.copytree(skill, target)
        return Mock(
            resolved_reference=ResolvedReference(
                original_ref="v1", ref_type=GitReferenceType.TAG, resolved_commit="b" * 40
            )
        )

    downloader.download_package.side_effect = download
    monkeypatch.setattr(
        "apm_cli.deps.github_downloader.GitHubPackageDownloader", lambda **kwargs: downloader
    )
    with _prepare(package, caller) as source:
        plan = frontend.plan_contract(
            Path("job.contract.md"), caller, harness="copilot", source=source
        )
        assert plan.imported_skills[0].resolved_commit == "b" * 40
        assert plan.imported_skills[0].verified_package_hash == locked.content_hash
    assert downloader.download_package.call_count == 1
