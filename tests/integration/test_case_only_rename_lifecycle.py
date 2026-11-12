"""A package that renames primitives only by letter case updates cleanly.

On macOS and Windows the default filesystems are case-insensitive, so the old
spelling (``Guide.instructions.md``) and the new one (``guide.instructions.md``)
are one directory entry. ``apm update`` must deploy the new spelling for every
target, never treat the APM-owned old spelling as a user collision or a user
edit, never delete the file it just wrote, record exactly the new spellings in
``apm.lock.yaml``, and leave ``apm audit --ci`` clean and the next install a
no-op. On case-sensitive filesystems the old spelling is simply removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._path_corner_world_kit import (
    CornerWorld,
    output,
    remote_url,
    write,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

RENAMES = {
    ".apm/instructions/Guide.instructions.md": ".apm/instructions/guide.instructions.md",
    ".apm/agents/Helper.agent.md": ".apm/agents/helper.agent.md",
    ".apm/prompts/Run.prompt.md": ".apm/prompts/run.prompt.md",
}
OLD_DEPLOYED = (
    ".github/instructions/Guide.instructions.md",
    ".claude/rules/Guide.md",
    ".cursor/rules/Guide.mdc",
    ".github/agents/Helper.agent.md",
    ".claude/agents/Helper.md",
    ".github/prompts/Run.prompt.md",
    ".claude/commands/Run.md",
)
NEW_DEPLOYED = tuple(
    path.replace("Guide", "guide").replace("Helper", "helper").replace("Run", "run")
    for path in OLD_DEPLOYED
)


def _entry_names(project: Path, rel: str) -> list[str]:
    return sorted(entry.name for entry in (project / rel).parent.iterdir())


def test_update_after_case_only_rename_keeps_every_new_spelling(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = CornerWorld.create(tmp_path, apm_binary_path)
    source = world.isolated.package_root / "casepkg"
    write(source / "apm.yml", "name: casepkg\nversion: 1.0.0\ndescription: c\n")
    write(source / ".apm/instructions/Guide.instructions.md", "---\napplyTo: '**'\n---\nv1\n")
    write(source / ".apm/agents/Helper.agent.md", "---\nname: helper\ndescription: h\n---\nv1\n")
    write(source / ".apm/prompts/Run.prompt.md", "---\ndescription: p\n---\nv1\n")
    world.publish("casepkg", source)

    project = world.project(
        "case consumer", targets=("claude", "copilot", "cursor"), deps=(remote_url("casepkg"),)
    )
    world.apm(project, "install", "--no-policy")
    for rel in OLD_DEPLOYED:
        assert rel.rsplit("/", 1)[1] in _entry_names(project, rel), rel

    origin = world.origins["casepkg"]
    for old, new in RENAMES.items():
        world.git(origin.worktree, "mv", "-f", old, new)
        target = origin.worktree / new
        target.write_text(target.read_text().replace("v1", "v2"))
    world.repos.commit(origin, message="rename by case")

    update = world.apm(project, "update", "--yes")
    text = output(update)
    assert "user-edited" not in text, text
    assert "local file exists" not in text, text

    for old, new in zip(OLD_DEPLOYED, NEW_DEPLOYED, strict=True):
        names = _entry_names(project, new)
        assert new.rsplit("/", 1)[1] in names, (new, names)
        assert old.rsplit("/", 1)[1] not in names, (old, names)
        assert "v2" in (project / new).read_text(), new

    deployed = world.lock(project)["dependencies"][0]["deployed_files"]
    assert set(NEW_DEPLOYED) <= set(deployed), deployed
    assert not set(OLD_DEPLOYED) & set(deployed), deployed

    world.apm(project, "audit", "--ci", "--no-policy")
    lock_before = (project / "apm.lock.yaml").read_bytes()
    world.apm(project, "install", "--no-policy")
    assert (project / "apm.lock.yaml").read_bytes() == lock_before
