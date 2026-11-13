"""Multi-target install, hand edits, compile, audit, narrowing and uninstall agree (#3649).

Every step asserts exit code, files on disk and ``apm.lock.yaml``. User-authored
neighbours in target directories must survive every step.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._target_compile_kit import (
    ALL_TARGETS,
    Scenario,
    deployed_files,
    output,
    tree_digest,
    write,
    write_package,
    write_project,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_USER_FILES = {
    ".cursor/rules/mine.mdc": "---\nalwaysApply: true\n---\nMine.\n",
    ".claude/rules/mine.md": "Mine.\n",
    ".kiro/steering/mine.md": "Mine.\n",
    ".agents/rules/mine.md": "Mine.\n",
    ".github/instructions/mine.instructions.md": "Mine.\n",
}


def _assert_user_files(project: Path) -> None:
    for relative, text in _USER_FILES.items():
        assert (project / relative).read_text(encoding="ascii") == text, relative


def test_multi_target_edit_compile_audit_narrow_uninstall_chain(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    write_package(scenario.isolated.work_root / "pkg")
    project = write_project(scenario.isolated.work_root / "proj", ALL_TARGETS)
    for relative, text in _USER_FILES.items():
        write(project / relative, text)

    # 1. install deploys native files per target and records them all.
    scenario.run("install", "--no-policy", cwd=project)
    deployed = deployed_files(project)
    for expected in (
        ".cursor/rules/global.mdc",
        ".agents/rules/global.md",
        ".kiro/steering/global.md",
        ".codex/agents/rev.toml",
        ".github/instructions/global.instructions.md",
    ):
        assert expected in deployed, deployed
    assert "alwaysApply: true" in (project / ".cursor/rules/global.mdc").read_text("ascii")
    assert "globs: src/**/*.py" in (project / ".cursor/rules/py.mdc").read_text("ascii")
    assert not any(path.endswith("mine.mdc") or "/mine." in path for path in deployed)
    _assert_user_files(project)

    # 2. re-run is a byte-level no-op, lockfile included.
    lock_bytes = (project / "apm.lock.yaml").read_bytes()
    tree = tree_digest(project)
    scenario.run("install", "--no-policy", cwd=project)
    assert (project / "apm.lock.yaml").read_bytes() == lock_bytes
    assert tree_digest(project) == tree

    # 3. hand-edit one rule and one agent: audit --ci fails and names both files.
    edited_rule = project / ".kiro/steering/py.md"
    edited_agent = project / ".codex/agents/rev.toml"
    edited_rule.write_text(edited_rule.read_text("ascii") + "EDIT\n", encoding="ascii")
    edited_agent.write_text(edited_agent.read_text("ascii") + "# EDIT\n", encoding="ascii")
    audit = scenario.run("audit", "--ci", "--no-policy", cwd=project, expect=1)
    text = output(audit)
    assert ".kiro/steering/py.md" in text and ".codex/agents/rev.toml" in text, text
    assert ".cursor/rules/py.mdc" not in text
    assert (project / "apm.lock.yaml").read_bytes() == lock_bytes

    # 4. compile is deterministic and leaves the lockfile alone.
    scenario.run("compile", cwd=project)
    compiled = {name: (project / name).read_bytes() for name in ("AGENTS.md", "GEMINI.md")}
    scenario.run("compile", cwd=project)
    for name, data in compiled.items():
        assert (project / name).read_bytes() == data, name
    assert (project / "apm.lock.yaml").read_bytes() == lock_bytes

    # 5. documented: plain reinstall re-deploys edited managed files from the cache.
    scenario.run("install", "--no-policy", cwd=project)
    assert "EDIT" not in edited_rule.read_text("ascii")
    assert "EDIT" not in edited_agent.read_text("ascii")
    assert (project / "apm.lock.yaml").read_bytes() == lock_bytes
    scenario.run("audit", "--ci", "--no-policy", cwd=project)
    _assert_user_files(project)

    # 6. narrow targets with one edited cursor rule: unchanged output of dropped
    #    targets is removed, the edited file is kept and stays tracked for audit.
    edited_cursor = project / ".cursor/rules/py.mdc"
    edited_cursor.write_text(edited_cursor.read_text("ascii") + "EDIT\n", encoding="ascii")
    manifest = (project / "apm.yml").read_text("ascii")
    write(
        project / "apm.yml",
        manifest.replace(f"targets: [{', '.join(ALL_TARGETS)}]", "targets: [copilot, claude]"),
    )
    narrowed = scenario.run("install", "--no-policy", cwd=project)
    assert ".cursor/rules/py.mdc" in output(narrowed)
    assert edited_cursor.read_text("ascii").endswith("EDIT\n")
    for gone in (
        ".cursor/rules/global.mdc",
        ".kiro/steering/global.md",
        ".agents/rules/global.md",
        ".codex/agents/rev.toml",
        ".opencode/agents/rev.md",
    ):
        assert not (project / gone).exists(), gone
    remaining = deployed_files(project)
    assert ".cursor/rules/py.mdc" in remaining
    assert not any(path.startswith((".kiro/", ".codex/", ".opencode/")) for path in remaining)
    _assert_user_files(project)
    audit = scenario.run("audit", "--ci", "--no-policy", cwd=project, expect=1)
    assert ".cursor/rules/py.mdc" in output(audit)

    # 7. uninstall fails closed on the edited file and names it; user files survive.
    failed = scenario.run("uninstall", "../pkg", cwd=project, expect=1)
    assert ".cursor/rules/py.mdc" in output(failed)
    assert edited_cursor.exists()
    _assert_user_files(project)

    # 8. after the user resolves the edit, uninstall completes and removes only APM files.
    edited_cursor.unlink()
    scenario.run("uninstall", "../pkg", cwd=project)
    for path in (".github/agents/rev.agent.md", ".claude/rules/global.md"):
        assert not (project / path).exists(), path
    assert "../pkg" not in (project / "apm.yml").read_text("ascii")
    _assert_user_files(project)
