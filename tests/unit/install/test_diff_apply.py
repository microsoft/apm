"""Diff-apply install engine (lane C S2): decisions that must take the full pipeline."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install import diff_apply
from apm_cli.install.diff_apply import Decline

pytestmark = pytest.mark.component

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


def test_store_miss_and_tampered_entry_decline(tmp_path: Path) -> None:
    data = b"hello\n"
    digest = diff_apply._store_put(tmp_path, data)
    assert diff_apply._store_get(tmp_path, digest) == data
    diff_apply._blob_path(tmp_path, digest).write_bytes(b"evil\n")
    with pytest.raises(Decline, match="tampered"):
        diff_apply._store_get(tmp_path, digest)
    with pytest.raises(Decline, match="store miss"):
        diff_apply._store_get(tmp_path, _digest(b"absent"))


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
        diff_apply._plan_files(tmp_path, lock, tmp_path / "store", [True], {})
    target.write_bytes(b"locked\n")
    with pytest.raises(Decline, match="without a receipt"):
        diff_apply._plan_files(tmp_path, lock, tmp_path / "store", [True], {})


def test_partially_applied_package_declines(tmp_path: Path) -> None:
    lock = _lock(**{"o/p": {"a.md": _digest(b"a"), "b.md": _digest(b"b")}})
    files = [(tmp_path / "a.md", b"a", _digest(b"a"), "o/p")]
    with pytest.raises(Decline, match="partial"):
        diff_apply._outcomes(lock, files, tmp_path)


def test_fresh_package_without_a_recorded_body_declines(tmp_path: Path) -> None:
    lock = _lock(**{"o/p": {"a.md": _digest(b"a")}})
    files = [(tmp_path / "a.md", b"a", _digest(b"a"), "o/p")]
    with pytest.raises(Decline, match="no recorded output"):
        diff_apply._outcomes(lock, files, tmp_path)
    untouched = diff_apply._outcomes(lock, [], tmp_path)
    assert [o.body for o in untouched] == [("  |-- (files unchanged)",)]
