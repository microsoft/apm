"""A refused --frozen install withdraws only what this invocation wrote (#4309).

PROMISE: P-INV-I7 -- a failed command preserves durable state. Each case
fails without one tightening of the withdrawal owner: invocation-local
receipts, committed members protected, pre-existing directories kept, and an
accurate kept reason with a runnable next step.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.frozen_withdrawal import (
    WriteJournal,
    kept_lines,
    refusal_tip,
    withdraw_unrecorded_deployments,
)
from apm_cli.utils.content_hash import compute_file_hash

pytestmark = [pytest.mark.component, pytest.mark.promise("P-INV-I7")]


def _lock(*claims: str) -> LockFile:
    lock = LockFile(lockfile_version="1", apm_version="0.0.0")
    lock.add_dependency(
        LockedDependency(
            repo_url="https://github.com/o/r",
            resolved_ref="main",
            resolved_commit="a" * 40,
            depth=1,
            deployed_files=list(claims),
        )
    )
    return lock


def _write(root: Path, rel: str, text: str = "content\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")
    return path


def _receipt(path: Path) -> dict[str, str]:
    return {path.resolve().as_posix(): compute_file_hash(path)}


def _withdraw(root: Path, candidate: LockFile, unclaimed: set[str], **kwargs):
    defaults = {"committed_paths": (), "deploy_root": root, "user_scope": False}
    return withdraw_unrecorded_deployments([candidate], unclaimed, **{**defaults, **kwargs})


def test_a_file_a_previous_install_receipted_is_kept(tmp_path: Path) -> None:
    old = _write(tmp_path, ".claude/skills/old/SKILL.md")
    receipts = _receipt(old)  # written and receipted before this invocation
    journal = WriteJournal(receipts=receipts, directories=frozenset())

    outcome = _withdraw(
        tmp_path,
        _lock(".claude/skills/old/SKILL.md"),
        {".claude/skills/old/SKILL.md"},
        journal=journal,
        receipts_after=receipts,
    )

    assert old.is_file()
    assert outcome.removed == []
    assert outcome.kept == {".claude/skills/old/SKILL.md": "adopted"}


def test_a_committed_member_is_never_removed(tmp_path: Path) -> None:
    member = _write(tmp_path, ".claude/skills/ex/SKILL.md")
    new = _write(tmp_path, ".claude/skills/ex/new.md")
    journal = WriteJournal(receipts={}, directories=frozenset())

    outcome = _withdraw(
        tmp_path,
        _lock(".claude/skills/ex", ".claude/skills/ex/SKILL.md", ".claude/skills/ex/new.md"),
        {".claude/skills/ex", ".claude/skills/ex/new.md"},
        committed_paths=(".claude/skills/ex/SKILL.md",),
        journal=journal,
        receipts_after={**_receipt(member), **_receipt(new)},
    )

    assert member.is_file()
    assert not new.exists()
    assert outcome.removed == [".claude/skills/ex/new.md"]


def test_only_directories_this_invocation_created_are_removed(tmp_path: Path) -> None:
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    journal = WriteJournal(
        receipts={}, directories=frozenset_of_dirs(tmp_path, (".claude", ".claude/skills"))
    )
    created = _write(tmp_path, ".claude/skills/x/deep/SKILL.md")

    _withdraw(
        tmp_path,
        _lock(".claude/skills/x"),
        {".claude/skills/x"},
        journal=journal,
        receipts_after=_receipt(created),
    )

    assert not (tmp_path / ".claude/skills/x").exists()
    assert (tmp_path / ".claude/skills").is_dir()


def frozenset_of_dirs(root: Path, rels: tuple[str, ...]) -> frozenset[str]:
    return frozenset(
        {root.resolve().as_posix(), *((root / rel).resolve().as_posix() for rel in rels)}
    )


def test_each_kept_reason_is_named_with_a_next_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adopted = _write(tmp_path, ".claude/skills/adopted/SKILL.md")
    edited = _write(tmp_path, ".claude/skills/edited/SKILL.md")
    stuck = _write(tmp_path, ".claude/skills/stuck/SKILL.md")
    claims = {f".claude/skills/{name}/SKILL.md" for name in ("adopted", "edited", "stuck")}
    after = {
        **_receipt(adopted),
        edited.resolve().as_posix(): "sha256:" + "0" * 64,  # written, then edited
        **_receipt(stuck),
    }
    real_unlink = Path.unlink

    def _unlink(path: Path, *args, **kwargs) -> None:
        if path.name == "SKILL.md" and path.parent.name == "stuck":
            raise PermissionError("locked")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _unlink)

    outcome = _withdraw(
        tmp_path,
        _lock(*sorted(claims)),
        claims,
        journal=WriteJournal(receipts=_receipt(adopted), directories=frozenset()),
        receipts_after=after,
    )

    assert kept_lines(outcome.kept) == [
        "  - kept .claude/skills/adopted/SKILL.md: already on disk before this install "
        "(not written by it); delete or move it aside if it is not yours to keep, then "
        "rerun 'apm install --frozen'",
        "  - kept .claude/skills/edited/SKILL.md: edited after this install wrote it; "
        "move it aside, then rerun 'apm install --frozen'",
        "  - kept .claude/skills/stuck/SKILL.md: could not be removed; delete it, then "
        "rerun 'apm install --frozen'",
    ]
    assert adopted.is_file() and edited.is_file() and stuck.is_file()
    assert refusal_tip(outcome.kept).startswith(
        "Tip: the other unrecorded files were removed again and apm.lock.yaml is unchanged."
    )
    assert refusal_tip({}).startswith("Tip: the unrecorded files were removed again")
