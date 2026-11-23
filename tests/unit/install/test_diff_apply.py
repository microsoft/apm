"""Diff-apply install engine (lane C S2): decisions that must take the full pipeline."""

from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install import diff_apply
from apm_cli.install.diff_apply import Decline

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-frozen-install-mutation-eligibility"),
]

_ENGINE_MODULES = ("diff_apply.py", "fast_entry.py")
_REMOVALS = {"unlink", "rmtree", "remove", "rmdir", "removedirs", "rename", "replace_tree"}


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _targets() -> list:
    from apm_cli.integration.targets import KNOWN_TARGETS

    return [KNOWN_TARGETS["copilot"]]


def _lock(**deps: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        manifest_targets=["copilot"],
        dependencies={
            key: SimpleNamespace(
                repo_url=key,
                host="github.com",
                resolved_commit="a" * 40,
                content_hash="sha256:" + "b" * 64,
                deployed_file_hashes=files,
            )
            for key, files in deps.items()
        },
    )


def test_apply_path_never_deletes_outside_its_own_staging() -> None:
    """APPLY only writes; removal stays with the cleanup owner (VP ruling)."""
    root = Path(diff_apply.__file__).parent
    for name in _ENGINE_MODULES:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for func in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            calls = {
                c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", "")
                for c in ast.walk(func)
                if isinstance(c, ast.Call)
            }
            removals = calls & _REMOVALS
            if func.name == "_discard_staging":
                assert removals == {"rmtree"}
            elif func.name == "_undo_created":
                # Only a file this apply created, while it is still that object.
                assert removals <= {"unlink"}
            elif func.name == "_write":
                # Windows only: a rename there fails on an existing file (never replaces).
                assert removals == {"rename"}
            else:
                assert not removals, f"{name}:{func.name} removes {removals}"


def test_only_the_memoized_read_ever_swaps_the_process_umask(tmp_path: Path) -> None:
    """os.umask is process-wide: every reader goes through deploy_store.process_umask."""
    from apm_cli.cache.deploy_store import plain_file_mode

    src = Path(diff_apply.__file__).parents[1]
    callers = set()
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = {}
        for func in ast.walk(tree):
            if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(func):
                    owner.setdefault(id(node), func.name)
        for call in (c for c in ast.walk(tree) if isinstance(c, ast.Call)):
            if getattr(call.func, "attr", getattr(call.func, "id", "")) == "umask":
                where = owner.get(id(call), "<module>")
                callers.add(f"{path.relative_to(src).as_posix()}::{where}")
    assert callers == {"cache/deploy_store.py::process_umask"}

    # And a fast apply's write gets exactly the plain mode the memo reports.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    target = tmp_path / "out" / "a.md"
    diff_apply._write(target, b"x\n", scratch)
    assert target.read_bytes() == b"x\n"
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == plain_file_mode()


@pytest.mark.parametrize("rel", ["../x", "/abs", "a\\b", "a//b", "a/./b", ""])
def test_unsafe_deployed_paths_decline(tmp_path: Path, rel: str) -> None:
    with pytest.raises(Decline, match="unsafe path"):
        diff_apply._within(tmp_path, rel)


def test_symlinked_parent_of_a_deployed_path_declines(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(Decline, match="symlink"):
        diff_apply._within(tmp_path, "link/file.md")


def test_receipted_output_the_lock_no_longer_claims_declines(tmp_path: Path) -> None:
    lock = _lock(**{"o/p": {".github/prompts/a.prompt.md": _digest(b"a")}})
    orphan = (tmp_path / ".github/prompts/gone.prompt.md").as_posix()
    with pytest.raises(Decline, match="orphaned"):
        diff_apply._refuse_orphans(tmp_path, lock, {orphan: _digest(b"x")})


_REL = ".github/prompts/a.prompt.md"


@pytest.mark.parametrize(
    "rel",
    [
        ".git/hooks/pre-commit",
        ".github/.GIT/config",
        ".github/.git./config",
        ".github/.git /config",
        ".github/GIT~1/config",
        ".github/workflows/ci.yml",
        ".github/Actions/setup/action.yml",
        ".github/workflows./ci.yml",
        ".github/workflows /ci.yml",
        ".vscode/settings.json",
        "notes.md",
    ],
)
def test_a_lock_path_outside_the_target_roots_declines_before_any_write(
    tmp_path: Path, rel: str
) -> None:
    """The fast run writes only where the full pipeline would: active target roots, no .git."""
    lock = _lock(**{"o/p": {rel: _digest(b"x")}})
    records = {"o/p": {"files": {rel: [_digest(b"x")[7:], hashlib.sha256(b"x").hexdigest()]}}}
    deploy = SimpleNamespace(get=lambda hexd, raw_sha256=None: b"x")
    with pytest.raises(Decline, match=r"unsafe path|outside the target roots"):
        diff_apply._plan_files(tmp_path, lock, records, deploy, _untrusted, targets=_targets())
    assert not any(tmp_path.iterdir())


def test_a_lock_path_under_a_target_root_is_planned(tmp_path: Path) -> None:
    """Control for the cell above: the same row under .github/ is written."""
    lock = _lock(**{"o/p": {_REL: _digest(b"x")}})
    records = {"o/p": {"files": {_REL: [_digest(b"x")[7:], hashlib.sha256(b"x").hexdigest()]}}}
    deploy = SimpleNamespace(get=lambda hexd, raw_sha256=None: b"x")
    plan = diff_apply._plan_files(tmp_path, lock, records, deploy, _untrusted, targets=_targets())
    assert [(path, data) for path, data, _d, _o in plan] == [(tmp_path / _REL, b"x")]
    assert not any(tmp_path.iterdir()), "planning writes nothing"


def test_a_dependency_without_a_locked_content_hash_declines(tmp_path: Path) -> None:
    """Without a content hash a package copy cannot be proven: the full run decides."""
    lock = _lock(**{"o/p": {_REL: _digest(b"x")}})
    lock.dependencies["o/p"].content_hash = None
    with pytest.raises(Decline, match="no locked content hash"):
        diff_apply._plan_packages(tmp_path, lock)
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_a_non_directory_at_a_package_path_declines(tmp_path: Path, kind: str) -> None:
    """APPLY never replaces what sits at a package path: only a package copy is kept."""
    from apm_cli.models.dependency.reference import DependencyReference

    lock = _lock(**{"o/p": {_REL: _digest(b"x")}})
    lock.dependencies["o/p"].to_dependency_ref = lambda: DependencyReference.parse("o/p")
    occupant = tmp_path / "apm_modules" / "o" / "p"
    occupant.parent.mkdir(parents=True)
    if kind == "file":
        occupant.write_text("user\n", encoding="utf-8")
    else:
        (occupant.parent / "real").mkdir()
        occupant.symlink_to(occupant.parent / "real")
    with pytest.raises(Decline, match="non-directory at a package path"):
        diff_apply._plan_packages(tmp_path, lock)
    assert occupant.is_symlink() or occupant.read_text(encoding="utf-8") == "user\n"


def _untrusted(path: Path) -> None:
    return None


def test_a_present_file_needs_the_ledger_s_trusted_receipt(tmp_path: Path) -> None:
    """Identical bytes receipted under another owner or claim are not APM's file here."""
    lock = _lock(**{"o/p": {_REL: _digest(b"locked\n")}})
    target = tmp_path / _REL
    target.parent.mkdir(parents=True)
    target.write_bytes(b"locked\n")
    asked: list[Path] = []

    def stale(path: Path) -> None:
        # The raw receipt exists, but its owner/claim binding is stale.
        asked.append(path)

    with pytest.raises(Decline, match="without a receipt"):
        diff_apply._plan_files(tmp_path, lock, {}, None, stale, targets=_targets())
    assert asked == [target]
    trusted = diff_apply._plan_files(
        tmp_path, lock, {}, None, lambda path: _digest(b"locked\n"), targets=_targets()
    )
    assert trusted == []
    assert target.read_bytes() == b"locked\n"


_SHA_B, _SHA_C = "b" * 40, "c" * 40


def _edge_project(tmp_path: Path, child_ref: str):
    from apm_cli.deps.lockfile import LockFile

    (tmp_path / "apm.yml").write_text(
        "name: root\nversion: 1.0.0\ndependencies:\n  apm:\n    - o/p#v1.0.0\n",
        encoding="utf-8",
    )
    parent = tmp_path / "apm_modules" / "o" / "p"
    parent.mkdir(parents=True)
    (parent / "apm.yml").write_text(
        f"name: p\nversion: 1.0.0\ndependencies:\n  apm:\n    - o/q#{child_ref}\n",
        encoding="utf-8",
    )
    (tmp_path / "apm.lock.yaml").write_text(
        "lockfile_version: '1'\n"
        "generated_at: '2025-01-01T00:00:00Z'\n"
        "dependencies:\n"
        f"- repo_url: o/p\n  host: github.com\n  resolved_ref: v1.0.0\n"
        f"  resolved_commit: {'a' * 40}\n  content_hash: sha256:{'d' * 64}\n"
        f"- repo_url: o/q\n  host: github.com\n  resolved_ref: main\n"
        f"  resolved_commit: {_SHA_C}\n  content_hash: sha256:{'e' * 64}\n  depth: 2\n"
        "  resolved_by: o/p\n",
        encoding="utf-8",
    )
    return LockFile.read(tmp_path / "apm.lock.yaml")


def test_a_parent_pin_the_lock_does_not_satisfy_declines(tmp_path: Path) -> None:
    """A verified parent pinning its child to B refuses a child locked at C (frozen resolver)."""
    lock = _edge_project(tmp_path, _SHA_B)
    before = (tmp_path / "apm.lock.yaml").read_bytes()
    with pytest.raises(Decline, match="immutable requirement"):
        diff_apply._refuse_unproven_edges(tmp_path, lock, [])
    assert (tmp_path / "apm.lock.yaml").read_bytes() == before


def test_a_parent_pin_the_lock_satisfies_is_admitted(tmp_path: Path) -> None:
    """Control: the same edge pinned to the locked commit passes."""
    lock = _edge_project(tmp_path, _SHA_C)
    before = (tmp_path / "apm.lock.yaml").read_bytes()
    diff_apply._refuse_unproven_edges(tmp_path, lock, [])
    assert (tmp_path / "apm.lock.yaml").read_bytes() == before


@pytest.mark.parametrize(
    "env",
    [
        {"PROXY_REGISTRY_URL": "https://proxy.example/artifactory/api/vcs"},
        {
            "PROXY_REGISTRY_URL": "https://proxy.example/artifactory/api/vcs",
            "PROXY_REGISTRY_ONLY": "1",
        },
    ],
    ids=["proxy", "proxy-only"],
)
def test_a_configured_registry_proxy_takes_the_full_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, env: dict[str, str]
) -> None:
    """Source routing (and the proxy-only lockfile refusal) is the full pipeline's decision."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("APM_INSTALL_FAST_PATH", "trace")
    (tmp_path / "apm.yml").write_text("name: demo\nversion: 1.0.0\n", encoding="utf-8")
    disposition = SimpleNamespace(replay=False, withhold_lockfile_writes=False)

    applied = diff_apply.try_apply({}, ["install"], tmp_path, frozen_disposition=disposition)

    assert applied is False
    assert "full run (registry proxy configured)" in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["apm.yml"]


def test_a_lockfile_the_cli_gate_refuses_takes_the_full_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A lockfile declaring apm_version twice is refused by the command's own gate."""
    from apm_cli.deps.lockfile import installed_apm_version

    for name in ("PROXY_REGISTRY_URL", "PROXY_REGISTRY_ONLY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APM_INSTALL_FAST_PATH", "trace")
    (tmp_path / "apm.yml").write_text("name: demo\nversion: 1.0.0\n", encoding="utf-8")
    version = installed_apm_version()
    lock = (
        f"lockfile_version: '1'\napm_version: '0.0.1'\napm_version: '{version}'\n"
        "generated_at: '2025-01-01T00:00:00Z'\ndependencies: []\n"
    )
    (tmp_path / "apm.lock.yaml").write_text(lock, encoding="utf-8")
    disposition = SimpleNamespace(replay=False, withhold_lockfile_writes=False)

    applied = diff_apply.try_apply({}, ["install"], tmp_path, frozen_disposition=disposition)

    assert applied is False
    assert "full run (lockfile generator)" in capsys.readouterr().err
    assert (tmp_path / "apm.lock.yaml").read_text(encoding="utf-8") == lock


@pytest.mark.parametrize("owner", ["this-workspace", "another-workspace"])
def test_only_this_workspace_s_abandoned_journal_declines_the_fast_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, owner: str
) -> None:
    """Another workspace's journal waits for that workspace; it costs this install nothing."""
    from apm_cli.install.deploy_snapshot import DeployJournal

    for name in ("PROXY_REGISTRY_URL", "PROXY_REGISTRY_ONLY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("APM_INSTALL_FAST_PATH", "trace")
    project, other = tmp_path / "project", tmp_path / "other"
    project.mkdir()
    other.mkdir()
    _locked_project(project)
    root = project if owner == "this-workspace" else other
    disposition = SimpleNamespace(replay=False, withhold_lockfile_writes=False)

    def decide() -> tuple[bool, list[str]]:
        applied = diff_apply.try_apply({}, ["install"], project, frozen_disposition=disposition)
        trace = [line for line in capsys.readouterr().err.splitlines() if "fast path" in line]
        return applied, trace

    without_journal = decide()
    assert not any("interrupted install journal" in line for line in without_journal[1])
    killed = root / "killed.md"
    journal = DeployJournal(tmp_path / "home" / "install-snapshots", root=root)
    journal.record(killed)
    killed.write_bytes(b"half\n")
    journal.after_write(killed)

    with_journal = decide()

    if owner == "this-workspace":
        assert with_journal[0] is False
        assert any("full run (interrupted install journal)" in line for line in with_journal[1])
    else:
        # The same decision, and the same trace, as with no journal at all.
        assert with_journal == without_journal
    assert journal.store is not None and journal.store.is_dir()
    assert killed.read_bytes() == b"half\n"


def test_a_symlink_in_a_package_copy_takes_the_full_pipeline(tmp_path: Path) -> None:
    """Package hashes skip symlinks and the source plan warns on them: only the full run judges."""
    from apm_cli.install import noop_diff

    lock = _lock(**{"o/p": {}})
    lock.dependencies["o/p"].to_dependency_ref = lambda: SimpleNamespace(
        get_install_path=lambda modules: modules / "o" / "p"
    )
    agents = tmp_path / "apm_modules" / "o" / "p" / ".apm" / "agents"
    agents.mkdir(parents=True)
    (agents / "real.agent.md").write_text("# agent\n", encoding="ascii")
    assert noop_diff.sources_scan_clean(tmp_path, lock)
    (agents / "extra.agent.md").symlink_to(agents / "real.agent.md")
    assert not noop_diff.sources_scan_clean(tmp_path, lock)
    assert (agents / "real.agent.md").read_text(encoding="ascii") == "# agent\n"


def test_a_symlinked_package_root_takes_the_full_pipeline(tmp_path: Path) -> None:
    """apm_modules/<pkg> pointing elsewhere is never walked as the package (W3c)."""
    from apm_cli.install import noop_diff

    lock = _lock(**{"o/p": {}})
    lock.dependencies["o/p"].to_dependency_ref = lambda: SimpleNamespace(
        get_install_path=lambda modules: modules / "o" / "p"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "apm.yml").write_text("name: p\n", encoding="ascii")
    (tmp_path / "apm_modules" / "o").mkdir(parents=True)
    (tmp_path / "apm_modules" / "o" / "p").symlink_to(elsewhere)
    assert not noop_diff.sources_scan_clean(tmp_path, lock)
    assert (elsewhere / "apm.yml").read_text(encoding="ascii") == "name: p\n"


def test_edited_or_unreceipted_deployed_file_declines(tmp_path: Path) -> None:
    rel = ".github/prompts/a.prompt.md"
    lock = _lock(**{"o/p": {rel: _digest(b"locked\n")}})
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    target.write_bytes(b"edited\n")
    with pytest.raises(Decline, match="edited"):
        diff_apply._plan_files(tmp_path, lock, {}, None, _untrusted, targets=_targets())
    target.write_bytes(b"locked\n")
    with pytest.raises(Decline, match="without a receipt"):
        diff_apply._plan_files(tmp_path, lock, {}, None, _untrusted, targets=_targets())


def test_partially_applied_package_declines(tmp_path: Path) -> None:
    lock = _lock(**{"o/p": {"a.md": _digest(b"a"), "b.md": _digest(b"b")}})
    files = [(tmp_path / "a.md", b"a", _digest(b"a"), "o/p")]
    with pytest.raises(Decline, match="partial"):
        diff_apply._outcomes(lock, files, {})


def test_fresh_package_without_a_recorded_body_declines(tmp_path: Path) -> None:
    lock = _lock(**{"o/p": {"a.md": _digest(b"a")}})
    files = [(tmp_path / "a.md", b"a", _digest(b"a"), "o/p")]
    with pytest.raises(Decline, match="no recorded output"):
        diff_apply._outcomes(lock, files, {"o/p": {"body": None}})
    untouched = diff_apply._outcomes(lock, [], {})
    assert [o.body for o in untouched] == [("  |-- (files unchanged)",)]


def test_lock_hashes_without_a_matching_package_record_decline() -> None:
    """A lock edit cannot select cached bytes: the package-bound record must equal the lock."""
    lock = _lock(**{"o/p": {"a.md": _digest(b"edited-lock")}})
    recorded = {"v": 1, "files": {"a.md": ["0" * 64, "0" * 64]}, "ascii_only": True, "body": []}
    deploy = SimpleNamespace(package_record=lambda *args: recorded)
    with pytest.raises(Decline, match="no matching package record"):
        diff_apply._package_records(lock, ["copilot"], deploy)


def test_non_ascii_deployed_bytes_take_the_full_run(tmp_path: Path) -> None:
    data = "zero\u200bwidth\n".encode()
    lock = _lock(**{"o/p": {_REL: _digest(data)}})
    records = {"o/p": {"files": {_REL: [_digest(data)[7:], hashlib.sha256(data).hexdigest()]}}}
    deploy = SimpleNamespace(get=lambda hexd, raw_sha256=None: data)
    with pytest.raises(Decline, match="non-ASCII"):
        diff_apply._plan_files(tmp_path, lock, records, deploy, _untrusted, targets=_targets())


@pytest.mark.parametrize("argv", [["install"], ["install", "--only", "apm"]])
def test_decide_seam_exits_nonzero_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """APM_INSTALL_FAST_PATH=decide is a test seam: never a green install, never a write."""
    from click.testing import CliRunner

    from apm_cli.cli import cli
    from apm_cli.install.fast_entry import DECIDE_EXIT

    (tmp_path / "apm.yml").write_text("name: demo\nversion: 1.0.0\n", encoding="utf-8")
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APM_INSTALL_FAST_PATH", "decide")
    result = CliRunner().invoke(cli, argv, obj={})
    assert result.exit_code == DECIDE_EXIT
    assert "install fast path: full run" in result.output
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert after == before
    assert (tmp_path / "apm.yml").read_text(encoding="utf-8") == "name: demo\nversion: 1.0.0\n"


def test_escape_sequences_in_deployed_bytes_take_the_full_run(tmp_path: Path) -> None:
    data = b'{"command": "echo \\u202e"}\n'
    lock = _lock(**{"o/p": {".github/hooks/a.json": _digest(data)}})
    records = {
        "o/p": {
            "files": {".github/hooks/a.json": [_digest(data)[7:], hashlib.sha256(data).hexdigest()]}
        }
    }
    deploy = SimpleNamespace(get=lambda hexd, raw_sha256=None: data)
    with pytest.raises(Decline, match="non-ASCII"):
        diff_apply._plan_files(tmp_path, lock, records, deploy, _untrusted, targets=_targets())


def test_a_failure_while_rendering_restores_the_pre_run_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Kill-mid-commit: the journal puts back every path the apply touched, then declines."""
    project = tmp_path / "project"
    keep = project / ".gitignore"
    keep.parent.mkdir()
    keep.write_text("user\n", encoding="utf-8")
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    target = project / ".github" / "prompts" / "a.prompt.md"
    session = SimpleNamespace(record=lambda *a: None, flush=lambda: None)

    lock = project / "apm.lock.yaml"
    lock.write_text("locked\n", encoding="utf-8")

    def render() -> None:
        from apm_cli.integration import write_seam

        print("[+] rendered before the failure")
        write_seam.write_text(keep, "apm_modules/\n")
        lock.write_text("rewritten\n", encoding="utf-8")
        raise OSError("disk full")

    files = [(target, b"x\n", _digest(b"x\n"), "o/p")]
    with pytest.raises(Decline, match="write failed"):
        diff_apply._commit(project, None, [], files, [], session, False, render)
    assert not target.exists()
    assert not (project / ".github").exists()
    assert keep.read_text(encoding="utf-8") == "user\n"
    assert lock.read_text(encoding="utf-8") == "locked\n"
    assert not (project / "apm_modules").exists(), "the staging root goes with the rollback"
    assert "rendered before the failure" not in capsys.readouterr().out, "fallback prints once"


@pytest.mark.parametrize("existed", [False, True])
def test_an_apply_with_no_package_to_stage_leaves_apm_modules_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existed: bool
) -> None:
    """A converged apply of a project without packages adds no apm_modules (#5159 I2)."""
    project = tmp_path / "project"
    project.mkdir()
    modules = project / "apm_modules"
    if existed:
        modules.mkdir()
    (project / "apm.lock.yaml").write_text("locked\n", encoding="utf-8")
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    session = SimpleNamespace(record=lambda *a: None, flush=lambda: None)

    diff_apply._commit(project, None, [], [], [], session, False, lambda: None)

    assert modules.is_dir() is existed
    if existed:
        assert not any(modules.iterdir())


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0, reason="needs POSIX directory permissions"
)
def test_an_incomplete_restore_stops_instead_of_falling_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path the journal cannot put back is reported; it never becomes a fallback."""
    from apm_cli.install.transaction import ApplyRestoreIncomplete

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    target = project / ".github" / "prompts" / "a.prompt.md"
    session = SimpleNamespace(record=lambda *a: None, flush=lambda: None)

    def render() -> None:
        target.parent.chmod(0o555)  # the created file can no longer be removed
        raise OSError("disk full")

    files = [(target, b"x\n", _digest(b"x\n"), "o/p")]
    try:
        with pytest.raises(ApplyRestoreIncomplete) as caught:
            diff_apply._commit(project, None, [], files, [], session, False, render)
    finally:
        target.parent.chmod(0o755)
    assert [p.name for p, _reason in caught.value.issues][:1] == ["a.prompt.md"]


def test_a_lost_create_race_keeps_the_file_that_won(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file created at the destination before APPLY's link survives the rollback."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    ours = project / ".github" / "prompts" / "a.prompt.md"
    theirs = project / ".github" / "prompts" / "b.prompt.md"
    session = SimpleNamespace(record=lambda *a: None, flush=lambda: None)

    def racing_link(source, target):
        if Path(target) == theirs:
            theirs.write_text("someone else\n", encoding="utf-8")
        return real_link(source, target)

    # The no-replace primitive: a rename on Windows, a hard link elsewhere.
    racing_name = "rename" if os.name == "nt" else "link"
    real_link = getattr(os, racing_name)
    monkeypatch.setattr(f"os.{racing_name}", racing_link)
    files = [
        (ours, b"x\n", _digest(b"x\n"), "o/p"),
        (theirs, b"y\n", _digest(b"y\n"), "o/p"),
    ]
    with pytest.raises(Decline, match="write failed"):
        diff_apply._commit(project, None, [], files, [], session, False, lambda: None)
    monkeypatch.undo()

    assert not ours.exists(), "what APPLY created goes"
    assert theirs.read_text(encoding="utf-8") == "someone else\n", "what it did not create stays"


def test_apply_never_replaces_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "x.md"
    target.write_text("user\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(FileExistsError):
        diff_apply._write(target, b"apm\n", scratch)
    assert target.read_text(encoding="utf-8") == "user\n"


@pytest.mark.parametrize("marker", ["deleted", "foreign"])
def test_a_present_copy_without_its_cache_pin_marker_declines(tmp_path: Path, marker: str) -> None:
    """A matching content hash does not restore `.apm-pin`: the full run's pin sync does."""
    from apm_cli.install.cache_pin import MARKER_FILENAME, write_marker
    from apm_cli.models.dependency.reference import DependencyReference

    lock = _lock(**{"o/p": {_REL: _digest(b"x")}})
    lock.dependencies["o/p"].to_dependency_ref = lambda: DependencyReference.parse("o/p")
    copy = tmp_path / "apm_modules" / "o" / "p"
    copy.mkdir(parents=True)
    (copy / "apm.yml").write_text("name: p\n", encoding="ascii")
    if marker == "foreign":
        write_marker(copy, "c" * 40)
    before = sorted(p.name for p in copy.iterdir())
    with pytest.raises(Decline, match="cache pin marker"):
        diff_apply._plan_packages(tmp_path, lock)
    assert sorted(p.name for p in copy.iterdir()) == before
    assert (copy / MARKER_FILENAME).exists() is (marker == "foreign")


def _locked_project(tmp_path: Path, row: str = "") -> list[str]:
    from apm_cli.deps.lockfile import installed_apm_version

    (tmp_path / "apm.yml").write_text(
        "name: demo\nversion: 1.0.0\ndependencies:\n  apm:\n    - o/p#v1.0.0\n",
        encoding="utf-8",
    )
    (tmp_path / "apm.lock.yaml").write_text(
        "lockfile_version: '1'\n"
        "generated_at: '2025-01-01T00:00:00Z'\n"
        f"apm_version: '{installed_apm_version()}'\n"
        "dependencies:\n"
        f"- repo_url: o/p\n  host: github.com\n  resolved_ref: v1.0.0\n"
        f"  resolved_commit: {'a' * 40}\n  content_hash: sha256:{'d' * 64}\n" + row,
        encoding="utf-8",
    )
    return sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))


def _assert_declines_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, before: list[str], reason: str
) -> None:
    monkeypatch.setenv("APM_INSTALL_FAST_PATH", "trace")
    disposition = SimpleNamespace(replay=False, withhold_lockfile_writes=False)
    applied = diff_apply.try_apply({}, ["install"], tmp_path, frozen_disposition=disposition)
    assert applied is False
    assert f"full run ({reason})" in capsys.readouterr().err
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


def test_apm_owned_codex_registrations_take_the_full_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A dropped Codex plugin is retired only by the full run's codex_plugins phase."""
    journal = tmp_path / ".codex-native" / "ownership.json"
    journal.parent.mkdir()
    journal.write_text('{"version": 1, "plugins": {}}', encoding="ascii")
    before = _locked_project(tmp_path)
    _assert_declines_with(tmp_path, monkeypatch, capsys, before, "codex native registrations")


@pytest.mark.parametrize("package_type", ["marketplace_plugin", "agent_plugin"])
def test_a_locked_plugin_package_takes_the_full_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, package_type: str
) -> None:
    """Native admission and registration of a plugin package are full-run phases."""
    before = _locked_project(tmp_path, f"  package_type: {package_type}\n")
    _assert_declines_with(tmp_path, monkeypatch, capsys, before, "plugin package")


def test_a_locked_native_plugin_record_declines(tmp_path: Path) -> None:
    """A removed enable declaration or plugin root is restored only by claude_plugins."""
    lock = _lock(**{"o/p": {}})
    lock.dependencies["o/p"].native_plugin = SimpleNamespace(name="p")
    with pytest.raises(Decline, match="native plugin"):
        diff_apply._refuse_native_plugins(tmp_path, lock)
    assert not any(tmp_path.iterdir())


def test_the_legacy_skill_layout_request_takes_the_full_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """APM_LEGACY_SKILL_PATHS changes the layout the deploy record does not key."""
    monkeypatch.setenv("APM_LEGACY_SKILL_PATHS", "1")
    before = _locked_project(tmp_path)
    _assert_declines_with(tmp_path, monkeypatch, capsys, before, "legacy skill paths")


def test_a_pending_legacy_skill_migration_takes_the_full_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """After a --legacy-skill-paths install, a bare install migrates to .agents/skills/."""
    monkeypatch.delenv("APM_LEGACY_SKILL_PATHS", raising=False)
    before = _locked_project(tmp_path, "  deployed_files:\n  - .github/skills/s/SKILL.md\n")
    _assert_declines_with(tmp_path, monkeypatch, capsys, before, "legacy skill migration pending")


def test_an_abandoned_journal_declines_and_the_full_run_recovers_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A killed install's journal sends the fast path to the full run, which replays it."""
    from apm_cli.constants import APM_MODULES_DIR
    from apm_cli.install.deploy_snapshot import DeployJournal
    from apm_cli.install.transaction import replay_abandoned_deploys

    for name in ("PROXY_REGISTRY_URL", "PROXY_REGISTRY_ONLY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    _locked_project(project)
    killed = project / ".github" / "prompts" / "killed.prompt.md"
    journal = DeployJournal(tmp_path / "home" / "install-snapshots", root=project)
    journal.record(killed)
    killed.parent.mkdir(parents=True)
    killed.write_bytes(b"half\n")
    journal.after_write(killed)
    store = journal.store
    assert store is not None

    def contents() -> dict[str, bytes]:
        return {
            f"{base.name}/{p.relative_to(base).as_posix()}": p.read_bytes()
            for base in (project, store)
            for p in sorted(base.rglob("*"))
            if p.is_file()
        }

    untouched = contents()
    before = sorted(str(p.relative_to(project)) for p in project.rglob("*"))

    _assert_declines_with(project, monkeypatch, capsys, before, "interrupted install journal")
    assert contents() == untouched, "a decline reads; it never changes the project or the journal"

    replay_abandoned_deploys(project / APM_MODULES_DIR, None)

    assert not killed.exists()
    assert not store.exists()


def test_a_fast_apply_config_write_is_sealed_at_the_write_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fast apply journals each seam write and listens for its post-write seal.

    Its journal is in memory (no deploy root): it records and seals at the
    seam like an install's, but persists nothing for replay (#4982).
    """
    from apm_cli.install.transaction import journaled_apply
    from apm_cli.integration import write_seam

    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    config = tmp_path / "project" / ".gitignore"
    config.parent.mkdir()

    with journaled_apply() as journal:
        sealed_by = write_seam._after
        write_seam.write_text(config, "apm_modules/\n")
        recorded = [unit.path for unit in journal.units]

    assert sealed_by == journal.after_write
    assert recorded == [config]
    assert config.read_text(encoding="utf-8") == "apm_modules/\n"
    assert write_seam._after is None, "the seam stops listening once the apply settles"
