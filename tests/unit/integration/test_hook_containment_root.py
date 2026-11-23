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
