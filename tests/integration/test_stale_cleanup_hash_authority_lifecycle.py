"""Stale cleanup deletes only files whose recorded hash matches (#3663).

A lockfile row without a recorded content hash proves nothing about who
wrote a file. On a fresh clone without receipts, a lockfile that claims a
user's own skill and instruction must not make install or uninstall delete
them; both commands route through the same cleanup gate. A forged matching
hash remains documented lockfile authority (enterprise/security.md).
"""

from __future__ import annotations

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._lockfile_chain_kit import (
    build_kit,
    fresh_clone,
    git,
    output,
    write,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
]

_USER_SKILL = ".claude/skills/mine/SKILL.md"
_USER_INSTRUCTION = ".github/instructions/mine.instructions.md"
_SKILL_BYTES = b"---\nname: mine\ndescription: mine\n---\nMine.\n"
_INSTRUCTION_BYTES = b"---\napplyTo: '**'\n---\nMine.\n"


def _claim_user_files_without_hashes(project) -> None:
    lock = load_yaml(project / "apm.lock.yaml")
    entry = next(dep for dep in lock["dependencies"] if dep["repo_url"] == "test/kit")
    entry["deployed_files"].extend([".claude/skills/mine", _USER_SKILL, _USER_INSTRUCTION])
    dump_yaml(lock, project / "apm.lock.yaml")


def _assert_user_files_kept(project) -> None:
    assert (project / _USER_SKILL).read_bytes() == _SKILL_BYTES
    assert (project / _USER_INSTRUCTION).read_bytes() == _INSTRUCTION_BYTES


def test_unhashed_lock_claims_never_delete_user_files(tmp_path, apm_binary_path) -> None:
    kit = build_kit(tmp_path, apm_binary_path, targets=("copilot", "claude"))
    clone = fresh_clone(kit, "clone")
    write(clone / _USER_SKILL, _SKILL_BYTES.decode("ascii"))
    write(clone / _USER_INSTRUCTION, _INSTRUCTION_BYTES.decode("ascii"))
    _claim_user_files_without_hashes(clone)
    git(clone, kit.env, "add", "--all")
    git(clone, kit.env, "commit", "-m", "user files claimed by a hand-edited lock")

    installed = kit.run("install", "--no-policy", cwd=clone)
    text = output(installed)
    assert installed.returncode == 0, text
    _assert_user_files_kept(clone)
    for path in (_USER_SKILL, _USER_INSTRUCTION):
        assert f"Kept {path}" in text and "no content hash" in text, text

    removed = kit.run("uninstall", "test/kit", cwd=clone)
    refusal = output(removed)
    assert removed.returncode != 0, refusal
    assert "could not remove tracked target files" in refusal, refusal
    assert _USER_INSTRUCTION in refusal, refusal
    _assert_user_files_kept(clone)
    assert "test/kit" in str(load_yaml(clone / "apm.yml")["dependencies"]["apm"])
