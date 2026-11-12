"""Project, HOME and package file names with spaces and non-ASCII letters.

One consumer under ``iso w\\u00f6rld \\u00e9/work/my pr\\u00f8ject \\u00e9`` installs
a whole-repo package whose primitive file names contain spaces and accents,
a monorepo subdirectory package at ``packages/t\\u00f6\\u00f6l kit`` and a local
directory package at ``../local pkg \\u00fc`` for five targets. The chain checks
that install is idempotent and frozen-replayable, ``audit --ci`` is clean,
read-only commands never write, and ``export-patch`` emits patches that
``git apply`` from the package repository root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._path_corner_world_kit import (
    CornerWorld,
    publish_corner_packages,
    remote_url,
    tree_bytes,
    write,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

TARGETS = ("claude", "copilot", "cursor", "codex", "opencode")
EDITED = (
    ".claude/skills/caf-skill/notes \u00e9 \u00fc.md",
    ".claude/skills/tool-skill/SKILL.md",
)


def test_spaced_unicode_paths_install_audit_export_chain(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = CornerWorld.create(tmp_path, apm_binary_path)
    commits = publish_corner_packages(world)
    local = world.isolated.work_root / "local pkg \u00fc"
    write(local / "apm.yml", "name: local-pkg\nversion: 1.0.0\ndescription: l\n")
    write(local / ".apm/instructions/l\u00f6 cal.instructions.md", "---\napplyTo: '**'\n---\nl\n")
    project = world.project(
        "my pr\u00f8ject \u00e9",
        targets=TARGETS,
        deps=(
            remote_url("uni"),
            {"git": remote_url("mono"), "path": "packages/t\u00f6\u00f6l kit", "ref": "main"},
            {"path": "../local pkg \u00fc"},
        ),
    )
    for marker in (".claude", ".github", ".cursor", ".codex", ".opencode"):
        (project / marker).mkdir()

    world.apm(project, "install", "--no-policy")
    for rel in (
        *EDITED,
        ".claude/skills/tool-skill/LICENSE",
        ".github/instructions/my notes.instructions.md",
        ".cursor/rules/caf\u00e9.mdc",
        ".claude/rules/l\u00f6 cal.md",
        ".codex/agents/r\u00e9viewer.toml",
        ".opencode/commands/do thing.md",
    ):
        assert (project / rel).is_file(), rel
    world.apm(project, "audit", "--ci", "--no-policy")

    installed = tree_bytes(project)
    world.apm(project, "install", "--no-policy")
    world.apm(project, "install", "--frozen", "--no-policy")
    assert tree_bytes(project) == installed

    for rel in EDITED:
        target = project / rel
        target.write_bytes(target.read_bytes() + b"local edit\n")
    edited = tree_bytes(project)
    home_before = tree_bytes(world.home)
    for args in (
        ("deps", "list"),
        ("deps", "tree"),
        ("view", "--file", EDITED[0]),
        ("export-patch", "--dry-run"),
    ):
        world.apm(project, *args)
    world.apm(project, "audit", "--ci", "--no-policy", expect=1)
    assert tree_bytes(project) == edited
    assert tree_bytes(world.home) == home_before

    out = world.isolated.work_root / "patches out \u00e9"
    world.apm(project, "export-patch", "-o", str(out))
    assert tree_bytes(project) == edited
    patches = {path.name: path for path in out.glob("*.patch")}
    assert len(patches) == 2, sorted(patches)
    for name, repo in (("uni", "uni"), ("mono", "mono")):
        patch = next(path for key, path in patches.items() if f"acme__{name}" in key)
        clone = world.isolated.work_root / f"apply {name}"
        world.git(world.isolated.work_root, "clone", "-q", remote_url(repo), str(clone))
        world.git(clone, "checkout", "-q", commits[repo])
        applied = world.git(clone, "apply", "--check", str(patch), check=False)
        assert applied.returncode == 0, (applied.stderr, patch.read_text(encoding="utf-8"))
    assert all("+local edit" in path.read_text(encoding="utf-8") for path in patches.values())
