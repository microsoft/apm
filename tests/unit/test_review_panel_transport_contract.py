"""Static instruction/provenance guards, NOT model or hosted-delivery tests."""

from pathlib import Path

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.content_hash import compute_file_hash

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "packages/apm-review-panel/SKILL.md"
DEPLOYED = ".agents/skills/apm-review-panel/SKILL.md"


def _section(text: str, start: str, end: str) -> str:
    return " ".join(text.split(start, 1)[1].split(end, 1)[0].split())


@pytest.mark.parametrize(
    "clause",
    [
        "invoke structured `add_comment` ONCE with the complete markdown "
        "directly in its `body` argument.",
        "No shell staging, wrapper, or intermediate comment file.",
        "Configured safeoutputs that are missing, failed, or uncertain "
        "NEVER authorize direct GitHub writes or a second comment.",
        "Unknown is not absent.",
        "Only outside a safe-output workflow, when safeoutputs are absent "
        "AND the caller authorizes interactive writes",
        "create the body via a native file-edit tool",
        "`gh pr comment <PR_NUMBER> --repo <OWNER/REPO> --body-file <PATH>`",
        "never final, panelist, or CEO prose: no heredocs, `echo`, `printf`, "
        "inline scripts, substitutions, or encoding workarounds.",
    ],
)
def test_emission_boundary_retains_required_clauses(clause: str) -> None:
    """Deleting a required transport clause must trip a bounded text guard."""
    emission = _section(SOURCE.read_text(), "7. **Render", "8. **Sweep")
    assert clause in emission


def test_exit_distinguishes_acceptance_from_delivery() -> None:
    text = SOURCE.read_text()
    exit_rule = _section(text, "9. **Verify exit.", "## Output contract")
    for clause in (
        "label cleanup alone is not the required output",
        "Buffered acceptance is NOT publication",
        "call advertised `noop`",
        "If that tool is missing/fails, report failure explicitly",
        "Zero safe outputs is a FAILURE",
        "verify the posted comment and label state via CLI read-back",
    ):
        assert clause in exit_rule
    cleanup = _section(text, "8. **Sweep", "9. **Verify")
    assert "[panel-review, panel-approved, panel-rejected]" in cleanup
    assert "NEVER apply verdict labels" in cleanup
    assert "`27815857237`" in text


def test_deployment_and_both_hash_views_match_source() -> None:
    assert SOURCE.read_bytes() == (ROOT / DEPLOYED).read_bytes()
    digest = compute_file_hash(ROOT / DEPLOYED)
    lock = LockFile.read(ROOT / "apm.lock.yaml")
    assert lock is not None
    rows = [
        row
        for row in DeploymentLedgerCodec.from_lockfile(lock).records.values()
        if row.locator.value == DEPLOYED
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row.locator.target == "copilot"
    assert row.locator.scope == "project"
    assert row.active_owner == "local:packages/apm-review-panel"
    assert row.content_hash == digest
    assert lock.dependencies[row.active_owner].deployed_file_hashes[DEPLOYED] == digest
