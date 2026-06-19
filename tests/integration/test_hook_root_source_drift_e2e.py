"""End-to-end regression for #1329: stale root hook _apm_source heals on reinstall.

Parametrized across every harness whose hooks live in a merged config file
(Claude, Codex, Cursor, Gemini, Windsurf). Copilot is intentionally excluded:
its hooks live in per-file namespaces under ``.github/hooks/`` and are not
subject to the merged-config drift this fix targets.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CLI = [sys.executable, "-m", "apm_cli.cli"]


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(CLI + list(args), cwd=str(cwd), capture_output=True, text=True)


# Per-target on-disk layout descriptors:
#   - settings_rel: path (relative to project root) of the merged hook config
#   - sidecar_rel:  path of the apm-hooks.json sidecar (schema-strict targets only)
#   - event_key:    event key under which PreToolUse entries land for the target
#
# Sidecar-bearing targets (currently Claude) keep _apm_source in the sidecar
# rather than in settings.json; the heal assertion reads from there.
_HARNESS_CASES = [
    pytest.param(
        "claude",
        ".claude/settings.json",
        ".claude/apm-hooks.json",
        "PreToolUse",
        id="claude",
    ),
    pytest.param(
        "codex",
        ".codex/hooks.json",
        None,
        "PreToolUse",
        id="codex",
    ),
    pytest.param(
        "cursor",
        ".cursor/hooks.json",
        None,
        "PreToolUse",
        id="cursor",
    ),
    pytest.param(
        "gemini",
        ".gemini/settings.json",
        None,
        "BeforeTool",
        id="gemini",
    ),
    pytest.param(
        "windsurf",
        ".windsurf/hooks.json",
        None,
        "PreToolUse",
        id="windsurf",
    ),
]


def _load_sources(
    project: Path,
    settings_rel: str,
    sidecar_rel: str | None,
    event_key: str,
) -> list[str]:
    """Return the list of _apm_source markers for the target's PreToolUse-equivalent.

    Entries lacking ``_apm_source`` (user-owned hooks) are excluded so the
    heal assertion can compare just the APM-owned slice.
    """
    if sidecar_rel:
        sidecar = json.loads((project / sidecar_rel).read_text(encoding="utf-8"))
        return [
            e["_apm_source"]
            for e in sidecar.get(event_key, [])
            if isinstance(e, dict) and e.get("_apm_source")
        ]
    settings = json.loads((project / settings_rel).read_text(encoding="utf-8"))
    return [
        e["_apm_source"]
        for e in settings.get("hooks", {}).get(event_key, [])
        if isinstance(e, dict) and e.get("_apm_source")
    ]


def _rewrite_source(
    project: Path,
    settings_rel: str,
    sidecar_rel: str | None,
    event_key: str,
    *,
    old: str,
    new: str,
) -> None:
    """Mutate _apm_source markers in-place to simulate a stale checkout basename."""
    target_rel = sidecar_rel or settings_rel
    target = project / target_rel
    data = json.loads(target.read_text(encoding="utf-8"))
    container = data if sidecar_rel else data.get("hooks", {})
    entries = container.get(event_key, [])
    for entry in entries:
        if isinstance(entry, dict) and entry.get("_apm_source") == old:
            entry["_apm_source"] = new
    target.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("target, settings_rel, sidecar_rel, event_key", _HARNESS_CASES)
def test_root_hook_source_drift_heals_on_reinstall(
    tmp_path: Path,
    target: str,
    settings_rel: str,
    sidecar_rel: str | None,
    event_key: str,
) -> None:
    """After a checkout rename, a second `apm install` heals stale source markers.

    Strategy: install once cleanly so the integrator writes target-shaped entries
    with `_local/myapp`; then rewrite the marker to simulate an old checkout
    basename and append a user-owned hook; then install again and assert the
    stale marker is gone, the user-owned hook survives, and exactly one APM
    entry with `_local/myapp` remains.
    """
    project = tmp_path / "myapp"
    project.mkdir()
    (project / "apm.yml").write_text(
        f"name: myapp\nversion: 0.0.0\ntargets:\n  - {target}\n",
        encoding="utf-8",
    )
    hooks_dir = project / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "pre.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo apm-managed"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    # First install: produces target-shaped entries marked `_local/myapp`.
    first = _run(project, "install")
    assert first.returncode == 0, first.stderr or first.stdout
    assert _load_sources(project, settings_rel, sidecar_rel, event_key) == ["_local/myapp"], (
        "First install must produce a single _local/myapp entry; "
        f"got {_load_sources(project, settings_rel, sidecar_rel, event_key)}"
    )

    # Simulate a legacy install whose _apm_source came from an old checkout basename.
    _rewrite_source(
        project,
        settings_rel,
        sidecar_rel,
        event_key,
        old="_local/myapp",
        new="old-checkout-name",
    )

    # Append a user-owned entry directly to the settings file (never in the sidecar).
    settings_path = project / settings_rel
    settings_data = json.loads(settings_path.read_text(encoding="utf-8"))
    settings_data.setdefault("hooks", {}).setdefault(event_key, []).append(
        {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "echo user-owned"}],
        }
    )
    settings_path.write_text(json.dumps(settings_data), encoding="utf-8")

    # Second install: must heal the stale marker without touching the user-owned entry.
    second = _run(project, "install")
    assert second.returncode == 0, second.stderr or second.stdout

    sources = _load_sources(project, settings_rel, sidecar_rel, event_key)
    assert sources == ["_local/myapp"], (
        f"Expected single _local/myapp entry after heal for {target}, got {sources}"
    )

    settings_data = json.loads(settings_path.read_text(encoding="utf-8"))
    entries = settings_data.get("hooks", {}).get(event_key, [])
    user_owned = [
        e
        for e in entries
        if isinstance(e, dict)
        and isinstance(e.get("hooks"), list)
        and e["hooks"]
        and isinstance(e["hooks"][0], dict)
        and e["hooks"][0].get("command") == "echo user-owned"
    ]
    assert len(user_owned) == 1, (
        f"User-owned hook entry must survive healing for {target}; entries={entries}"
    )
