"""Regression checks for generated workflow action pins and Triage Panel metadata."""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = REPO_ROOT / ".github" / "workflows" / "triage-panel.lock.yml"
ACTIONS_LOCK_PATH = REPO_ROOT / ".github" / "aw" / "actions-lock.json"


def _load_lock_header(lock_text: str, prefix: str) -> dict:
    """Load exactly one JSON header from the generated workflow lock."""
    matching_lines = [line for line in lock_text.splitlines() if line.startswith(prefix)]
    assert len(matching_lines) == 1
    return json.loads(matching_lines[0].removeprefix(prefix))


@pytest.mark.parametrize(
    "workflow",
    ["cli-consistency-checker", "daily-doc-updater", "docs-sync", "perf-scan", "triage-panel"],
)
def test_lock_manifest_matches_runtime_action_pins(workflow: str) -> None:
    """Keep updated runtime actions aligned with manifests and the canonical lock."""
    lock_text = LOCK_PATH.with_name(f"{workflow}.lock.yml").read_text(encoding="utf-8")
    manifest = _load_lock_header(lock_text, "# gh-aw-manifest: ")
    actions_lock = json.loads(ACTIONS_LOCK_PATH.read_text(encoding="utf-8"))
    for repo in ("github/gh-aw-actions/setup", "actions/create-github-app-token"):
        runtime_refs = set(
            re.findall(
                rf"^\s+uses:\s*{re.escape(repo)}@([^\s#]+)",
                lock_text,
                re.MULTILINE,
            )
        )
        manifest_actions = [action for action in manifest["actions"] if action["repo"] == repo]
        if repo == "github/gh-aw-actions/setup":
            assert len(manifest_actions) == 1
            assert runtime_refs
        assert runtime_refs == {action["sha"] for action in manifest_actions}
        for action in manifest_actions:
            assert action == actions_lock["entries"][f"{repo}@{action['version']}"]
            assert f"#   - {repo}@{action['sha']} # {action['version']}" in lock_text


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
