"""Regression checks for generated workflow action pins and Triage Panel metadata."""

import json
import re
from pathlib import Path

import pytest
import yaml

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


def test_triage_source_and_compiled_writers_are_advisory_only() -> None:
    """Check real agent and privileged handler allowlists, not prompt promises."""
    source = LOCK_PATH.with_name("triage-panel.md").read_text(encoding="ascii")
    frontmatter = yaml.safe_load(source.split("---", 2)[1])
    lock = yaml.safe_load(LOCK_PATH.read_text(encoding="utf-8"))
    contract = json.loads(
        (REPO_ROOT / "packages/apm-triage-panel/assets/label-contract.json").read_text()
    )
    expected_add = set(contract["classification_labels"]) | {
        contract["processing"]["active_write_reviewed"]
    }
    outputs = frontmatter["safe-outputs"]
    assert set(outputs) == {"add-comment", "add-labels", "remove-labels"}
    assert set(outputs["add-labels"]["allowed"]) == expected_add
    assert outputs["add-labels"]["issue-intent"] is False
    assert outputs["remove-labels"]["allowed"] == contract["processing"]["removable"]
    assert set(frontmatter["on" if "on" in frontmatter else True]["labels"]) == set(
        contract["processing"]["request_triggers"]
    )
    assert frontmatter["concurrency"]["group"] == "triage-panel"
    assert set(frontmatter["permissions"].values()) == {"read"}

    configs = {}
    for job in lock["jobs"].values():
        for step in job.get("steps", []):
            for key, value in step.get("env", {}).items():
                if key in {"GH_AW_SAFE_OUTPUTS_CONFIG", "GH_AW_SAFE_OUTPUTS_HANDLER_CONFIG"}:
                    configs[key] = json.loads(value)
    assert set(configs) == {"GH_AW_SAFE_OUTPUTS_CONFIG", "GH_AW_SAFE_OUTPUTS_HANDLER_CONFIG"}
    forbidden = {
        "assign_milestone",
        "update_issue",
        "create_issue",
        "close_issue",
        "add_assignee",
        "update_comment",
    }
    for config in configs.values():
        assert not forbidden & config.keys()
        assert set(config["add_labels"]["allowed"]) == expected_add
        assert config["add_labels"]["issue_intent"] is False
        assert config["remove_labels"]["allowed"] == ["triage/requested"]
        assert "status/needs-triage" not in config["remove_labels"]["allowed"]
        assert not set(contract["human_decisions"]) & set(config["add_labels"]["allowed"])
        assert not {"help wanted", "good first issue", "priority/high", "priority/low"} & set(
            config["add_labels"]["allowed"]
        )
    manifest = _load_lock_header(LOCK_PATH.read_text(), "# gh-aw-manifest: ")
    servers = {server["name"]: server["tools"] for server in manifest["mcp_servers"]}
    assert {"list_label", "get_label"} <= set(servers["github"])
    assert "label_write" not in servers["github"]
    assert not forbidden & set(servers["safeoutputs"])
