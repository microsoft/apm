"""Audit classifies skipped user-authored same-name files as unmanaged conflicts (#3682).

Install skips a user file that shares a name with a package primitive ("local
files exist, not managed by APM"). `apm audit --ci` must report it as the
existing `unmanaged-conflict` drift kind with rename guidance -- never as a
managed `modified` edit and never with the `apm export-patch` hint.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration._target_compile_kit import (
    Scenario,
    output,
    write,
    write_package,
    write_project,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

INSTALL = ("install", "--no-policy")
USER_FILES = {
    ".github/prompts/hello.prompt.md": "USER prompt\n",
    ".claude/agents/rev.md": "USER claude agent\n",
    ".claude/commands/hello.md": "USER claude command\n",
}
CASE_ONLY = (".github/agents/REV.agent.md", ".github/agents/rev.agent.md")


def _drift(result) -> dict[str, str]:
    report = json.loads(result.stdout)
    return {item["path"]: item["kind"] for item in report["drift"]["drift"]}


def test_skipped_user_collisions_are_unmanaged_conflicts_until_forced(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    package = write_package(tmp_path / "pkg")
    (package / ".apm/skills").rename(tmp_path / "skills-parked")
    project = write_project(tmp_path / "proj", ("copilot", "claude"))
    for relative, text in USER_FILES.items():
        write(project / relative, text)
    write(project / CASE_ONLY[0], "USER case-only agent\n")
    case_insensitive = (project / CASE_ONLY[1]).exists()

    installed = scenario.run(*INSTALL, cwd=project)
    assert "local files exist" in output(installed)

    audit = scenario.run("audit", "--ci", "--no-policy", cwd=project, expect=1)
    text = output(audit)
    assert "local edit(s) to managed files" not in text, text
    assert "export-patch" not in text, text
    assert "Rename the conflicting local file" in text, text

    report = scenario.run("audit", "--ci", "--no-policy", "--format", "json", cwd=project, expect=1)
    kinds = _drift(report)
    for relative in USER_FILES:
        assert kinds.get(relative) == "unmanaged-conflict", kinds
    if case_insensitive:
        assert kinds.get(CASE_ONLY[1]) == "unmanaged-conflict", kinds
    assert "modified" not in kinds.values(), kinds

    exported = scenario.run("export-patch", "-o", str(tmp_path / "out"), cwd=project)
    assert not list((tmp_path / "out").glob("*.patch")), output(exported)
    for relative, text_bytes in USER_FILES.items():
        assert (project / relative).read_text(encoding="utf-8") == text_bytes

    # Resolution: rename the case-only file, overwrite the rest with --force.
    (project / CASE_ONLY[0]).rename(project / ".github/agents/mine.agent.md")
    scenario.run(*INSTALL, "--force", cwd=project)
    scenario.run("audit", "--ci", "--no-policy", cwd=project)


def test_edited_managed_file_still_reports_modified(tmp_path: Path, apm_binary_path: Path) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    write_package(tmp_path / "pkg")
    project = write_project(tmp_path / "proj", ("copilot",))
    scenario.run(*INSTALL, cwd=project)
    prompt = project / ".github/prompts/hello.prompt.md"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "local edit\n", encoding="utf-8")

    report = scenario.run("audit", "--ci", "--no-policy", "--format", "json", cwd=project, expect=1)
    assert _drift(report) == {".github/prompts/hello.prompt.md": "modified"}
