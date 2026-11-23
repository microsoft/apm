"""Diff-apply install engine (lane C S2): decisions that must take the full pipeline."""

from __future__ import annotations

import ast
import hashlib
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
            else:
                assert not removals, f"{name}:{func.name} removes {removals}"


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


def test_edited_or_unreceipted_deployed_file_declines(tmp_path: Path) -> None:
    rel = ".github/prompts/a.prompt.md"
    lock = _lock(**{"o/p": {rel: _digest(b"locked\n")}})
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    target.write_bytes(b"edited\n")
    with pytest.raises(Decline, match="edited"):
        diff_apply._plan_files(tmp_path, lock, {}, None, {})
    target.write_bytes(b"locked\n")
    with pytest.raises(Decline, match="without a receipt"):
        diff_apply._plan_files(tmp_path, lock, {}, None, {})


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
    lock = _lock(**{"o/p": {"a.md": _digest(data)}})
    records = {"o/p": {"files": {"a.md": [_digest(data)[7:], hashlib.sha256(data).hexdigest()]}}}
    deploy = SimpleNamespace(get=lambda hexd, raw_sha256=None: data)
    with pytest.raises(Decline, match="non-ASCII"):
        diff_apply._plan_files(tmp_path, lock, records, deploy, {})


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
    lock = _lock(**{"o/p": {"a.json": _digest(data)}})
    records = {"o/p": {"files": {"a.json": [_digest(data)[7:], hashlib.sha256(data).hexdigest()]}}}
    deploy = SimpleNamespace(get=lambda hexd, raw_sha256=None: data)
    with pytest.raises(Decline, match="non-ASCII"):
        diff_apply._plan_files(tmp_path, lock, records, deploy, {})


def test_a_failure_while_rendering_restores_the_pre_run_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill-mid-commit: the journal puts back every path the apply touched, then declines."""
    project = tmp_path / "project"
    keep = project / ".gitignore"
    keep.parent.mkdir()
    keep.write_text("user\n", encoding="utf-8")
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    target = project / ".github" / "prompts" / "a.prompt.md"
    session = SimpleNamespace(record=lambda *a: None, flush=lambda: None)

    def render() -> None:
        from apm_cli.integration import write_seam

        write_seam.write_text(keep, "apm_modules/\n")
        raise OSError("disk full")

    files = [(target, b"x\n", _digest(b"x\n"), "o/p")]
    with pytest.raises(Decline, match="write failed"):
        diff_apply._commit(project, None, [], files, [], session, False, render)
    assert not target.exists()
    assert not (project / ".github").exists()
    assert keep.read_text(encoding="utf-8") == "user\n"


def test_apply_never_replaces_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "x.md"
    target.write_text("user\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        diff_apply._write(target, b"apm\n")
    assert target.read_text(encoding="utf-8") == "user\n"
