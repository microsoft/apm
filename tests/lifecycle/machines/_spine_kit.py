"""M-SPINE: the install, audit and repair reference-model machine (#4040, #4098).

Moved from ``tests/integration/test_generated_lifecycle_state_machine.py``
onto the lifecycle family core. Every APM command, including those the
shared scenario helpers run, goes through ``tests/lifecycle/driver.py``
(engine on PR, binary on the nightly) and then ``check_cross_cutting``,
so each step keeps I1-I9 and emits receipts. The per-rule snapshot oracles
and the reference-model invariant are unchanged.

Variants: ``project``, ``global-canonical`` and ``global-aliased``
(``HOME`` reached through a symlink). The mandatory spine replays once per
world; generated examples restore that post-spine world (#4040).
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import yaml
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from apm_cli.core.deployment_ledger import DeploymentMaterializationSession
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_frozen_no_rewrite_e2e import (
    _add_skill,
    _assert_frozen_refusal_and_recovery,
)
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
from tests.lifecycle import driver
from tests.lifecycle.oracles import (
    ExpectedCell,
    Observation,
    Operation,
    check_cross_cutting,
    operation_for,
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
from tests.utils.world_snapshot import restore_tree

_SKILL_NAME = "model-skill"
_PACKAGE_NAME = "model-kit"
_SKILL_BYTES = _skill(_SKILL_NAME).encode()
_TAMPER_BYTES = b"# user tamper\n"
_IDENTITY_WRITE_PATHS = frozenset(
    {".apm-local", ".apm-local/.gitignore", ".apm-local/installation"}
)
_PROJECT_WRITE_PATHS = frozenset(
    {
        *_IDENTITY_WRITE_PATHS,
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
        # filelock >= 4 keeps the released OS-user lifecycle lock file on disk.
        ".apm/.apm-lifecycle.lock",
        # The OS-user deployment-root lock serializes every APM_HOME.
        ".apm-local",
        ".apm-local/.apm-lifecycle.lock",
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
_LEDGER_PATH = Path(__file__).parents[2] / "fixtures" / "lifecycle_bug_ledger.json"
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
    "install_unlocked_revision": frozenset({"source.ref_cache_coherent"}),
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
    "frozen_underrecording_recovery": frozenset(
        {"lockfile.frozen_preserves_bytes", "outcome.status_matches_state"}
    ),
    "uninstall": frozenset({"ownership.preserve_unowned"}),
    "audit_empty": frozenset({"outcome.status_matches_state"}),
    "inspect_installed": frozenset({"filesystem.open_world_observation"}),
    "export_lock": frozenset({"source.ref_cache_coherent"}),
    "clean_sources": frozenset({"source.ref_cache_coherent", "ownership.preserve_unowned"}),
    "clean_cache": frozenset({"filesystem.open_world_observation"}),
    "legacy_update": frozenset({"source.ref_cache_coherent"}),
}
_VARIANTS = ("project", "global-canonical", "global-aliased")
# Run-level expected cell: a preserved user edit of a managed file (see expected_cells).
USER_EDIT_CELL = "spine-user-edit-preserved"


def _phase_one_properties() -> frozenset[str]:
    ledger = json.loads(_LEDGER_PATH.read_text(encoding="ascii"))
    return frozenset(row["id"] for row in ledger["property_catalog"] if row["phase"] == 1)


_PHASE_ONE_PROPERTIES = _phase_one_properties()


# Commands that never change durable state in this machine (I5 read-only).
_READ_ONLY_COMMANDS = frozenset(
    {
        "audit",
        "outdated",
        "view",
        "info",
        "targets",
        "find",
        "deps list",
        "deps tree",
        "deps why",
        "deps info",
        "lock export",
        "cache info",
    }
)
_GROUPED_COMMANDS = frozenset({"deps", "lock", "cache"})


@dataclass(frozen=True)
class _At:
    """The driver world for one command: its cwd and environment."""

    project: Path
    env: dict[str, str]


class _DriverRunner:
    """``scenario.runner`` of the shared helpers, routed through the family driver.

    While a machine is bound, every command (including the ones the shared
    ``tests/integration`` helpers run) is observed and checked by
    ``check_cross_cutting``.
    """

    def __init__(self, entry_point: Sequence[str]) -> None:
        self.entry_point = tuple(entry_point)
        self.machine: _LifecycleReferenceModel | None = None

    def run(
        self,
        args: Sequence[str],
        *,
        scenario_id: str = "single-command",
        cwd: Path,
        env: Mapping[str, str],
    ) -> CommandResult:
        del scenario_id
        if self.machine is not None:
            return self.machine.checked_run(tuple(args), cwd=cwd, env=env)
        return driver.run(_At(cwd, dict(env)), tuple(args), entry_point=self.entry_point)


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
    def create(
        cls, root: Path, entry_point: Sequence[str], variant: str = "project"
    ) -> _ModelFixture:
        fixture = cls._build(root, entry_point, variant)
        # Hermetic world: no update check (it would write ~/.cache/apm and try the network).
        fixture.environment["APM_E2E_TESTS"] = "1"
        return fixture

    @classmethod
    def _build(cls, root: Path, entry_point: Sequence[str], variant: str) -> _ModelFixture:
        scenario = dataclasses.replace(
            _new_scenario(root, Path(entry_point[0])), runner=_DriverRunner(entry_point)
        )
        published = _publish(scenario, _PACKAGE_NAME, skill=_SKILL_NAME)
        if variant != "project":
            commit_a = _publish_revision(scenario, published, "a")
            remote_url = f"https://code.example.com/apm-fixture-org/{_PACKAGE_NAME}"
            dependency = {**published.dependency, "git": remote_url, "ref": "main"}
            environment = scenario.repositories.url_rewrite_subprocess_env(
                published.repository, remote_url
            )
            for setting in ("GITHUB_HOST", "GITLAB_HOST", "APM_GITLAB_HOSTS"):
                environment.pop(setting, None)
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
                remote_url,
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
            home = self.scenario.isolated.home
            with patch("apm_cli.core.deployment_ledger.Path.home", return_value=home):
                receipt = DeploymentMaterializationSession(
                    self.project.root, InstallScope.PROJECT, None
                ).state_path
            return {
                **_INSTALL_EXACT_PATHS,
                "user": _USER_WRITE_PATHS
                | {
                    receipt.parent.relative_to(home).as_posix(),
                    receipt.relative_to(home).as_posix(),
                },
            }
        return {
            "user": _USER_WRITE_PATHS | _IDENTITY_WRITE_PATHS,
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
        self.local_skill_bytes: bytes | None = None
        self.step = 0
        self.last_operation = "initial"
        self.published_revision = "a"
        self.installed_revision = "a"
        self.published_commit = fixture.source.commit.sha
        self.installed_commit = fixture.source.commit.sha
        self.converged = False
        self.expected_seen: set[str] = set()
        fixture.scenario.runner.machine = self

    @property
    def scenario(self) -> _Scenario:
        return self.fixture.scenario

    @property
    def project(self) -> LocalPackage:
        return self.fixture.project

    @property
    def skill_path(self) -> Path:
        return self.fixture.skill_path

    # -- family oracle -------------------------------------------------------

    @property
    def scope(self) -> str:
        return "user" if self.fixture.global_scope else "project"

    @property
    def deploy_root(self) -> Path:
        if self.fixture.global_scope:
            return self.fixture.external_roots[0].path
        return self.project.root

    def _managed_skill(self) -> bool:
        """The deployed skill carries APM content (user bytes are not managed)."""
        return self.skill_path.is_file() and self.skill_path.read_bytes().startswith(_SKILL_BYTES)

    def observe(self) -> Observation:
        managed = self._managed_skill()
        return Observation(
            state=self._state(),
            artifacts=self._capture(),
            deployed=frozenset({"skill"}) if managed else frozenset(),
            deploy_root=self.deploy_root,
            lock=LockFile.read(self.project.root / "apm.lock.yaml"),
            managed_paths=frozenset({self._skill_record()}) if managed else frozenset(),
        )

    def _skill_record(self) -> str:
        """How the lock names the skill: project-relative, or absolute for --global."""
        if self.fixture.global_scope:
            return self.skill_path.as_posix()
        return self.skill_path.relative_to(self.deploy_root).as_posix()

    def allowed_surfaces(self) -> frozenset[str]:
        return frozenset({"skill"}) if self.declared else frozenset()

    def converged_before(self) -> bool:
        return self.converged

    def expected_cells(self, op, before, after, result) -> Mapping[str, tuple[ExpectedCell, ...]]:
        """I1 cannot hold while a user edit of the managed skill is preserved.

        By design the lock keeps the deployed hash (``audit --ci`` then reports
        the drift; ``audit_tampered`` asserts it), so only the edited file's
        hash mismatch is excused, and only while the edit is on disk.
        """
        del op, before, result
        if not self.skill_path.is_file() or self._managed_skill():
            return {}
        del after
        relative = re.escape(self._skill_record())
        return {
            "I1": (
                ExpectedCell(
                    USER_EDIT_CELL, f"^recorded hash does not match disk: {relative}$", False
                ),
            )
        }

    def record_expected(self, cell_id: str) -> None:
        self.expected_seen.add(cell_id)

    def _operation(self, args: tuple[str, ...]) -> Operation:
        op = operation_for(args, scope=self.scope)
        key = " ".join(args[:2]) if args[0] in _GROUPED_COMMANDS else args[0]
        if key in _READ_ONLY_COMMANDS or "DRYRUN" in op.modifiers:
            return dataclasses.replace(op, read_only=True)
        if self.fixture.global_scope:
            # A --global command never writes the directory it runs in.
            return dataclasses.replace(op, forbidden={"project": ("*",)})
        user_paths = tuple(sorted(self.fixture.exact_paths["user"]))
        return dataclasses.replace(op, forbidden={"user": ("*",)}, allowed={"user": user_paths})

    def checked_run(
        self, args: tuple[str, ...], *, cwd: Path, env: Mapping[str, str]
    ) -> CommandResult:
        """Run one command through the driver and check I1-I9 around it."""
        before = self.observe()
        result = driver.run(_At(cwd, dict(env)), args, entry_point=self.scenario.runner.entry_point)
        check_cross_cutting(before, self.observe(), result, self, self._operation(args))
        self.converged = False
        return result

    def _run(
        self,
        args: tuple[str, ...],
        operation: str,
        expected_returncode: int = 0,
        *,
        command_cwd: Path | None = None,
        converged: bool = False,
    ) -> CommandResult:
        # I2 applies only where the model knows the step repeats a converged state.
        self.converged = converged
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
        before, state = self._capture(), self._state()
        result = self._run(
            self.fixture.install_args,
            "install",
            expected_returncode=1 if self.retained_user_file else 0,
        )
        assert_snapshot_changes_within(
            before,
            self._capture(),
            exact_paths=self.fixture.exact_paths,
            tree_prefixes=self.fixture.tree_prefixes,
        )
        if self.retained_user_file:
            assert "conflicts with unmanaged file" in " ".join(result.stdout.split())
            assert self.skill_path.read_bytes() == _TAMPER_BYTES
            _assert_same_state(state, self._state())
            return
        self.materialized = True
        self.clean = not self.retained_user_file
        self.locked = True
        self.installed_revision = self.published_revision
        self.installed_commit = self.published_commit

    @rule()
    @precondition(
        lambda self: (
            self.fixture.global_scope
            and self.declared
            and not self.materialized
            and self.published_revision == "b"
        )
    )
    def install_unlocked_revision(self) -> None:
        """A sibling virtual-package lock cannot authorize the removed revision's cache."""
        before = self._capture()
        commit = _publish_revision(self.scenario, self.fixture.source, "c")
        self.published_revision = "c"
        self.published_commit = commit.sha
        assert_snapshot_set_unchanged(before, self._capture())
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(
                repo_url=f"apm-fixture-org/{_PACKAGE_NAME}",
                host="code.example.com",
                virtual_path="skills/sibling",
                is_virtual=True,
                resolved_ref="main",
                resolved_commit=self.installed_commit,
            )
        )
        lock.save(self.project.root / "apm.lock.yaml")
        self.install()
        assert self._state().deployment_records

    @rule()
    @precondition(lambda self: self.declared and self.materialized and not self.retained_user_file)
    def reinstall(self) -> None:
        before, state = self._capture(), self._state()
        if not self.clean:
            assert any(
                record.content_hash is not None
                and record.locator.value.endswith(f"/{_SKILL_NAME}/SKILL.md")
                for record in state.deployment_records
            )
        self._run(self.fixture.install_args, "reinstall", converged=True)
        assert_snapshot_set_unchanged(before, self._capture())
        if not self.clean:
            assert self.skill_path.read_bytes() == _TAMPER_BYTES
            assert self._state().deployment_records == state.deployment_records, (
                "Ordinary reinstall must preserve the original deployment hashes"
            )

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
    @precondition(
        lambda self: (
            self.declared and (self.retained_user_file or (self.materialized and not self.clean))
        )
    )
    def repair(self) -> None:
        before = self._capture()
        self._run((*self.fixture.install_args, "--force"), "repair")
        assert_snapshot_changes_within(
            before,
            self._capture(),
            exact_paths=self.fixture.exact_paths,
            tree_prefixes=self.fixture.tree_prefixes,
        )
        if not self.materialized:
            self.materialized = True
            self.locked = True
            self.installed_revision = self.published_revision
            self.installed_commit = self.published_commit
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
            exact_paths=self.fixture.exact_paths,
            tree_prefixes=self.fixture.tree_prefixes,
        )
        self.materialized = False
        self.retained_user_file = retained_bytes is not None
        if retained_bytes is not None:
            assert self.skill_path.read_bytes() == retained_bytes
        self.clean = True
        self.locked = self.local_skill_bytes is not None

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
        # apm find keeps stdout for package names; errors go to stderr (#3994).
        assert "is not tracked by any installed package" in " ".join(found.stderr.split())
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
        revision_changes = self.installed_commit != self.published_commit
        self._run(("update", "--global", "--yes", "--parallel-downloads", "0"), "update")
        assert_snapshot_changes_within(
            before,
            self._capture(),
            exact_paths=self.fixture.exact_paths,
            tree_prefixes=self.fixture.tree_prefixes,
        )
        self.installed_revision = self.published_revision
        self.installed_commit = self.published_commit
        # An update with nothing newer is a no-op and keeps local edits.
        if revision_changes:
            self.clean = True

    @rule()
    @precondition(lambda self: self.fixture.global_scope and self.materialized)
    def compile(self) -> None:
        before = self._capture()
        self._run(("compile", "--global"), "compile", converged=True)
        target_root = self.fixture.external_roots[0].path
        native = target_root / "rules" / "revision.md"
        assert native.read_text(encoding="ascii") == f"# revision-{self.installed_revision}\n"
        assert not (target_root / "CLAUDE.md").exists()
        assert_snapshot_set_unchanged(before, self._capture())

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
        self._run((*self.fixture.install_args, "--frozen"), "frozen-replay", converged=True)
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
        self.fixture.replace_dependencies(({**self.fixture.dependency, "type": "gitlab"},))
        before, state = self._capture(), self._state()
        refused = self._run((*self.fixture.install_args, "--frozen"), "frozen-provider-refusal", 1)
        assert "host provider" in " ".join(refused.stdout.split()), _result_evidence(refused)
        _assert_same_state(state, self._state())
        assert_snapshot_set_unchanged(before, self._capture())
        self.project.manifest_path.write_bytes(manifest_bytes)
        before, state = self._capture(), self._state()
        self._run((*self.fixture.install_args, "--frozen"), "frozen-provider-recovery")
        _assert_same_state(state, self._state())
        assert_snapshot_set_unchanged(before, self._capture())

    @rule()
    @precondition(
        lambda self: (
            not self.fixture.global_scope
            and self.declared
            and self.materialized
            and self.clean
            and self.local_skill_bytes is None
        )
    )
    def frozen_underrecording_recovery(self) -> None:
        """Exercise post-deployment refusal, not just preflight pin mismatches."""
        before = self._capture()
        source = _add_skill(self.project.root, "frozen-extra")
        deployed_path = PurePosixPath(".agents/skills/frozen-extra")
        _assert_frozen_refusal_and_recovery(
            self.scenario, self.project, self.fixture.environment, deployed_path
        )
        self.local_skill_bytes = source.read_bytes()
        assert_snapshot_changes_within(
            before,
            self._capture(),
            exact_paths={
                **self.fixture.exact_paths,
                "project": self.fixture.exact_paths["project"] | {".apm", ".apm/skills"},
            },
            tree_prefixes={
                "project": self.fixture.tree_prefixes["project"]
                | {".apm/skills/frozen-extra", ".agents/skills/frozen-extra"},
            },
        )

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
        if self.local_skill_bytes is not None:
            for relative in (
                ".apm/skills/frozen-extra/SKILL.md",
                ".agents/skills/frozen-extra/SKILL.md",
            ):
                assert (self.project.root / relative).read_bytes() == self.local_skill_bytes
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
            assert self.fixture.dependency["alias"] == _PACKAGE_NAME
            assert dependencies[0].alias == _PACKAGE_NAME
            assert (
                LockedDependency.from_dict(dependencies[0].to_dict()).to_dependency_ref().alias
                == _PACKAGE_NAME
            )
            modules = self.project.root / "apm_modules"
            assert (
                modules / _PACKAGE_NAME / "skills" / _SKILL_NAME / "SKILL.md"
            ).read_bytes() == _SKILL_BYTES + f"\nrevision-{self.installed_revision}\n".encode()
            assert not (modules / "apm-fixture-org" / _PACKAGE_NAME).exists()
            assert self._state().deployment_records
            assert (
                self.fixture.external_roots[0].path / "user-owned.txt"
            ).read_bytes() == b"user-owned\n"
            if self.fixture.variant == "global-aliased":
                alias = self.scenario.isolated.root / "home-alias"
                assert alias.is_symlink()
                assert alias.readlink() == self.scenario.isolated.home
        assert (self.project.root / "apm.lock.yaml").exists() is self.locked


_REFERENCE_FIELDS = (
    "declared",
    "materialized",
    "clean",
    "locked",
    "retained_user_file",
    "local_skill_bytes",
    "step",
    "last_operation",
    "published_revision",
    "installed_revision",
    "published_commit",
    "installed_commit",
)


@dataclass(frozen=True)
class _SpineWorld:
    """One world per variant: the mandatory spine replays once, then each example restores it.

    The world is restored at its original absolute path, so every environment
    value, Git URL rewrite and symlink recorded during the spine stays valid.
    """

    fixture: _ModelFixture
    pristine: Path
    reference: tuple[tuple[str, object], ...]

    @classmethod
    def replay(
        cls, root: Path, entry_point: Sequence[str], variant: str
    ) -> tuple[_SpineWorld, _LifecycleReferenceModel]:
        model = _LifecycleReferenceModel(_ModelFixture.create(root, entry_point, variant))
        mandatory_replay(model)
        pristine = root.parent / f"{root.name}-pristine"
        shutil.copytree(model.scenario.isolated.root, pristine, symlinks=True)
        reference = tuple((name, getattr(model, name)) for name in _REFERENCE_FIELDS)
        return cls(model.fixture, pristine, reference), model

    def restore(self) -> _LifecycleReferenceModel:
        restore_tree(self.pristine, self.fixture.scenario.isolated.root)
        model = _LifecycleReferenceModel(self.fixture)
        for name, value in self.reference:
            setattr(model, name, value)
        return model


def mandatory_replay(model: _LifecycleReferenceModel) -> None:
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
    else:
        operations.append(model.frozen_underrecording_recovery)
    operations.extend(
        [
            model.tamper,
            model.reinstall,
            model.audit_tampered,
            model.repair,
            model.audit_clean,
        ]
    )
    if model.fixture.global_scope:
        operations.extend([model.uninstall, model.audit_empty])
    else:
        operations.extend([model.remove_declaration, model.prune_removed])
    operations.extend(
        [
            model.readd_declaration,
            model.install_unlocked_revision if model.fixture.global_scope else model.install,
            model.audit_clean,
        ]
    )
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
