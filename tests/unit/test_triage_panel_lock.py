"""Regression checks for the generated Triage Panel workflow."""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = REPO_ROOT / ".github" / "workflows" / "triage-panel.lock.yml"
ACTIONS_LOCK_PATH = REPO_ROOT / ".github" / "aw" / "actions-lock.json"


def _load_lock_header(lock_text: str, prefix: str) -> dict:
    """Load exactly one JSON header from the generated workflow lock."""
    matching_lines = [line for line in lock_text.splitlines() if line.startswith(prefix)]
    assert len(matching_lines) == 1
    return json.loads(matching_lines[0].removeprefix(prefix))


def test_triage_panel_lock_manifest_matches_runtime_setup_pin() -> None:
    """Keep the runtime setup action aligned with the compiled manifest."""
    lock_text = LOCK_PATH.read_text(encoding="utf-8")
    manifest = _load_lock_header(lock_text, "# gh-aw-manifest: ")
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
            r"^\s+uses:\s*github/gh-aw-actions/setup@([^\s#]+)",
            lock_text,
            re.MULTILINE,
        )
    )
    assert runtime_setup_refs == {manifest_setup["sha"]}


def test_triage_panel_lock_pins_copilot_cli_version() -> None:
    """Keep the compiled Copilot CLI installation deterministic."""
    lock_text = LOCK_PATH.read_text(encoding="utf-8")
    metadata = _load_lock_header(lock_text, "# gh-aw-metadata: ")
    copilot_version = metadata["engine_versions"]["copilot"]

    installed_versions = set(
        re.findall(
            r'install_copilot_cli\.sh"[ \t]+([^ \t\r\n]+)',
            lock_text,
        )
    )
    assert installed_versions == {copilot_version}
