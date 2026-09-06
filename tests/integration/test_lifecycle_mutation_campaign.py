"""Three real source-CLI mutants against green, unchanged lifecycle oracles.

Requires the installed apm-cli Python distribution in pytest's interpreter.
No frozen apm_binary_path is substituted: this explicitly tests source CLI.
"""

from __future__ import annotations

import inspect
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import apm_cli
from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.integration import cleanup
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from tests.integration.test_primitive_target_covering_array import _execute_row
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.lifecycle_interactions import ROUTING_ROWS, known_gap_for
from tests.utils.lifecycle_mutations import (
    MUTATIONS,
    LifecycleMutation,
    classify_mutation,
    observe_run,
    write_report,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.lifecycle_merge_group,
    pytest.mark.requires_e2e_mode,
]


def _parent_owners() -> tuple[Callable[..., object], ...]:
    """Snapshot the exact parent owners; child probes must never replace these."""
    return (
        SkillIntegrator._promote_sub_skills,
        DeploymentLedgerCodec.rows,
        cleanup.remove_stale_deployed_files,
    )


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda mutation: mutation.id)
def test_lifecycle_production_mutation(
    tmp_path: Path,
    mutation: LifecycleMutation,
    record_property: Callable[[str, object], None],
) -> None:
    """Require a passing baseline and a reached, law-specific production kill."""
    row = next(row for row in ROUTING_ROWS if row.id == mutation.case_id)
    assert known_gap_for(row) is None
    owners_before = _parent_owners()
    catalog_before = repr(KNOWN_TARGETS)
    sources_before = {
        path: path.read_bytes()
        for owner in owners_before
        for path in (Path(inspect.getfile(owner)),)
    }
    helper = Path(__file__).parents[1] / "utils/lifecycle_mutations.py"
    observations = []
    for mode in ("baseline", "mutant"):
        work = tmp_path / mode
        work.mkdir()
        events = work / "child-events.jsonl"
        runner = ApmLifecycleRunner(
            (
                sys.executable,
                "-B",
                str(helper),
                "--child",
                mutation.id,
                mode,
                str(events),
                str(Path(apm_cli.__file__).resolve().parent),
            ),
            scenario_timeout_seconds=300,
        )

        def run(runner: ApmLifecycleRunner = runner, mode: str = mode, work: Path = work) -> None:
            with runner.scenario(scenario_id=f"{mutation.id}-{mode}"):
                evidence = _execute_row(work, row, runner, time.monotonic(), None, [], None)
                assert evidence.status == "executed"

        observations.append(observe_run(run, events))
    result = classify_mutation(mutation, *observations)
    result["parent_owners_unchanged"] = _parent_owners() == owners_before
    result["parent_catalog_unchanged"] = repr(KNOWN_TARGETS) == catalog_before
    result["production_sources_unchanged"] = all(
        path.read_bytes() == content for path, content in sources_before.items()
    )
    report = write_report(tmp_path / "mutation-report.json", (result,))
    record_property("lifecycle_mutation", json.dumps(report, sort_keys=True, ensure_ascii=True))
    assert result["parent_owners_unchanged"]
    assert result["parent_catalog_unchanged"]
    assert result["production_sources_unchanged"]
    assert result["status"] == "killed", (
        f"{mutation.id}: {result['status']}; "
        f"baseline={result['baseline']}; mutant={result['mutant']}"
    )
