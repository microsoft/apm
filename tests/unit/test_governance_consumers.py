"""Offline contracts for existing automation's human-scope boundaries."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jsonschema import Draft7Validator

from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.utils.content_hash import compute_file_hash

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
BATCH = ROOT / "packages/batch-bug-shepherd/.apm/skills/batch-bug-shepherd"
DOCS = ROOT / ".apm/skills/docs-sync"
AUTOPILOT = ROOT / "packages/apm-issue-autopilot/.apm/skills/apm-issue-autopilot"


def _workflow(name: str) -> tuple[dict, dict, str]:
    """Load source and compiled forms without invoking any remote automation."""
    source = (ROOT / f".github/workflows/{name}.md").read_text()
    lock = yaml.safe_load((ROOT / f".github/workflows/{name}.lock.yml").read_text())
    return yaml.safe_load(source.split("---", 2)[1]), lock, source


def _output_configs(lock: dict) -> dict[str, dict]:
    """Extract both agent-side and privileged handler capability configurations."""
    keys = {"GH_AW_SAFE_OUTPUTS_CONFIG", "GH_AW_SAFE_OUTPUTS_HANDLER_CONFIG"}
    configs = {
        key: json.loads(value)
        for job in lock["jobs"].values()
        for step in job.get("steps", [])
        for key, value in step.get("env", {}).items()
        if key in keys
    }
    assert set(configs) == keys
    return configs


def test_daily_docs_has_no_issue_or_pr_writing_capability_or_privileged_token() -> None:
    """Unattended discovery cannot use a configured implementation write channel."""
    source, lock, text = _workflow("daily-doc-updater")
    outputs = source["safe-outputs"]
    assert set(outputs) == {
        "upload-artifact",
        "noop",
        "missing-tool",
        "missing-data",
        "report-incomplete",
        "report-failure-as-issue",
        "activation-comments",
    }
    for name in (
        "missing-tool",
        "missing-data",
        "report-incomplete",
        "report-failure-as-issue",
        "activation-comments",
    ):
        assert outputs[name] is False
    for config in _output_configs(lock).values():
        assert set(config) == {"noop", "upload_artifact"}
        assert config["noop"]["report-as-issue"] == "false"
        assert config["upload_artifact"]["allowed-paths"] == ["documentation-gaps.md"]
        assert config["upload_artifact"]["max-uploads"] == 1
        assert config["upload_artifact"]["max-size-bytes"] == 1048576
    lock_text = (ROOT / ".github/workflows/daily-doc-updater.lock.yml").read_text()
    assert "CREATE_PR_PAT" not in lock_text
    assert "create_issue" not in lock_text
    assert "create_pull_request" not in lock_text
    assert '"GITHUB_READ_ONLY": "1"' in lock_text
    assert 'GH_AW_FAILURE_REPORT_AS_ISSUE: "false"' in lock_text
    assert 'GH_AW_NOOP_REPORT_AS_ISSUE: "false"' in lock_text
    assert "Record missing tool" not in lock_text
    assert "Record incomplete" not in lock_text
    for job in lock["jobs"].values():
        if any(
            "GH_AW_SAFE_OUTPUTS_CONFIG" in step.get("env", {})
            or "GH_AW_SAFE_OUTPUTS_HANDLER_CONFIG" in step.get("env", {})
            for step in job.get("steps", [])
        ):
            assert job.get("permissions", {}).get("issues") != "write"
        for permission in ("contents", "pull-requests"):
            assert job.get("permissions", {}).get(permission) != "write"
    assert "edit" not in source["tools"]
    assert "Both scheduled and manual" in text
    assert "Do not edit repository files, create implementation" in text
    assert "PRs, or call create-pull-request" in text
    assert "README.md is never a fallback" in text
    assert "Never claim unattended execution has obtained it" in text
    assert "After the separate human checkpoint" in text
    assert "single `type/docs` classification, no auto-merge" in text
    assert "handoff for a human-run session" in text
    assert set(source["permissions"].values()) == {"read"}


def test_cli_consistency_uses_one_type_without_false_docs_classification() -> None:
    """The literal source labels and both executable writers must agree."""
    source, lock, _ = _workflow("cli-consistency-checker")
    expected = ["type/automation", "area/cli"]
    assert source["safe-outputs"]["create-issue"]["labels"] == expected
    for config in _output_configs(lock).values():
        assert config["create_issue"]["labels"] == expected


def test_docs_workflow_never_treats_a_label_as_companion_approval() -> None:
    source, lock, text = _workflow("docs-sync")
    assert source["checkout"] is False
    assert "CONFIRM_PRESENT" not in text
    assert "IF AND ONLY IF" not in text
    assert "Both label and manual dispatch are advisory-only" in text
    assert "fresh responsible-human issue-scope checkpoint" in text
    assert "Steps 1-6" in text
    for config in _output_configs(lock).values():
        assert "create_pull_request" not in config


@pytest.mark.parametrize(
    "path",
    [
        BATCH / "references/invariants.md",
        DOCS / "SKILL.md",
        AUTOPILOT / "SKILL.md",
        ROOT / ".github/workflows/daily-doc-updater.md",
    ],
    ids=["bug-shepherd", "docs-sync", "issue-autopilot", "daily-docs"],
)
def test_consumers_probe_shared_trusted_owner_and_require_fresh_human(path: Path) -> None:
    """No consumer gets a second authority implementation or a machine grant."""
    text = path.read_text()
    assert "node scripts/governance/eligibility.cjs --help" in text or (
        path.name == "daily-doc-updater.md" and "first with `--help`" in text
    )
    assert "--repo microsoft/apm --issue N --approval-url URL" in text
    assert "authority.cjs" in text
    assert "trusted default-branch" in text
    assert "authorizes_implementation: false" in text
    assert "deleted withdrawals" in text
    assert "fresh" in text and "responsible" in text
    assert "STOP" in text


def test_bug_shepherd_union_human_gate_and_owned_cleanup() -> None:
    """Regression traps for legacy-only discovery, fail-open advice, and marker theft."""
    skill = (BATCH / "SKILL.md").read_text()
    prompt = (
        ROOT / "packages/batch-bug-shepherd/.apm/prompts/batch-bug-shepherd.prompt.md"
    ).read_text()
    for text in (skill, prompt):
        assert "--label type/bug" in text
        assert "--label bug" in text
        assert "deduplicate by issue number" in text
        assert "suspicion" in text
    assert skill.index("### Phase 2.5") < skill.index("### Phase 3 -")
    assert "rows without the Phase 2.5 checkpoint" in skill
    assert "ONLY PRs with a current Phase 2.5 checkpoint" in skill
    assert "only after rechecking the human checkpoint" in skill
    invariants = (BATCH / "references/invariants.md").read_text()
    assert "shepherd_marker_added_by_run" in invariants
    assert "Never sweep or clean up another run's markers" in invariants
    assert "If it\nwas already present" in invariants
    for path in [
        BATCH / "SKILL.md",
        BATCH / "assets/strategic-alignment-prompt.md",
        BATCH / "assets/verdict-schema.json",
        BATCH / "references/strategic-alignment-gate.md",
        BATCH / "references/invariants.md",
    ]:
        assert "fail-open" not in path.read_text().lower()
        assert "fails open" not in path.read_text().lower()


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("withdrawal", "Scope withdrawn after Phase 2.5."),
        ("unavailable evidence", "Trusted eligibility API unavailable at child recheck."),
    ],
    ids=["revoked-after-parent-checkpoint", "unavailable-after-parent-checkpoint"],
)
def test_fix_child_recheck_blocked_contract(failure: str, reason: str) -> None:
    """Validate refusal payloads and the prose interlock, not simulated LLM behavior."""
    prompt = (BATCH / "assets/fix-prompt.md").read_text()
    preflight = prompt.split("1. Re-read", 1)[0]
    assert failure in preflight
    assert "recheck overrides the earlier Phase 2.5 receipt" in preflight
    assert "make no edits or PR" in preflight
    schema = json.loads((BATCH / "assets/verdict-schema.json").read_text())
    Draft7Validator.check_schema(schema)
    validator = Draft7Validator(schema)
    examples = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", prompt, re.S)]
    assert {example["status"] for example in examples} == {"pr-opened", "blocked"}
    for example in examples:
        validator.validate(example)
    blocked = next(example for example in examples if example["status"] == "blocked")
    blocked["reason"] = reason
    validator.validate(blocked)
    for invalid in (
        {key: value for key, value in blocked.items() if key != "reason"},
        {**blocked, "pr": 3000},
        {**blocked, "branch": "fabricated"},
        {**blocked, "status": "pr-opened"},
    ):
        assert not validator.is_valid(invalid)
    skill = " ".join((BATCH / "SKILL.md").read_text().split())
    phase = skill.split("### Phase 3 -", 1)[1].split("### Phase 4 -", 1)[0]
    assert "inspect `status` before reading `pr` or `branch`" in phase
    assert "persist the row's `blocked` status and returned `reason`" in phase
    assert "exclude it from driver inputs, and continue" in phase
    assert phase.index("On `blocked`") < phase.index("Only `pr-opened`")
    assert "Malformed or wrong-issue returns also block" in phase


def test_docs_confirmation_label_is_request_not_ratification() -> None:
    """A label or populated template condition cannot invent a companion PR."""
    skill = (DOCS / "SKILL.md").read_text()
    template = (DOCS / "assets/advisory-comment-template.md").read_text()
    assert "never ratifies scope or permits companion implementation" in skill
    assert "older workflow prompt" in skill
    assert "Unattended\nlabel/manual-dispatch runs end with advice" in skill
    assert "confirm_label_present" not in template
    assert "{{ #if companion_pr_link }}" in template
    assert "it is not ratification" in template


def test_triage_keeps_receipt_recovery_and_explicit_retriage() -> None:
    """Label-write failure cannot cause repeated bot advice or consume human state."""
    _, lock, text = _workflow("triage-panel")
    assert "<!-- apm-triage-advisory:v2 -->" in text
    assert "github-actions[bot]" in text
    assert "complete comment history" in text
    assert "emit only the missing active processing marker" in text
    assert "Explicit requests may\nproduce fresh advice" in text
    for job in lock["jobs"].values():
        assert "status/needs-triage" not in str(job.get("if", ""))


def test_scope_eval_inventory_is_inputs_not_fake_results() -> None:
    """Preserve original cases plus the child-wave regression and unchanged routing inputs."""
    manifest = json.loads(
        (ROOT / "tests/fixtures/governance/maintainer-scope-evals.json").read_text()
    )
    assert manifest["evaluation_protocol"]["arms"] == [
        "previous-version",
        "updated",
        "without-skill",
    ]
    assert {case["id"] for case in manifest["content_evals"]} == {
        "bug-union-is-not-approval",
        "docs-label-and-record-are-not-ratification",
        "bounded-human-checkpoint-and-missing-owner",
        "pipeline-wave-recheck-is-not-a-stored-receipt",
    }
    for case in manifest["content_evals"]:
        assert case["must_do"] and case["must_not_do"]
        assert not {"response", "passed", "score", "evaluation_result"} & case.keys()
        source = ROOT / case["skill_source"]
        assert source.is_file()
        for reference in case["required_references"]:
            assert (source.parent / reference).is_file()
    for polarity in ("should_trigger", "should_not_trigger"):
        examples = manifest["trigger_evals"][polarity]
        assert len(examples["train"]) == 6
        assert len(examples["val"]) == 4


@pytest.mark.parametrize("skill_root", [BATCH, AUTOPILOT, DOCS], ids=["batch", "autopilot", "docs"])
def test_edited_skill_metadata_and_line_budgets(skill_root: Path) -> None:
    """Edited bodies and metadata stay bounded without a new tokenizer dependency."""
    _, frontmatter, body = (skill_root / "SKILL.md").read_text().split("---", 2)
    description = yaml.safe_load(frontmatter)["description"]
    assert 1 <= len(description) <= 1024
    assert len(body.splitlines()) <= 500


def test_trimmed_summaries_explicitly_load_binding_references() -> None:
    """Trimming repeated prose must not orphan the full operational contracts."""
    batch = (BATCH / "SKILL.md").read_text()
    autopilot = (AUTOPILOT / "SKILL.md").read_text()
    assert "Load `references/invariants.md` before planning Phase 0" in batch
    assert "the **Human scope checkpoint** in `references/invariants.md`" in batch
    assert "Load `assets/solution-pipeline-prompt.md` on entering Phase 4" in autopilot
    assert "It owns the full four-stage procedure" in autopilot


def test_pipeline_child_rechecks_scope_before_each_mutating_boundary() -> None:
    """The actual child brief, not only its parent's receipt, guards provisioning."""
    prompt = (AUTOPILOT / "assets/solution-pipeline-prompt.md").read_text()
    skill = (AUTOPILOT / "SKILL.md").read_text()
    for required_input in ("TRUSTED_GOVERNANCE_ROOT", "APPROVAL_URL", "HUMAN_SCOPE_RECEIPT"):
        assert required_input in prompt
        assert required_input in skill
    gate = prompt.split("## Current human-scope gate", 1)[1].split("## Model routing", 1)[0]
    assert 'cd "$TRUSTED_GOVERNANCE_ROOT"' in gate
    assert "node scripts/governance/eligibility.cjs --help" in gate
    assert '--issue "$ISSUE_NUMBER" --approval-url "$APPROVAL_URL"' in gate
    assert "authority.cjs" in gate
    assert "authorizes_implementation: false" in gate
    assert "fresh explicit responsible-human confirmation for this wave" in gate
    assert "deleted withdrawals" in gate
    for failure in ("withdrawn", "error", "uncertain confirmation"):
        assert failure in gate
    wave = prompt.split("## Stage 3 - Implement", 1)[1].split("## Stage 4 -", 1)[0]
    assert wave.index("0. Run the **Current human-scope gate**") < wave.index("git worktree add")
    assert "including resumed, retried, and replanned waves" in wave
    close = prompt.split("## Stage 4 - Acceptance close", 1)[1].split("## Return", 1)[0]
    assert close.index("Current human-scope gate") < close.index("acceptance-observer.md")


def test_pipeline_scope_refusal_stops_parent_before_pr_consumption() -> None:
    """Blocked child output has no fabricated PR and is excluded from downstream driving."""
    prompt = (AUTOPILOT / "assets/solution-pipeline-prompt.md").read_text()
    examples = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", prompt, re.S)]
    refusal = next(example for example in examples if example["status"] == "blocked")
    assert set(refusal) == {"kind", "issue", "status", "reason"}
    assert refusal["kind"] == "implement-result"
    assert isinstance(refusal["issue"], int) and refusal["issue"] > 0
    assert refusal["reason"]
    skill = " ".join((AUTOPILOT / "SKILL.md").read_text().split())
    phase = skill.split("### Phase 4 -", 1)[1].split("### Phase 5 -", 1)[0]
    assert "persist its status and reason in the row and `proceed_manifest`" in phase
    assert "do not read PR fields or dispatch Phase 5/6" in phase
    assert "Only `pr-opened` returns" in phase


def test_local_docs_install_preserves_resolved_links_and_hashes() -> None:
    """Compare installed local prose through the same resolver used by integration."""
    lock = yaml.safe_load((ROOT / "apm.lock.yaml").read_text())
    integrator = SkillIntegrator()
    integrator.init_link_resolver(
        SimpleNamespace(install_path=ROOT, deployment_package_root=ROOT), ROOT
    )
    assert integrator.link_resolver is not None
    installed_root = ROOT / ".agents/skills/docs-sync"
    for relative, expected_hash in lock["local_deployed_file_hashes"].items():
        installed = ROOT / relative
        if not installed.is_relative_to(installed_root):
            continue
        source = DOCS / installed.relative_to(installed_root)
        content = source.read_text()
        if source.suffix == ".md":
            content, _ = integrator.resolve_links(
                content, source, installed, preserved_source_root=DOCS
            )
        assert installed.read_text() == content
        assert compute_file_hash(installed) == expected_hash


def test_affected_deployment_ledger_hashes_match_installed_files() -> None:
    """Both legacy views and canonical deployment entries describe actual emitted bytes."""
    lock = yaml.safe_load((ROOT / "apm.lock.yaml").read_text())
    roots = tuple(
        f".agents/skills/{name}/"
        for name in ("apm-triage-panel", "batch-bug-shepherd", "apm-issue-autopilot", "docs-sync")
    )
    for record in lock["deployments"]:
        if record["value"].startswith(roots) and record["content_hash"] is not None:
            assert record["content_hash"] == compute_file_hash(ROOT / record["value"])
