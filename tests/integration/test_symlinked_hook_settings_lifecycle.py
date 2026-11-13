"""Hook merges never read or write a project settings file through a symlink.

When ``.claude/settings.json`` is a symlink, ``apm install`` refuses to merge
hook entries into it: the link stays in place, its target is unchanged, and
no project file receives the target's contents.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration._containment_kit import build_containment_kit, output

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows"),
]

_LINKED = '{"outsideOnly": "value"}\n'


def test_install_refuses_linked_hook_settings(tmp_path: Path, apm_binary_path: Path) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    kit.write_manifest(("claude",))
    linked = kit.outside / "settings.json"
    linked.write_text(_LINKED, encoding="utf-8")
    settings = kit.project / ".claude/settings.json"
    settings.parent.mkdir()
    os.symlink(linked, settings)

    result = kit.run("install", "--no-policy")

    assert result.returncode != 0, output(result)
    assert "symlinked config file" in output(result)
    assert settings.is_symlink()
    assert linked.read_text(encoding="utf-8") == _LINKED
    copied = [
        path
        for path in (kit.project / ".claude").rglob("*")
        if path.is_file() and not path.is_symlink() and "outsideOnly" in path.read_text("utf-8")
    ]
    assert copied == []


def test_install_merges_hooks_into_regular_settings(tmp_path: Path, apm_binary_path: Path) -> None:
    kit = build_containment_kit(tmp_path, apm_binary_path)
    kit.write_manifest(("claude",))
    (kit.project / ".claude").mkdir()

    result = kit.run("install", "--no-policy")

    assert result.returncode == 0, output(result)
    assert "hooks" in (kit.project / ".claude/settings.json").read_text(encoding="utf-8")
