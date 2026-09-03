"""Regression checks for the generated Triage Panel workflow."""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = REPO_ROOT / ".github" / "workflows" / "triage-panel.lock.yml"


def test_triage_panel_lock_manifest_matches_runtime_setup_pin() -> None:
    """Keep the runtime setup action aligned with the compiled manifest."""
    lock_text = LOCK_PATH.read_text(encoding="utf-8")
    manifest_prefix = "# gh-aw-manifest: "
    manifest_line = lock_text.splitlines()[1]
    assert manifest_line.startswith(manifest_prefix)
    manifest = json.loads(manifest_line.removeprefix(manifest_prefix))
    manifest_setup = next(
        action for action in manifest["actions"] if action["repo"] == "github/gh-aw-actions/setup"
    )

    runtime_setup_shas = set(
        re.findall(
            r"^\s+uses: github/gh-aw-actions/setup@([0-9a-f]+)(?: # v[0-9.]+)?$",
            lock_text,
            re.MULTILINE,
        )
    )
    assert runtime_setup_shas == {manifest_setup["sha"]}
