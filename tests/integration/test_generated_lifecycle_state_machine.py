"""Deterministic replay and bounded search through one observed real-CLI driver."""

from __future__ import annotations

import itertools
import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import HealthCheck, Phase, settings
from hypothesis.stateful import RuleBasedStateMachine, precondition, rule, run_state_machine_as_test

from tests.utils.lifecycle_model import ReplayCase, legal_transitions, load_corpus
from tests.utils.lifecycle_model_driver import create_cli_driver

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.lifecycle_merge_group,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]


@pytest.mark.parametrize("case", load_corpus(), ids=lambda case: case.case_id)
def test_lifecycle_corpus_replay(
    tmp_path: Path,
    apm_binary_path: Path,
    record_property: Callable[[str, object], None],
    case: ReplayCase,
) -> None:
    """The first record guarantees all ten transitions; remaining records replay designs."""
    driver, runner = create_cli_driver(tmp_path / case.case_id, apm_binary_path, case.case_id)
    try:
        with runner.scenario(scenario_id=case.case_id):
            driver.replay(case)
    except Exception as error:
        if driver.failure is None:
            driver._fail("scenario.deadline_or_replay", error)
        raise
    finally:
        record_property("lifecycle_model_execution", json.dumps(driver.evidence(), sort_keys=True))


class _LifecycleSearch(RuleBasedStateMachine):
    """Only scheduling lives here; the driver owns all mutations and assertions."""

    def __init__(
        self,
        root: Path,
        apm_binary_path: Path,
        record_property: Callable[[str, object], None],
    ) -> None:
        super().__init__()
        self.root = root
        self.record_property = record_property
        self.driver, runner = create_cli_driver(root, apm_binary_path, root.name)
        self.deadline = runner.scenario(scenario_id=root.name)
        self.deadline.__enter__()

    def enabled(self, transition: str) -> bool:
        return transition in legal_transitions(self.driver.state)

    @rule()
    @precondition(lambda self: self.enabled("install"))
    def install(self) -> None:
        self.driver.apply("install")

    @rule()
    @precondition(lambda self: self.enabled("reinstall"))
    def reinstall(self) -> None:
        self.driver.apply("reinstall")

    @rule()
    @precondition(lambda self: self.enabled("dry_run"))
    def dry_run(self) -> None:
        self.driver.apply("dry_run")

    @rule()
    @precondition(lambda self: self.enabled("tamper"))
    def tamper(self) -> None:
        self.driver.apply("tamper")

    @rule()
    @precondition(lambda self: self.enabled("audit_tampered"))
    def audit_tampered(self) -> None:
        self.driver.apply("audit_tampered")

    @rule()
    @precondition(lambda self: self.enabled("repair"))
    def repair(self) -> None:
        self.driver.apply("repair")

    @rule()
    @precondition(lambda self: self.enabled("audit_clean"))
    def audit_clean(self) -> None:
        self.driver.apply("audit_clean")

    @rule()
    @precondition(lambda self: self.enabled("remove_declaration"))
    def remove_declaration(self) -> None:
        self.driver.apply("remove_declaration")

    @rule()
    @precondition(lambda self: self.enabled("readd_declaration"))
    def readd_declaration(self) -> None:
        self.driver.apply("readd_declaration")

    @rule()
    @precondition(lambda self: self.enabled("prune_removed"))
    def prune_removed(self) -> None:
        self.driver.apply("prune_removed")

    def teardown(self) -> None:
        """Check the aggregate deadline and publish actual evidence before deleting roots."""
        try:
            self.deadline.__exit__(None, None, None)
        except Exception as error:
            if self.driver.failure is None:
                self.driver._fail("scenario.deadline", error)
        finally:
            self.record_property(
                "lifecycle_model_execution", json.dumps(self.driver.evidence(), sort_keys=True)
            )
            shutil.rmtree(self.root)


def test_generated_lifecycle_sequences_preserve_reference_model(
    tmp_path: Path,
    apm_binary_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    """Search and shrink six examples of at most eight legal driver actions."""
    sequence = itertools.count()

    def factory() -> _LifecycleSearch:
        return _LifecycleSearch(
            tmp_path / f"generated-{next(sequence):03d}", apm_binary_path, record_property
        )

    run_state_machine_as_test(
        factory,
        settings=settings(
            database=None,
            deadline=None,
            derandomize=True,
            max_examples=6,
            phases=(Phase.generate, Phase.shrink),
            print_blob=True,
            stateful_step_count=8,
            suppress_health_check=(HealthCheck.too_slow,),
        ),
    )
