"""``apm view -g --file`` reports user-scope provenance for every target root.

``apm install -g`` records user deployments in ``~/.apm/apm.lock.yaml``. The
#3608 contract is that ``apm view --file PATH -g`` reads that lockfile and
reports the exact recorded owner of any deployed file -- not only files under
``~/.copilot/`` -- including when HOME and the file names contain spaces and
non-ASCII characters. The lookup must never write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._path_corner_world_kit import (
    CornerWorld,
    output,
    publish_corner_packages,
    remote_url,
    tree_bytes,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

OWNER = "git.example.invalid/acme/uni"
USER_FILES = (
    ".claude/skills/caf-skill/notes \u00e9 \u00fc.md",
    ".claude/agents/r\u00e9viewer.md",
    ".cursor/rules/caf\u00e9.mdc",
    ".agents/skills/caf-skill/SKILL.md",
    ".copilot/instructions/my notes.instructions.md",
)


def test_view_file_global_reports_owner_for_every_user_target(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = CornerWorld.create(tmp_path, apm_binary_path)
    publish_corner_packages(world)
    home = world.home
    assert " " in str(home) and not str(home).isascii()
    for marker in (".claude", ".cursor", ".copilot"):
        (home / marker).mkdir(parents=True, exist_ok=True)
    elsewhere = world.isolated.work_root

    world.apm(elsewhere, "install", "-g", "--no-policy", remote_url("uni"))
    deployed = world.lock(home / ".apm")["dependencies"][0]["deployed_files"]
    for rel in USER_FILES:
        assert rel in deployed, deployed
        assert (home / rel).is_file(), rel

    before = tree_bytes(home)
    for rel in USER_FILES:
        absolute = world.apm(elsewhere, "view", "-g", "--file", str(home / rel), expect=None)
        assert absolute.returncode == 0, output(absolute)
        text = output(absolute)
        assert f"Owner: {OWNER} [active writer]" in text, text
        assert "matches the recorded deployed hash" in text, text
        assert "(user scope" in text, text

        relative = world.apm(home, "view", "-g", "--file", rel, expect=None)
        assert relative.returncode == 0, output(relative)
        assert f"Owner: {OWNER} [active writer]" in output(relative)
    assert tree_bytes(home) == before

    edited = home / USER_FILES[2]
    original = edited.read_bytes()
    edited.write_bytes(original + b"user edit\n")
    modified = world.apm(elsewhere, "view", "-g", "--file", str(edited), expect=None)
    assert modified.returncode == 0, output(modified)
    assert "modified since deployment" in output(modified)
    edited.write_bytes(original)

    world.apm(elsewhere, "uninstall", "-g", remote_url("uni"))
    for rel in USER_FILES:
        assert not (home / rel).exists(), rel
    gone = world.apm(elsewhere, "view", "-g", "--file", str(home / USER_FILES[0]), expect=None)
    assert gone.returncode != 0, output(gone)
    assert OWNER not in output(gone)
