"""Bounded model-based lifecycle checks against the installed APM CLI."""

from __future__ import annotations

import ast
import inspect
import itertools
import json
import shutil
import textwrap
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest
import yaml
from hypothesis import HealthCheck, Phase, settings
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_required_lifecycle_state_machine import (
    _INSTALL_ARGS,
    _assert_same_state,
    _audit_at,
    _external_root_specs,
    _new_scenario,
    _publish,
    _publish_revision,
    _PublishedPackage,
    _result_evidence,
    _Scenario,
    _skill,
)
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import (
    ArtifactSnapshotSet,
    assert_only_snapshot_paths_changed,
    assert_snapshot_changes_within,
    assert_snapshot_set_unchanged,
)
from tests.utils.lifecycle_state import LifecycleStateRoot, LifecycleStateSnapshot
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
_TAMPER_BYTES = b"# user tamper\n"
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
    "publish": frozenset({"source.ref_cache_coherent"}),
    "outdated": frozenset({"source.ref_cache_coherent"}),
    "update": frozenset({"source.ref_cache_coherent"}),
    "compile": frozenset({"routing.authorized_targets_only"}),
    "lock": frozenset({"ownership.preserve_unowned"}),
    "frozen_replay": frozenset({"idempotency.byte_stable"}),
    "frozen_refusal": frozenset({"transaction.failed_command_preserves_state"}),
    "uninstall": frozenset({"ownership.preserve_unowned"}),
    "audit_empty": frozenset({"outcome.status_matches_state"}),
    "inspect_installed": frozenset({"filesystem.open_world_observation"}),
    "export_lock": frozenset({"source.ref_cache_coherent"}),
    "clean_sources": frozenset({"source.ref_cache_coherent", "ownership.preserve_unowned"}),
    "clean_cache": frozenset({"filesystem.open_world_observation"}),
    "legacy_update": frozenset({"source.ref_cache_coherent"}),
}
_VARIANTS = ("project", "global-canonical", "global-aliased")


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
    source: _PublishedPackage
    variant: str
    invoke_cwd: Path
    audit_cwd: Path
    skill_path: Path
    external_roots: tuple[LifecycleStateRoot, ...] = ()

    @classmethod
    def create(cls, root: Path, apm_binary_path: Path, variant: str = "project") -> _ModelFixture:
        scenario = _new_scenario(root, apm_binary_path)
        published = _publish(scenario, _PACKAGE_NAME, skill=_SKILL_NAME)
        if variant != "project":
            commit_a = _publish_revision(scenario, published, "a")
            dependency = {**published.dependency, "ref": "main"}
            environment = dict(published.environment)
            target_root = scenario.isolated.root / "claude-home"
            target_root.mkdir()
            (target_root / "user-owned.txt").write_bytes(b"user-owned\n")
            environment["CLAUDE_CONFIG_DIR"] = str(target_root)
            if variant == "global-aliased":
                alias = scenario.isolated.root / "home-alias"
                alias.symlink_to(scenario.isolated.home, target_is_directory=True)
                environment.update(
                    HOME=str(alias), USERPROFILE=str(alias), APM_HOME=str(alias / ".apm")
                )
            state_root = scenario.isolated.config_root
            state_root.mkdir(parents=True, exist_ok=True)
            manifest_path = state_root / "apm.yml"
            dump_yaml(
                {
                    "name": "global-model-consumer",
                    "version": "0.1.0",
                    "dependencies": {"apm": [dependency]},
                    "targets": ["claude"],
                },
                manifest_path,
            )
            source = _PublishedPackage(
                published.package,
                published.repository,
                commit_a,
                published.remote_url,
                dependency,
                environment,
            )
            return cls(
                scenario,
                LocalPackage("global-model-consumer", state_root, manifest_path),
                dependency,
                environment,
                source,
                variant,
                scenario.isolated.work_root,
                state_root,
                target_root / "skills" / _SKILL_NAME / "SKILL.md",
                _external_root_specs(
                    {"claude": target_root},
                    config_paths={"claude": (PurePosixPath("CLAUDE.md"),)},
                ),
            )
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
            source=published,
            variant=variant,
            invoke_cwd=project.root,
            audit_cwd=project.root,
            skill_path=project.root / ".agents" / "skills" / _SKILL_NAME / "SKILL.md",
        )

    @property
    def global_scope(self) -> bool:
        return self.variant != "project"

    @property
    def install_args(self) -> tuple[str, ...]:
        return (*_INSTALL_ARGS, "--global") if self.global_scope else _INSTALL_ARGS

    @property
    def exact_paths(self) -> dict[str, frozenset[str]]:
        if not self.global_scope:
            return _INSTALL_EXACT_PATHS
        return {
            "user": _USER_WRITE_PATHS,
            "target": frozenset({"skills", "rules", "rules/revision.md", "CLAUDE.md"}),
        }

    @property
    def tree_prefixes(self) -> dict[str, frozenset[str]]:
        if not self.global_scope:
            return _INSTALL_TREE_PREFIXES
        return {
            "user": frozenset({".apm"}),
            "target": frozenset({f"skills/{_SKILL_NAME}"}),
        }

    def replace_dependencies(self, dependencies: tuple[dict[str, object], ...]) -> None:
        manifest = load_yaml(self.project.manifest_path)
        manifest.setdefault("dependencies", {})["apm"] = list(dependencies)
        dump_yaml(manifest, self.project.manifest_path)


class _LifecycleReferenceModel(RuleBasedStateMachine):
    """Reference state independent of lockfile deployment records."""

    def __init__(self, fixture: _ModelFixture) -> None:
        super().__init__()
        self.fixture = fixture
        self.declared = True
        self.materialized = False
        self.clean = True
        self.locked = False
        self.retained_user_file = False
        self.step = 0
        self.last_operation = "initial"
        self.published_revision = "a"
        self.installed_revision = "a"
        self.published_commit = fixture.source.commit.sha
        self.installed_commit = fixture.source.commit.sha

    @initialize()
    def mandatory_spine(self) -> None:
        """Guarantee connected commands inside every generated model execution."""
        _mandatory_replay(self)

    @property
    def scenario(self) -> _Scenario:
        return self.fixture.scenario

    @property
    def project(self) -> LocalPackage:
        return self.fixture.project

    @property
    def skill_path(self) -> Path:
        return self.fixture.skill_path

    def _run(
        self,
        args: tuple[str, ...],
        operation: str,
        expected_returncode: int = 0,
        *,
        command_cwd: Path | None = None,
    ) -> CommandResult:
        result = self.scenario.runner.run(
            args,
            cwd=command_cwd or self.fixture.invoke_cwd,
            env=self.fixture.environment,
            scenario_id=self._next_id(operation),
        )
        assert result.returncode == expected_returncode, _result_evidence(result)
        return result

    def _state(self) -> LifecycleStateSnapshot:
        return LifecycleStateSnapshot.capture(
            self.project.root, external_roots=self.fixture.external_roots
        )

    def _changed_path(self, relative_path: str) -> dict[str, set[str]]:
        if self.fixture.global_scope:
            return {"user": {f".apm/{relative_path}"}}
        return {"project": {relative_path}}

    def _next_id(self, operation: str) -> str:
        self.step += 1
        self.last_operation = operation
        return f"generated-{self.step:02d}-{operation}"

    def _capture(self) -> ArtifactSnapshotSet:
        roots = {"project": self.fixture.invoke_cwd, "user": self.scenario.isolated.home}
        if self.fixture.global_scope:
            roots["target"] = self.fixture.external_roots[0].path
        return ArtifactSnapshotSet.capture(roots)

    @rule()
    @precondition(lambda self: self.declared and not self.materialized)
    def install(self) -> None:
        before = self._capture()
        self._run(self.fixture.install_args, "install")
        assert_snapshot_changes_within(
            before,
            self._capture(),
            exact_paths=self.fixture.exact_paths,
            tree_prefixes=self.fixture.tree_prefixes,
        )
        self.materialized = True
        self.clean = not self.retained_user_file
        self.locked = True
        self.installed_revision = self.published_revision
        self.installed_commit = self.published_commit

    @rule()
    @precondition(lambda self: self.declared and self.materialized and self.clean)
    def reinstall(self) -> None:
        before = self._capture()
        self._run(self.fixture.install_args, "reinstall")
        assert_snapshot_set_unchanged(before, self._capture())

    @rule()
    @precondition(lambda self: self.declared)
    def dry_run(self) -> None:
        before = self._capture()
        result = self._run((*self.fixture.install_args, "--dry-run"), "dry-run")
        assert "[i] APM dependencies (1):" in result.stdout.splitlines()
        assert_snapshot_set_unchanged(before, self._capture())

    @rule()
    @precondition(lambda self: self.materialized and self.clean)
    def tamper(self) -> None:
        before = self._capture()
        self.skill_path.write_bytes(_TAMPER_BYTES)
        assert_only_snapshot_paths_changed(
            before,
            self._capture(),
            (
                {"target": {f"skills/{_SKILL_NAME}/SKILL.md"}}
                if self.fixture.global_scope
                else {"project": {f".agents/skills/{_SKILL_NAME}/SKILL.md"}}
            ),
        )
        self.clean = False

    @rule()
    @precondition(
        lambda self: (
            self.declared and self.materialized and not self.clean and not self.retained_user_file
        )
    )
    def audit_tampered(self) -> None:
        before = self._capture()
        result, payload = _audit_at(
            self.scenario,
            self.fixture.audit_cwd,
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
        args = self.fixture.install_args
        if self.retained_user_file:
            args = (*args, "--force")
        self._run(args, "repair")
        assert_snapshot_changes_within(
            before,
            self._capture(),
            exact_paths=self.fixture.exact_paths,
            tree_prefixes=self.fixture.tree_prefixes,
        )
        self.clean = True
        self.retained_user_file = False

    @rule()
    @precondition(lambda self: self.declared and self.materialized and self.clean)
    def audit_clean(self) -> None:
        before = self._capture()
        _result, payload = _audit_at(
            self.scenario,
            self.fixture.audit_cwd,
            environment=self.fixture.environment,
            scenario_id=self._next_id("audit-clean"),
        )
        assert payload["passed"] is True
        assert_snapshot_set_unchanged(before, self._capture())

    @rule()
    @precondition(lambda self: self.declared)
    def remove_declaration(self) -> None:
        before = self._capture()
        self.fixture.replace_dependencies(())
        assert_only_snapshot_paths_changed(
            before,
            self._capture(),
            self._changed_path("apm.yml"),
        )
        self.declared = False

    @rule()
    @precondition(lambda self: not self.declared)
    def readd_declaration(self) -> None:
        before = self._capture()
        self.fixture.replace_dependencies((self.fixture.dependency,))
        assert_only_snapshot_paths_changed(
            before,
            self._capture(),
            self._changed_path("apm.yml"),
        )
        self.declared = True

    @rule()
    @precondition(
        lambda self: not self.fixture.global_scope and not self.declared and self.materialized
    )
    def prune_removed(self) -> None:
        before = self._capture()
        retained_bytes = self.skill_path.read_bytes() if not self.clean else None
        self._run(("prune",), "prune")
        assert_snapshot_changes_within(
            before,
            self._capture(),
            exact_paths=_INSTALL_EXACT_PATHS,
            tree_prefixes=_INSTALL_TREE_PREFIXES,
        )
        self.materialized = False
        self.retained_user_file = retained_bytes is not None
        if retained_bytes is not None:
            assert self.skill_path.read_bytes() == retained_bytes
        self.clean = True
        self.locked = False

    @rule()
    @precondition(lambda self: self.fixture.global_scope and self.published_revision == "a")
    def publish(self) -> None:
        before = self._capture()
        commit = _publish_revision(self.scenario, self.fixture.source, "b")
        self.published_revision = "b"
        self.published_commit = commit.sha
        assert_snapshot_set_unchanged(before, self._capture())

    @rule()
    @precondition(lambda self: self.fixture.global_scope and self.declared and self.materialized)
    def outdated(self) -> None:
        before, state = self._capture(), self._state()
        result = self._run(
            ("outdated", "--global", "--parallel-checks", "0", "--verbose"), "outdated"
        )
        if self.installed_commit != self.published_commit:
            assert self.published_commit[:7] in result.stdout, _result_evidence(result)
        _assert_same_state(state, self._state())
        assert_snapshot_set_unchanged(before, self._capture())

    @rule()
    @precondition(lambda self: self.fixture.global_scope and self.materialized and self.clean)
    def inspect_installed(self) -> None:
        before, state = self._capture(), self._state()
        for args in (
            ("deps", "list", "--global"),
            ("deps", "tree", "--global"),
            ("deps", "why", _PACKAGE_NAME, "--global", "--json"),
            ("view", _PACKAGE_NAME, "--global"),
            ("info", _PACKAGE_NAME, "--global"),
        ):
            result = self._run(args, f"inspect-{'-'.join(args[:2])}")
            assert _PACKAGE_NAME in result.stdout, _result_evidence(result)
            _assert_same_state(state, self._state())
            assert_snapshot_set_unchanged(before, self._capture())
        info = self._run(
            ("deps", "info", _PACKAGE_NAME), "deps-info", command_cwd=self.fixture.audit_cwd
        )
        assert _PACKAGE_NAME in info.stdout, _result_evidence(info)
        self._run(("cache", "info"), "cache-info")
        pruned = self._run(("cache", "prune", "--days", "30"), "cache-prune")
        assert "Pruned 0 SHA group(s)" in pruned.stdout, _result_evidence(pruned)
        targets = self._run(("targets", "--json"), "targets-project-only")
        rows = json.loads(targets.stdout)
        assert rows and all(row["status"] == "inactive" for row in rows)
        found = self._run(
            ("find", str(self.skill_path)),
            "find-global-refusal",
            1,
            command_cwd=self.fixture.audit_cwd,
        )
        assert "is not tracked by any installed package" in " ".join(found.stdout.split())
        _assert_same_state(state, self._state())
        assert_snapshot_set_unchanged(before, self._capture())

    @rule()
    @precondition(lambda self: self.fixture.global_scope and self.materialized and self.clean)
    def export_lock(self) -> None:
        before, state = self._capture(), self._state()
        for format_name in ("cyclonedx", "spdx"):
            result = self._run(
                ("lock", "export", "--global", "--format", format_name), f"export-{format_name}"
            )
            document = json.loads(result.stdout)
            assert document.get("bomFormat") == "CycloneDX" or document.get("spdxVersion")
            assert self.installed_commit in result.stdout, _result_evidence(result)
            _assert_same_state(state, self._state())
            assert_snapshot_set_unchanged(before, self._capture())

    @rule()
    @precondition(lambda self: self.fixture.global_scope and self.materialized and self.clean)
    def clean_cache(self) -> None:
        before, state = self._capture(), self._state()
        self._run(("cache", "clean", "--yes"), "clean-cache")
        _assert_same_state(state, self._state())
        assert_snapshot_set_unchanged(before, self._capture())

    @rule()
    @precondition(lambda self: self.fixture.global_scope and self.materialized and self.clean)
    def clean_sources(self) -> None:
        before, state = self._capture(), self._state()
        self._run(
            ("deps", "clean", "--dry-run"), "clean-preview", command_cwd=self.fixture.audit_cwd
        )
        assert_snapshot_set_unchanged(before, self._capture())
        self._run(("deps", "clean", "--yes"), "clean-sources", command_cwd=self.fixture.audit_cwd)
        assert not (self.project.root / "apm_modules").exists()
        assert self.skill_path.read_bytes() == (
            _SKILL_BYTES + f"\nrevision-{self.installed_revision}\n".encode("ascii")
        )
        result = self._run(
            ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json"),
            "audit-missing-sources",
            1,
            command_cwd=self.fixture.audit_cwd,
        )
        assert json.loads(result.stdout)["passed"] is False
        self._run(self.fixture.install_args, "rehydrate-sources")
        _assert_same_state(state, self._state())
        assert_snapshot_set_unchanged(before, self._capture())

    @rule()
    @precondition(lambda self: self.fixture.global_scope and self.materialized and self.clean)
    def legacy_update(self) -> None:
        before = self._capture()
        self._run(("deps", "update", "--global", "--parallel-downloads", "0"), "legacy-update")
        self.installed_revision = self.published_revision
        self.installed_commit = self.published_commit
        assert_snapshot_changes_within(
            before,
            self._capture(),
            exact_paths=self.fixture.exact_paths,
            tree_prefixes=self.fixture.tree_prefixes,
        )

    @rule()
    @precondition(lambda self: self.fixture.global_scope and self.declared and self.materialized)
    def update(self) -> None:
        before = self._capture()
        self._run(("update", "--global", "--yes", "--parallel-downloads", "0"), "update")
        assert_snapshot_changes_within(
            before,
            self._capture(),
            exact_paths=self.fixture.exact_paths,
            tree_prefixes=self.fixture.tree_prefixes,
        )
        self.installed_revision = self.published_revision
        self.installed_commit = self.published_commit
        self.clean = True

    @rule()
    @precondition(lambda self: self.fixture.global_scope and self.materialized)
    def compile(self) -> None:
        before = self._capture()
        self._run(("compile", "--global"), "compile")
        text = (self.fixture.external_roots[0].path / "CLAUDE.md").read_text(encoding="ascii")
        assert f"# revision-{self.installed_revision}" in text
        assert_snapshot_changes_within(
            before, self._capture(), exact_paths={"target": {"CLAUDE.md"}}, tree_prefixes={}
        )

    @rule()
    @precondition(
        lambda self: (
            self.fixture.global_scope
            and self.declared
            and self.materialized
            and self.clean
            and self.installed_commit == self.published_commit
        )
    )
    def lock(self) -> None:
        before, state = self._capture(), self._state()
        self._run(("lock", "--global", "--no-policy", "--parallel-downloads", "0"), "lock")
        after = self._state()
        assert after.deployment_records == state.deployment_records
        assert after.files == state.files
        assert_snapshot_changes_within(
            before,
            self._capture(),
            exact_paths=self._changed_path("apm.lock.yaml"),
            tree_prefixes={},
        )

    @rule()
    @precondition(
        lambda self: (
            self.fixture.global_scope and self.declared and self.materialized and self.clean
        )
    )
    def frozen_replay(self) -> None:
        before, state = self._capture(), self._state()
        self._run((*self.fixture.install_args, "--frozen"), "frozen-replay")
        _assert_same_state(state, self._state())
        assert_snapshot_set_unchanged(before, self._capture())

    @rule()
    @precondition(
        lambda self: (
            self.fixture.global_scope and self.declared and self.materialized and self.clean
        )
    )
    def frozen_refusal(self) -> None:
        manifest_bytes = self.project.manifest_path.read_bytes()
        self.fixture.replace_dependencies(({**self.fixture.dependency, "ref": "missing-ref"},))
        before, state = self._capture(), self._state()
        self._run((*self.fixture.install_args, "--frozen"), "frozen-refusal", 1)
        _assert_same_state(state, self._state())
        assert_snapshot_set_unchanged(before, self._capture())
        self.project.manifest_path.write_bytes(manifest_bytes)

    @rule()
    @precondition(lambda self: self.fixture.global_scope and self.materialized and self.clean)
    def uninstall(self) -> None:
        before = self._capture()
        self._run(("uninstall", "--global", self.fixture.source.remote_url), "uninstall")
        assert_snapshot_changes_within(
            before,
            self._capture(),
            exact_paths=self.fixture.exact_paths,
            tree_prefixes=self.fixture.tree_prefixes,
        )
        self.materialized = False
        self.declared = False
        self.locked = False

    @rule()
    @precondition(
        lambda self: self.fixture.global_scope and not self.materialized and not self.declared
    )
    def audit_empty(self) -> None:
        before = self._capture()
        _, payload = _audit_at(
            self.scenario,
            self.fixture.audit_cwd,
            environment=self.fixture.environment,
            scenario_id=self._next_id("audit-empty"),
        )
        assert payload["passed"] is True
        assert_snapshot_set_unchanged(before, self._capture())

    @invariant()
    def durable_state_matches_reference_model(self) -> None:
        manifest = yaml.safe_load(self.project.manifest_path.read_text(encoding="utf-8"))
        assert isinstance(manifest, dict)
        dependencies = manifest.get("dependencies", {}).get("apm", [])
        assert bool(dependencies) is self.declared
        assert self.skill_path.exists() is (self.materialized or self.retained_user_file)
        if self.retained_user_file:
            assert self.skill_path.read_bytes() == _TAMPER_BYTES
        if self.materialized and self.clean:
            expected = _SKILL_BYTES
            if self.fixture.global_scope:
                expected += f"\nrevision-{self.installed_revision}\n".encode()
            assert self.skill_path.read_bytes() == expected, (
                f"after {self.last_operation} step={self.step}; expected revision "
                f"{self.installed_revision} at {self.skill_path}"
            )
        if self.materialized and self.fixture.global_scope:
            lock = LockFile.read(self.project.root / "apm.lock.yaml")
            assert lock is not None
            dependencies = lock.get_package_dependencies()
            assert len(dependencies) == 1
            assert dependencies[0].resolved_commit == self.installed_commit
            modules = self.project.root / "apm_modules"
            assert (
                modules / "apm-fixture-org" / _PACKAGE_NAME / "skills" / _SKILL_NAME / "SKILL.md"
            ).is_file()
            assert not (modules / _PACKAGE_NAME).exists()
            assert self._state().deployment_records
            assert (
                self.fixture.external_roots[0].path / "user-owned.txt"
            ).read_bytes() == b"user-owned\n"
            if self.fixture.variant == "global-aliased":
                alias = self.scenario.isolated.root / "home-alias"
                assert alias.is_symlink()
                assert alias.readlink() == self.scenario.isolated.home
        assert (self.project.root / "apm.lock.yaml").exists() is self.locked

    def teardown(self) -> None:
        """Remove per-example roots after Hypothesis records the result."""
        shutil.rmtree(self.scenario.isolated.root)


@pytest.mark.parametrize("variant", _VARIANTS)
def test_generated_lifecycle_sequences_preserve_reference_model(
    tmp_path: Path,
    apm_binary_path: Path,
    variant: str,
) -> None:
    """Generate and shrink guarded transition sequences over a real CLI."""
    sequence = itertools.count()

    def factory() -> _LifecycleReferenceModel:
        case_root = tmp_path / f"case-{next(sequence):03d}"
        return _LifecycleReferenceModel(_ModelFixture.create(case_root, apm_binary_path, variant))

    run_state_machine_as_test(
        factory,
        settings=settings(
            database=None,
            deadline=None,
            derandomize=True,
            max_examples=6 if variant == "project" else 3,
            phases=(Phase.generate, Phase.shrink),
            print_blob=True,
            stateful_step_count=8,
            suppress_health_check=(HealthCheck.too_slow,),
        ),
    )


@pytest.mark.parametrize("variant", _VARIANTS)
def test_generated_lifecycle_mandatory_replay(
    tmp_path: Path, apm_binary_path: Path, variant: str
) -> None:
    """Exercise the model's guarded commands without relying on random selection."""
    model = _LifecycleReferenceModel(
        _ModelFixture.create(tmp_path / "mandatory", apm_binary_path, variant)
    )
    _mandatory_replay(model)


def _mandatory_replay(model: _LifecycleReferenceModel) -> None:
    operations = [
        model.dry_run,
        model.install,
        model.reinstall,
    ]
    if model.fixture.global_scope:
        operations.extend(
            [
                model.compile,
                model.lock,
                model.inspect_installed,
                model.export_lock,
                model.publish,
                model.outdated,
                model.frozen_replay,
                model.update,
                model.compile,
                model.export_lock,
                model.legacy_update,
                model.frozen_replay,
                model.frozen_refusal,
                model.reinstall,
                model.clean_cache,
                model.clean_sources,
            ]
        )
    operations.extend(
        [
            model.tamper,
            model.audit_tampered,
            model.repair,
            model.audit_clean,
        ]
    )
    if model.fixture.global_scope:
        operations.extend([model.uninstall, model.audit_empty])
    else:
        operations.extend([model.remove_declaration, model.prune_removed])
    operations.extend([model.readd_declaration, model.install, model.audit_clean])
    if model.fixture.global_scope:
        operations.extend([model.uninstall, model.audit_empty])
    else:
        operations.extend(
            [
                model.tamper,
                model.remove_declaration,
                model.prune_removed,
                model.readd_declaration,
                model.install,
                model.repair,
                model.audit_clean,
            ]
        )
    for operation in operations:
        transition = operation.hypothesis_stateful_rule
        assert all(condition(model) for condition in transition.preconditions)
        operation()
        model.durable_state_matches_reference_model()


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
