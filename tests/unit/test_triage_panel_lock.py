"""Regression checks for the generated Triage Panel workflow."""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = REPO_ROOT / ".github" / "workflows" / "triage-panel.lock.yml"
ACTIONS_LOCK_PATH = REPO_ROOT / ".github" / "aw" / "actions-lock.json"


def test_triage_panel_lock_manifest_matches_runtime_setup_pin() -> None:
    """Keep the runtime setup action aligned with the compiled manifest."""
    lock_text = LOCK_PATH.read_text(encoding="utf-8")
    manifest_prefix = "# gh-aw-manifest: "
    manifest_lines = [line for line in lock_text.splitlines() if line.startswith(manifest_prefix)]
    assert len(manifest_lines) == 1
    manifest_line = manifest_lines[0]
    manifest = json.loads(manifest_line.removeprefix(manifest_prefix))
    manifest_setups = [
        action for action in manifest["actions"] if action["repo"] == "github/gh-aw-actions/setup"
    ]
    assert len(manifest_setups) == 1
    manifest_setup = manifest_setups[0]

    actions_lock = json.loads(ACTIONS_LOCK_PATH.read_text(encoding="utf-8"))
    canonical_setups = [
        action
        for action in actions_lock["entries"].values()
        if action["repo"] == "github/gh-aw-actions/setup"
    ]
    assert len(canonical_setups) == 1
    canonical_setup = canonical_setups[0]
    assert manifest_setup["version"] == canonical_setup["version"]
    assert manifest_setup["sha"] == canonical_setup["sha"]

    runtime_setup_refs = set(
        re.findall(
            r"^\s+uses: github/gh-aw-actions/setup@([^\s#]+)(?: # v[0-9.]+)?$",
            lock_text,
            re.MULTILINE,
        )
    )
    assert runtime_setup_refs == {manifest_setup["sha"]}
