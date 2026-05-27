"""Regression tests for #1499 -- global hook integration silently broken.

When a hook JSON file uses the "naked" Claude format with event names
as top-level keys (no outer ``"hooks":`` wrap), the merge silently
produced ``{"hooks": {}}`` for claude/cursor and skipped path rewriting
for copilot, while still logging ``1 hook(s) integrated`` to the user.

The bug-reported file shape:

    {"Stop": [{"matcher": "", "hooks": [{"type": "command", ...}]}]}

is the literal hooks-slice that Claude Code accepts inside its own
``settings.json``.  APM must therefore accept it as a top-level hook
file too, or else fail loudly -- silent success is the worst outcome.

These tests assert the post-fix behaviour:
- claude / cursor merge produces non-empty hook entries on disk.
- copilot writes a file with ``${PLUGIN_ROOT}`` expanded to a real path.
- The integration counter (``files_integrated``) only counts hook files
  that actually contributed entries, so the user-facing "N hook(s)
  integrated" line cannot lie about empty merges.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.models.apm_package import APMPackage, PackageInfo


def _make_package_info(install_path: Path, name: str = "test-pkg") -> PackageInfo:
    package = APMPackage(name=name, version="1.0.0")
    return PackageInfo(package=package, install_path=install_path)


# Naked Claude-format hook file (no outer "hooks" wrap) -- exact shape
# from the #1499 repro.  Top-level keys are event names.
NAKED_STOP_HOOK = {
    "Stop": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "python3 ${PLUGIN_ROOT}/scripts/example.py",
                    "timeout": 20000,
                }
            ],
        }
    ]
}


def _build_naked_hook_package(pkg_dir: Path) -> PackageInfo:
    """Materialise the #1499 repro package layout under *pkg_dir*."""
    hooks_dir = pkg_dir / ".apm" / "hooks"
    scripts_dir = pkg_dir / "scripts"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "session-metrics.json").write_text(json.dumps(NAKED_STOP_HOOK))
    (scripts_dir / "example.py").write_text("print('hi')\n")
    return _make_package_info(pkg_dir, "test-pkg")


def test_claude_merges_naked_hook_format(tmp_path: Path) -> None:
    """Claude settings.json must contain the Stop entry, not ``{"hooks": {}}``.

    Before the fix the file landed as ``{"hooks": {}}`` even though the
    user-facing log said "1 hook(s) integrated".  This is the live repro
    from #1499.
    """
    pkg_info = _build_naked_hook_package(tmp_path / "pkg")
    project_root = tmp_path / "project"
    (project_root / ".claude").mkdir(parents=True)

    integrator = HookIntegrator()
    result = integrator.integrate_package_hooks_claude(pkg_info, project_root)

    settings_path = project_root / ".claude" / "settings.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    assert settings.get("hooks", {}), (
        f"Claude settings.json hooks must be non-empty for #1499 repro; got {settings!r}"
    )
    assert "Stop" in settings["hooks"], (
        f"Stop event must be merged into settings.json; got {settings['hooks']!r}"
    )
    assert result.files_integrated == 1


def test_cursor_merges_naked_hook_format(tmp_path: Path) -> None:
    """Cursor hooks.json must contain the Stop entry, not ``{"hooks": {}}``."""
    pkg_info = _build_naked_hook_package(tmp_path / "pkg")
    project_root = tmp_path / "project"
    (project_root / ".cursor").mkdir(parents=True)

    integrator = HookIntegrator()
    result = integrator.integrate_package_hooks_cursor(pkg_info, project_root)

    hooks_path = project_root / ".cursor" / "hooks.json"
    assert hooks_path.exists()
    data = json.loads(hooks_path.read_text())
    assert data.get("hooks", {}), (
        f"Cursor hooks.json hooks must be non-empty for #1499 repro; got {data!r}"
    )
    assert "Stop" in data["hooks"], (
        f"Stop event must be merged into cursor hooks.json; got {data['hooks']!r}"
    )
    assert result.files_integrated == 1


def test_copilot_expands_plugin_root_naked_hook_format(tmp_path: Path) -> None:
    """Copilot user-scope deploy must expand ``${PLUGIN_ROOT}`` in the written file.

    Before the fix, the naked-format file bypassed the rewrite pass and
    was written verbatim with a literal ``${PLUGIN_ROOT}`` -- which the
    target cannot resolve at runtime.
    """
    pkg_info = _build_naked_hook_package(tmp_path / "pkg")
    project_root = tmp_path / "project"
    (project_root / ".github").mkdir(parents=True)

    integrator = HookIntegrator()
    result = integrator.integrate_package_hooks(pkg_info, project_root)

    written = list((project_root / ".github" / "hooks").glob("*.json"))
    assert len(written) == 1, f"Expected exactly one hook file written; got {written!r}"
    raw = written[0].read_text()
    assert "${PLUGIN_ROOT}" not in raw, (
        f"Copilot hook file must not contain literal ${{PLUGIN_ROOT}}; got: {raw}"
    )
    assert result.files_integrated == 1


def test_counter_does_not_lie_on_empty_merge(tmp_path: Path) -> None:
    """A hook file that contributes zero entries must not bump the counter.

    Regression trap: if the file format is unparseable or contains no
    event entries the user-facing "N hook(s) integrated" line must
    report 0, not 1.  Without this gate the log claims success on a
    write that did nothing -- the original #1499 symptom.
    """
    pkg_dir = tmp_path / "pkg"
    hooks_dir = pkg_dir / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    # Truly empty hook file (no events at all).  Must NOT count as 1.
    (hooks_dir / "empty.json").write_text(json.dumps({"hooks": {}}))
    pkg_info = _make_package_info(pkg_dir, "empty-pkg")

    project_root = tmp_path / "project"
    (project_root / ".claude").mkdir(parents=True)

    integrator = HookIntegrator()
    result = integrator.integrate_package_hooks_claude(pkg_info, project_root)

    assert result.files_integrated == 0, (
        "files_integrated must reflect actual merge contributions; "
        f"got {result.files_integrated} for an empty hook file"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])


def test_malformed_hooks_list_value_does_not_crash(tmp_path: Path) -> None:
    """A hook file with ``{"hooks": []}`` (list instead of dict) must fail closed.

    Regression trap for the Copilot review on #1516: previously
    ``_parse_hook_json`` returned the raw dict and downstream
    ``_rewrite_hooks_data`` / ``_integrate_merged_hooks`` called
    ``.items()`` on the list, raising AttributeError mid-merge.
    The parser must now treat the file as invalid (return None),
    the integration must not crash, and the counter must report 0.
    """
    pkg_dir = tmp_path / "pkg"
    hooks_dir = pkg_dir / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "malformed.json").write_text(json.dumps({"hooks": []}))
    pkg_info = _make_package_info(pkg_dir, "malformed-pkg")

    project_root = tmp_path / "project"
    (project_root / ".claude").mkdir(parents=True)

    integrator = HookIntegrator()
    # Must not raise AttributeError.
    result = integrator.integrate_package_hooks_claude(pkg_info, project_root)

    assert result.files_integrated == 0, (
        "Malformed hook file must be skipped, not counted as integrated; "
        f"got files_integrated={result.files_integrated}"
    )
    # Direct parser contract: malformed shape returns None.
    assert integrator._parse_hook_json(hooks_dir / "malformed.json") is None
