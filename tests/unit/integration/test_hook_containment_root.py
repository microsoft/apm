"""The containment-root decision for Claude hook bundles (#4772)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.integration.hook_bundle import hook_containment_root

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I5")]


@pytest.mark.parametrize(
    ("name", "root", "contained_by_root"),
    [
        ("claude", "outside", True),
        ("claude", None, False),
        ("claude", "/", False),
        ("claude", "home-parent", False),
        ("codex", "outside", False),
    ],
    ids=["claude-outside-home", "claude-default", "filesystem-root", "home-parent", "other"],
)
def test_containment_root_is_an_absolute_claude_root_outside_home_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    root: str | None,
    contained_by_root: bool,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    roots = {"outside": tmp_path / "claude-profile", "/": Path("/"), "home-parent": tmp_path}
    target = SimpleNamespace(name=name, managed_deploy_root=roots.get(root) if root else None)

    expected = target.managed_deploy_root if contained_by_root else home
    assert hook_containment_root(target, home) == expected


@pytest.mark.parametrize(
    ("user_key", "swapped"),
    [(False, False), (True, False), (False, True)],
    ids=["apm-created", "user-file", "profile-swapped-for-a-symlink"],
)
def test_retiring_hooks_under_an_outside_home_claude_root_deletes_an_emptied_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_key: bool, swapped: bool
) -> None:
    """Retirement checks symlinks from the Claude root, not HOME (#4945).

    A profile redirected through a symlink is only rewritten, never deleted from.
    """
    import json
    from dataclasses import replace

    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.integration.hook_ownership import local_bundle_hook_source_marker
    from apm_cli.integration.targets import KNOWN_TARGETS

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    profile = tmp_path / "claude-profile"
    if swapped:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        profile.symlink_to(elsewhere, target_is_directory=True)
    else:
        profile.mkdir()
    target = replace(KNOWN_TARGETS["claude"], root_dir=str(profile))
    entry = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "echo guard"}],
        "_apm_source": local_bundle_hook_source_marker("bun"),
    }
    data: dict = {"hooks": {"PreToolUse": [entry]}}
    if user_key:
        data["theme"] = "dark"
    settings = profile / "settings.json"
    settings.write_text(json.dumps(data), encoding="utf-8")

    stats = HookIntegrator().retire_local_bundle_hooks("bun", home, [target])

    assert stats["errors"] == 0
    if user_key:
        assert json.loads(settings.read_text(encoding="utf-8")) == {"theme": "dark"}
    elif swapped:
        assert json.loads((tmp_path / "elsewhere" / "settings.json").read_text("utf-8")) == {}
    else:
        assert not settings.exists(), settings.read_text(encoding="utf-8")
