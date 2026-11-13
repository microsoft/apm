"""Receipt-less clones restore, edit, audit and update without drift (#3649).

Every step asserts the exit code, the project tree, ``apm.lock.yaml`` and,
where relevant, local deployment receipts. Fresh clones without receipts or
download cache emulate CI runners.
"""

from __future__ import annotations

import pytest

from apm_cli.utils.file_ops import robust_rmtree
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._lockfile_chain_kit import (
    GUIDANCE,
    REFUSAL,
    Kit,
    build_kit,
    evict_machine_state,
    fresh_clone,
    git,
    locked_tag,
    output,
    publish_v110,
    receipt_destinations,
    status,
    tree,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_SKILL_ROOTS = (".agents/skills", ".claude/skills")
_INSTRUCTION = ".github/instructions/style.instructions.md"


def _assert_ok(result) -> None:
    text = output(result)
    assert result.returncode == 0, text
    assert REFUSAL not in text


def test_cold_clone_restore_is_idempotent_across_targets(tmp_path, apm_binary_path) -> None:
    """install, install, --frozen and audit --ci on a receipt-less clone are no-ops."""
    kit = build_kit(tmp_path, apm_binary_path)
    clone = fresh_clone(kit, "clone")
    before = tree(clone)
    assert not receipt_destinations(kit)

    for command in (
        ("install", "--no-policy"),
        ("install", "--no-policy"),
        ("install", "--no-policy", "--frozen"),
        ("audit", "--ci", "--no-policy"),
    ):
        result = kit.run(*command, cwd=clone)
        _assert_ok(result)
        assert tree(clone) == before, command
        assert status(clone, kit.env) == "", command

    recorded = receipt_destinations(kit)
    for root in _SKILL_ROOTS:
        for name in ("alpha", "beta", "gamma"):
            anchor = (clone / root / name / "SKILL.md").resolve().as_posix()
            assert anchor in recorded, sorted(recorded)


def test_cold_clone_restores_missing_files_and_refuses_edited_skill(
    tmp_path, apm_binary_path
) -> None:
    """Deleted managed files come back; an edited skill member fails closed."""
    kit = build_kit(tmp_path, apm_binary_path)

    missing = fresh_clone(kit, "missing")
    robust_rmtree(missing / ".claude/skills/gamma")
    (missing / _INSTRUCTION).unlink()
    lock = (missing / "apm.lock.yaml").read_bytes()
    result = kit.run("install", "--no-policy", "--frozen", cwd=missing)
    _assert_ok(result)
    assert status(missing, kit.env) == ""
    assert (missing / "apm.lock.yaml").read_bytes() == lock

    edited = fresh_clone(kit, "edited")
    member = edited / ".claude/skills/beta/extra.md"
    member.write_bytes(b"Local edit.\n")
    (edited / ".agents/skills/gamma/SKILL.md").unlink()
    before = tree(edited)
    for _ in range(2):
        result = kit.run("install", "--no-policy", "--frozen", cwd=edited)
        text = output(result)
        assert result.returncode != 0, text
        assert REFUSAL in text and GUIDANCE in text, text
        assert tree(edited) == before


def test_cold_update_replaces_adds_and_removes_then_converges(tmp_path, apm_binary_path) -> None:
    """Receipt-less update moves to v1.1.0 and every later command is a no-op."""
    kit = build_kit(tmp_path, apm_binary_path)
    publish_v110(kit)
    clone = fresh_clone(kit, "clone")

    result = kit.run("update", "--yes", cwd=clone)
    _assert_ok(result)
    assert locked_tag(clone) == "v1.1.0"
    for root in _SKILL_ROOTS:
        assert "Alpha v2." in (clone / root / "alpha/SKILL.md").read_text(encoding="utf-8")
        assert not (clone / root / "alpha/notes.md").exists()
        assert not (clone / root / "gamma").exists()
        assert (clone / root / "delta/SKILL.md").is_file()
        assert (clone / root / "beta/extra.md").read_bytes() == b"Beta extra.\n"
    assert "# Style v2" in (clone / _INSTRUCTION).read_text(encoding="utf-8")
    assert not (clone / ".github/prompts/hello.prompt.md").exists()

    after = tree(clone)
    git_after = status(clone, kit.env)
    evict_machine_state(kit)
    for command in (
        ("install", "--no-policy", "--frozen"),
        ("install", "--no-policy"),
        ("audit", "--ci", "--no-policy"),
    ):
        result = kit.run(*command, cwd=clone)
        _assert_ok(result)
        assert tree(clone) == after, command
        assert status(clone, kit.env) == git_after, command


@pytest.mark.parametrize(
    "member",
    [".claude/skills/alpha/SKILL.md", ".agents/skills/beta/extra.md"],
    ids=["changed-upstream", "unchanged-upstream"],
)
def test_cold_update_with_edited_skill_is_all_or_nothing(tmp_path, apm_binary_path, member) -> None:
    """An edited skill blocks the whole update: no new files, no deletions, same lock."""
    kit = build_kit(tmp_path, apm_binary_path)
    publish_v110(kit)
    clone = fresh_clone(kit, "clone")
    (clone / member).write_bytes(b"Local edit.\n")
    before = tree(clone)

    result = kit.run("update", "--yes", cwd=clone)

    text = output(result)
    assert result.returncode != 0, text
    assert REFUSAL in text and GUIDANCE in text, text
    assert tree(clone) == before
    assert locked_tag(clone) == "v1.0.0"


def _between(kit: Kit, mode: str) -> None:
    if mode == "cache-clean":
        result = kit.run("cache", "clean", "--yes")
        assert result.returncode == 0, result.stdout + result.stderr
    elif mode == "apm-home-wipe":
        evict_machine_state(kit, apm_home=True)


@pytest.mark.parametrize("mode", ["warm", "cache-clean", "apm-home-wipe"])
def test_edit_frozen_audit_revert_prune_uninstall_chain(tmp_path, apm_binary_path, mode) -> None:
    """The maintainer chain, asserting disk, lock and exit code after every step."""
    kit = build_kit(tmp_path, apm_binary_path)
    project = kit.project
    clean = tree(project)
    lock = (project / "apm.lock.yaml").read_bytes()

    (project / ".claude/skills/beta/SKILL.md").write_bytes(b"Edited skill.\n")
    (project / _INSTRUCTION).write_bytes(b"Edited instruction.\n")
    edited = tree(project)
    _between(kit, mode)

    # With receipts the edited skill is kept with a warning and the edited
    # instruction is re-synced (export-patch hint, #2162), then the kept edit
    # fails the req-lk-017 deployed-hash check (#3654); without receipts the
    # whole install fails closed before deployment.
    frozen = kit.run("install", "--no-policy", "--frozen")
    assert (project / "apm.lock.yaml").read_bytes() == lock
    assert (project / ".claude/skills/beta/SKILL.md").read_bytes() == b"Edited skill.\n"
    assert "apm export-patch" in output(frozen), output(frozen)
    assert frozen.returncode != 0, output(frozen)
    if mode == "apm-home-wipe":
        assert REFUSAL in output(frozen), output(frozen)
        assert tree(project) == edited
    else:
        assert "Kept user-edited file .claude/skills/beta/SKILL.md" in output(frozen)
        assert ".claude/skills/beta/SKILL.md: expected sha256:" in output(frozen)
        assert (project / _INSTRUCTION).read_bytes() == clean[_INSTRUCTION]
    current = tree(project)
    _between(kit, mode)

    audit = kit.run("audit", "--ci", "--no-policy")
    assert audit.returncode != 0, output(audit)
    assert "hash drift" in output(audit), output(audit)
    assert ".claude/skills/beta/SKILL.md" in output(audit), output(audit)
    assert tree(project) == current
    _between(kit, mode)

    git(project, kit.env, "checkout", "--", ".")
    assert tree(project) == clean
    for command in (("install", "--no-policy", "--frozen"), ("prune",)):
        result = kit.run(*command)
        _assert_ok(result)
        assert tree(project) == clean, command
        _between(kit, mode)

    removed = kit.run("uninstall", "test/kit")
    _assert_ok(removed)
    manifest = load_yaml(project / "apm.yml")
    assert not manifest["dependencies"]["apm"]
    assert not (project / "apm.lock.yaml").exists()
    leftovers = [path for path in tree(project) if path.startswith((".claude/", ".agents/"))]
    assert leftovers == [], leftovers
    _between(kit, mode)

    readd = kit.run("install", "--no-policy", "test/kit#^1.0.0")
    _assert_ok(readd)
    assert tree(project).keys() == clean.keys()
    for path in clean:
        if path not in ("apm.yml", "apm.lock.yaml"):
            assert tree(project)[path] == clean[path], path
    _between(kit, mode)
    again = kit.run("install", "--no-policy", "--frozen")
    _assert_ok(again)


def test_lockfile_tamper_fails_closed_without_writes(tmp_path, apm_binary_path) -> None:
    """A forged content_hash or a dropped lock row never writes under --frozen."""
    kit = build_kit(tmp_path, apm_binary_path)
    project = kit.project
    clean = tree(project)
    original = load_yaml(project / "apm.lock.yaml")

    forged = load_yaml(project / "apm.lock.yaml")
    forged["dependencies"][0]["content_hash"] = "sha256:" + "0" * 64
    dump_yaml(forged, project / "apm.lock.yaml")
    forged_bytes = (project / "apm.lock.yaml").read_bytes()
    robust_rmtree(project / "apm_modules")
    evict_machine_state(kit)
    result = kit.run("install", "--no-policy", "--frozen")
    assert result.returncode != 0, output(result)
    assert "Content hash mismatch" in output(result), output(result)
    assert (project / "apm.lock.yaml").read_bytes() == forged_bytes
    assert {k: v for k, v in tree(project).items() if k != "apm.lock.yaml"} == {
        k: v for k, v in clean.items() if k != "apm.lock.yaml"
    }

    dropped = dict(original)
    dropped["dependencies"] = []
    dump_yaml(dropped, project / "apm.lock.yaml")
    dropped_bytes = (project / "apm.lock.yaml").read_bytes()
    result = kit.run("install", "--no-policy", "--frozen")
    assert result.returncode != 0, output(result)
    assert "out of sync" in output(result), output(result)
    assert (project / "apm.lock.yaml").read_bytes() == dropped_bytes


def _uninstall_after_apm_home_wipe(tmp_path, apm_binary_path):
    kit = build_kit(tmp_path, apm_binary_path)
    project = kit.project
    evict_machine_state(kit, apm_home=True)
    (project / ".claude/skills/alpha/mine.md").write_bytes(b"Mine.\n")
    (project / ".claude/skills/beta/SKILL.md").write_bytes(b"Edited.\n")
    return kit, kit.run("uninstall", "test/kit")


def test_uninstall_after_apm_home_wipe_keeps_user_files(tmp_path, apm_binary_path) -> None:
    """Without receipts, uninstall keeps edited and user-added files."""
    kit, result = _uninstall_after_apm_home_wipe(tmp_path, apm_binary_path)
    project = kit.project
    assert result.returncode != 0, output(result)
    assert (project / ".claude/skills/alpha/mine.md").read_bytes() == b"Mine.\n"
    assert (project / ".claude/skills/beta/SKILL.md").read_bytes() == b"Edited.\n"
    assert "test/kit" in str(load_yaml(project / "apm.yml")["dependencies"]["apm"])


def test_global_cold_install_and_update(tmp_path, apm_binary_path) -> None:
    """User scope: receipt-less reinstall is a no-op; cold update converges."""
    kit = build_kit(tmp_path, apm_binary_path)
    home = kit.isolated.home
    result = kit.run("install", "-g", "--no-policy", "test/kit#^1.0.0")
    _assert_ok(result)
    user_lock = kit.isolated.config_root / "apm.lock.yaml"
    lock = user_lock.read_bytes()
    skills = home / ".agents/skills"
    deployed = tree(skills)
    assert "alpha/SKILL.md" in deployed

    robust_rmtree(kit.isolated.config_root / "apm_modules")
    for receipts in kit.isolated.config_root.glob("deployment-receipts*"):
        robust_rmtree(receipts)
    result = kit.run("install", "-g", "--no-policy")
    _assert_ok(result)
    assert user_lock.read_bytes() == lock
    assert tree(skills) == deployed

    publish_v110(kit)
    for receipts in kit.isolated.config_root.glob("deployment-receipts*"):
        robust_rmtree(receipts)
    result = kit.run("update", "-g", "--yes")
    _assert_ok(result)
    assert "Alpha v2." in (skills / "alpha/SKILL.md").read_text(encoding="utf-8")
    assert not (skills / "gamma").exists()
    assert (skills / "delta/SKILL.md").is_file()
