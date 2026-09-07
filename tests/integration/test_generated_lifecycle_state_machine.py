"""Bounded model-based lifecycle checks against the installed APM CLI."""

from __future__ import annotations

import ast
import inspect
import itertools
import json
import shutil
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, Phase, settings
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _audit,
    _new_scenario,
    _publish,
    _result_evidence,
    _run_success,
    _Scenario,
    _skill,
)
from tests.utils.artifact_snapshot import (
    ArtifactSnapshotSet,
    assert_only_snapshot_paths_changed,
    assert_snapshot_changes_within,
    assert_snapshot_set_unchanged,
)
from tests.utils.local_package import LocalPackage

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.lifecycle_merge_group,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_SKILL_NAME = "model-skill"
_PACKAGE_NAME = "model-kit"
_SKILL_BYTES = _skill(_SKILL_NAME).encode()
_PROJECT_WRITE_PATHS = frozenset(
    {
        ".agents",
        ".agents/skills",
        f".agents/skills/{_SKILL_NAME}",
        f".agents/skills/{_SKILL_NAME}/SKILL.md",
        ".github",
        ".gitignore",
        "apm.lock.yaml",
    }
)
_USER_WRITE_PATHS = frozenset(
    {
        ".apm/config.json",
        ".local",
        ".local/state",
        ".local/state/gh",
        ".local/state/gh/device-id",
    }
)
_INSTALL_EXACT_PATHS = {
    "project": _PROJECT_WRITE_PATHS,
    "user": _USER_WRITE_PATHS,
}
_INSTALL_TREE_PREFIXES = {"project": frozenset({"apm_modules"})}
_LEDGER_PATH = Path(__file__).parents[1] / "fixtures" / "lifecycle_bug_ledger.json"
_TRANSITION_PROPERTIES = {
    "audit_clean": frozenset({"filesystem.open_world_observation", "outcome.status_matches_state"}),
    "audit_tampered": frozenset(
        {
            "filesystem.open_world_observation",
            "outcome.status_matches_state",
            "transaction.failed_command_preserves_state",
        }
    ),
    "dry_run": frozenset(
        {
            "filesystem.open_world_observation",
            "transaction.failed_command_preserves_state",
        }
    ),
    "install": frozenset({"routing.authorized_targets_only", "source.ref_cache_coherent"}),
    "prune_removed": frozenset({"ownership.preserve_unowned"}),
    "readd_declaration": frozenset({"transaction.failed_command_preserves_state"}),
    "reinstall": frozenset({"idempotency.byte_stable"}),
    "remove_declaration": frozenset({"ownership.preserve_unowned"}),
    "repair": frozenset({"source.ref_cache_coherent"}),
    "tamper": frozenset({"outcome.status_matches_state"}),
}


def _phase_one_properties() -> frozenset[str]:
    ledger = json.loads(_LEDGER_PATH.read_text(encoding="ascii"))
    return frozenset(row["id"] for row in ledger["property_catalog"] if row["phase"] == 1)


_PHASE_ONE_PROPERTIES = _phase_one_properties()


@dataclass(frozen=True)
class _ModelFixture:
    scenario: _Scenario
    project: LocalPackage
    dependency: dict[str, object]
    environment: dict[str, str]

    @classmethod
    def create(cls, root: Path, apm_binary_path: Path) -> _ModelFixture:
        scenario = _new_scenario(root, apm_binary_path)
        published = _publish(scenario, _PACKAGE_NAME, skill=_SKILL_NAME)
        project = scenario.consumers.create(
            "model-consumer",
            dependencies=(published.dependency,),
            targets=("copilot",),
        )
        return cls(
            scenario=scenario,
            project=project,
            dependency=published.dependency,
            environment=published.environment,
        )


class _LifecycleReferenceModel(RuleBasedStateMachine):
    """Reference state independent of lockfile deployment records."""

    def __init__(self, fixture: _ModelFixture) -> None:
        super().__init__()
        self.fixture = fixture
        self.declared = True
        self.materialized = False
        self.clean = True
        self.locked = False
        self.step = 0

    @property
    def scenario(self) -> _Scenario:
        return self.fixture.scenario

    @property
    def project(self) -> LocalPackage:
        return self.fixture.project

    @property
    def skill_path(self) -> Path:
        return self.project.root / ".agents" / "skills" / _SKILL_NAME / "SKILL.md"

    def _next_id(self, operation: str) -> str:
        self.step += 1
        return f"generated-{self.step:02d}-{operation}"

    def _capture(self) -> ArtifactSnapshotSet:
        return ArtifactSnapshotSet.capture(
            {
                "project": self.project.root,
                "user": self.scenario.isolated.home,
            }
        )

    @rule()
    @precondition(lambda self: self.declared and not self.materialized)
    def install(self) -> None:
        before = self._capture()
        _run_success(
            self.scenario,
            self.project,
            _INSTALL_ARGS,
            environment=self.fixture.environment,
            scenario_id=self._next_id("install"),
        )
        assert_snapshot_changes_within(
            before,
            self._capture(),
            exact_paths=_INSTALL_EXACT_PATHS,
            tree_prefixes=_INSTALL_TREE_PREFIXES,
        )
        self.materialized = True
        self.clean = True
        self.locked = True

    @rule()
    @precondition(lambda self: self.declared and self.materialized and self.clean)
    def reinstall(self) -> None:
        before = self._capture()
        _run_success(
            self.scenario,
            self.project,
            _INSTALL_ARGS,
            environment=self.fixture.environment,
            scenario_id=self._next_id("reinstall"),
        )
        assert_snapshot_set_unchanged(before, self._capture())

    @rule()
    @precondition(lambda self: self.declared)
    def dry_run(self) -> None:
        before = self._capture()
        result = _run_success(
            self.scenario,
            self.project,
            (*_INSTALL_ARGS, "--dry-run"),
            environment=self.fixture.environment,
            scenario_id=self._next_id("dry-run"),
        )
        assert "[i] APM dependencies (1):" in result.stdout.splitlines(), (
            "dry-run must prove dependency resolution reached the fixture"
        )
        assert_snapshot_set_unchanged(before, self._capture())

    @rule()
    @precondition(lambda self: self.materialized and self.clean)
    def tamper(self) -> None:
        before = self._capture()
        self.skill_path.write_bytes(b"# user tamper\n")
        assert_only_snapshot_paths_changed(
            before,
            self._capture(),
            {"project": {f".agents/skills/{_SKILL_NAME}/SKILL.md"}},
        )
        self.clean = False

    @rule()
    @precondition(lambda self: self.declared and self.materialized and not self.clean)
    def audit_tampered(self) -> None:
        before = self._capture()
        result, payload = _audit(
            self.scenario,
            self.project,
            environment=self.fixture.environment,
            expected_returncode=1,
            scenario_id=self._next_id("audit-tampered"),
        )
        assert payload["passed"] is False, _result_evidence(result)
        assert_snapshot_set_unchanged(before, self._capture())

    @rule()
    @precondition(lambda self: self.declared and self.materialized and not self.clean)
    def repair(self) -> None:
        before = self._capture()
        _run_success(
            self.scenario,
            self.project,
            _INSTALL_ARGS,
            environment=self.fixture.environment,
            scenario_id=self._next_id("repair"),
        )
        assert_snapshot_changes_within(
            before,
            self._capture(),
            exact_paths=_INSTALL_EXACT_PATHS,
            tree_prefixes=_INSTALL_TREE_PREFIXES,
        )
        self.clean = True

    @rule()
    @precondition(lambda self: self.declared and self.materialized and self.clean)
    def audit_clean(self) -> None:
        before = self._capture()
        _result, payload = _audit(
            self.scenario,
            self.project,
            environment=self.fixture.environment,
            scenario_id=self._next_id("audit-clean"),
        )
        assert payload["passed"] is True
        assert_snapshot_set_unchanged(before, self._capture())

    @rule()
    @precondition(lambda self: self.declared)
    def remove_declaration(self) -> None:
        before = self._capture()
        assert self.scenario.consumers.remove_apm_dependency(
            self.project,
            self.fixture.dependency,
        )
        assert_only_snapshot_paths_changed(
            before,
            self._capture(),
            {"project": {"apm.yml"}},
        )
        self.declared = False

    @rule()
    @precondition(lambda self: not self.declared)
    def readd_declaration(self) -> None:
        before = self._capture()
        self.scenario.consumers.replace_apm_dependencies(
            self.project,
            (self.fixture.dependency,),
        )
        assert_only_snapshot_paths_changed(
            before,
            self._capture(),
            {"project": {"apm.yml"}},
        )
        self.declared = True

    @rule()
    @precondition(lambda self: not self.declared and self.materialized)
    def prune_removed(self) -> None:
        before = self._capture()
        _run_success(
            self.scenario,
            self.project,
            ("prune",),
            environment=self.fixture.environment,
            scenario_id=self._next_id("prune"),
        )
        assert_snapshot_changes_within(
            before,
            self._capture(),
            exact_paths=_INSTALL_EXACT_PATHS,
            tree_prefixes=_INSTALL_TREE_PREFIXES,
        )
        self.materialized = False
        self.clean = True
        self.locked = False

    @invariant()
    def durable_state_matches_reference_model(self) -> None:
        manifest = yaml.safe_load(self.project.manifest_path.read_text(encoding="utf-8"))
        assert isinstance(manifest, dict)
        dependencies = manifest.get("dependencies", {}).get("apm", [])
        assert bool(dependencies) is self.declared
        assert self.skill_path.exists() is self.materialized
        if self.materialized and self.clean:
            assert self.skill_path.read_bytes() == _SKILL_BYTES
        assert (self.project.root / "apm.lock.yaml").exists() is self.locked

    def teardown(self) -> None:
        """Remove per-example roots after Hypothesis records the result."""
        shutil.rmtree(self.scenario.isolated.root)


def test_generated_lifecycle_sequences_preserve_reference_model(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Generate and shrink guarded transition sequences over a real CLI."""
    sequence = itertools.count()

    def factory() -> _LifecycleReferenceModel:
        case_root = tmp_path / f"case-{next(sequence):03d}"
        return _LifecycleReferenceModel(_ModelFixture.create(case_root, apm_binary_path))

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


def test_generated_transition_catalog_covers_phase_one_properties() -> None:
    """Require every generated transition to invoke a filesystem oracle."""
    rules = {
        name: value
        for name, value in vars(_LifecycleReferenceModel).items()
        if getattr(value, "hypothesis_stateful_rule", None) is not None
    }

    assert set(_TRANSITION_PROPERTIES) == set(rules)
    assert set().union(*_TRANSITION_PROPERTIES.values()) == set(_PHASE_ONE_PROPERTIES)
    oracle_calls = {
        "assert_only_snapshot_paths_changed",
        "assert_snapshot_changes_within",
        "assert_snapshot_set_unchanged",
    }
    for rule_name, function in rules.items():
        tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(function))))
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert called_names & oracle_calls, f"{rule_name} does not invoke a filesystem oracle"
