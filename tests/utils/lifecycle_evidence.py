"""Scoped fresh pytest observations, adapted from be73ed139.

No smoke deferral, synthetic evidence ingestion, or global pytest registration.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from scripts.lifecycle_contracts import EvidenceError, command_inventory, command_path
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.isolated_apm_environment import DURABLE_ENVIRONMENT_ROOTS


def fingerprint(path: Path) -> str:
    """Hash the actual source or executable bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(domain: dict[str, str]) -> str:
    """Observe all durable roots with the existing open-world snapshot authority."""
    roots: list[Path] = []
    candidates = {Path(value).resolve() for value in domain.values()}
    for candidate in sorted(candidates, key=lambda p: (len(p.parts), str(p))):
        if not any(candidate.is_relative_to(root) for root in roots):
            roots.append(candidate)
    captured = ArtifactSnapshotSet.capture({str(path): path for path in roots})
    payload = [
        (name, value.root_existed, [asdict(entry) for entry in value.entries])
        for name, value in captured.snapshots
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _root_values(env: dict[str, str]) -> dict[str, str]:
    """Include target-specific overrides alongside the isolation owner's roots."""
    return {
        key: env[key]
        for key in (*DURABLE_ENVIRONMENT_ROOTS, "CLAUDE_CONFIG_DIR", "HERMES_HOME")
        if env.get(key)
    }


def _physical_roots(cwd: Path, env: dict[str, str]) -> dict[str, str]:
    """Resolve overrides exactly in the child HOME, not the observer's HOME."""
    expanded_keys = {"CLAUDE_CONFIG_DIR", "HERMES_HOME", "APM_CACHE_DIR", "XDG_CACHE_HOME"}
    values = {
        key: value.strip() if key in expanded_keys else value
        for key, value in _root_values(env).items()
    }
    tilde = {k: v for k, v in values.items() if k in expanded_keys and v.startswith("~")}
    if tilde:
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, sys; from pathlib import Path; "
                "print(json.dumps({k: str(Path(v).expanduser()) "
                "for k, v in json.load(sys.stdin).items()}))",
            ],
            input=json.dumps(tilde),
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if probe.returncode:
            raise EvidenceError("Child durable root expansion failed")
        try:
            expanded = json.loads(probe.stdout)
        except json.JSONDecodeError as exc:
            raise EvidenceError("Child durable root expansion produced invalid output") from exc
        if (
            not isinstance(expanded, dict)
            or expanded.keys() != tilde.keys()
            or any(not isinstance(value, str) or not value for value in expanded.values())
        ):
            raise EvidenceError("Child durable root expansion produced invalid paths")
        values.update(expanded)
    return {key: str((cwd / value).resolve()) for key, value in values.items()}


class LifecycleEvidencePlugin:
    """Observe exact collected nodes, genuine Hypothesis calls and runner subprocesses."""

    def __init__(
        self,
        nodeids: list[str],
        executable: Path | None,
        contexts: dict[str, set[str]] | None = None,
        candidate_source: Path | None = None,
    ) -> None:
        import apm_cli

        self.nodeids = set(nodeids)
        self.contexts = contexts or {}
        self.candidate_source = (candidate_source or Path(apm_cli.__file__).parent).resolve()
        self.source_hash = fingerprint(self.candidate_source / "cli.py")
        self.executable = executable.absolute() if executable else None
        self.executable_hash = fingerprint(self.executable) if self.executable else None
        self.python_hash = fingerprint(Path(sys.executable).resolve())
        self.inventory = command_inventory()
        self.records: dict[str, dict[str, Any]] = {}
        self.active: str | None = None
        self.phase: str | None = None
        self.model_run: int | None = None
        self.domains: dict[tuple[str, int | None, str], dict[str, str]] = {}
        self.patch = pytest.MonkeyPatch()

    def _source_identity(
        self,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        entrypoint: tuple[str, ...] | None = None,
    ) -> dict[str, str]:
        """Probe actual child import resolution, accounting for script versus -m."""
        path_head = (
            self.executable.parent
            if self.executable and entrypoint == (str(self.executable),)
            else cwd.resolve()
        )
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; "
                "sys.path[:] = sys.path if getattr(sys.flags, 'safe_path', False) "
                "else [sys.argv[1], *sys.path[1:]]; "
                "import hashlib, importlib, importlib.util, json; from pathlib import Path; "
                "package = importlib.import_module('apm_cli'); "
                "spec = importlib.util.find_spec('apm_cli.cli'); path = Path(spec.origin).resolve(); "
                "print(json.dumps({'package': str(Path(package.__file__).resolve().parent), "
                "'cli': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), "
                "'python': str(Path(sys.executable).resolve()), "
                "'python_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()}))",
                str(path_head),
            ],
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=min(timeout, 30),
            check=False,
        )
        if probe.returncode:
            raise EvidenceError(f"Child source identity probe failed: exit {probe.returncode}")
        try:
            source = json.loads(probe.stdout)
        except json.JSONDecodeError as exc:
            raise EvidenceError("Child source identity probe produced invalid output") from exc
        expected = {
            "package": str(self.candidate_source),
            "cli": str(self.candidate_source / "cli.py"),
            "sha256": self.source_hash,
            "python": str(Path(sys.executable).resolve()),
            "python_sha256": self.python_hash,
        }
        if source != expected:
            raise EvidenceError("Child source identity does not match the candidate checkout")
        return source

    def _domain(self, cwd: Path, env: dict[str, str]) -> tuple[dict[str, str], dict[str, str], str]:
        """Pin lexical and physical roots across explicitly reviewed cwd changes."""
        if self.active is None:
            raise EvidenceError("Lifecycle context requires an active test node")
        environment = _root_values(env)
        if not {"HOME", "APM_HOME"} <= environment.keys():
            raise EvidenceError("Lifecycle requires isolated HOME and APM_HOME")
        physical = _physical_roots(cwd, env)
        identity = json.dumps([environment, physical], sort_keys=True)
        key = (self.active, self.model_run, identity)
        location = str(cwd.resolve())
        if key not in self.domains:
            if any(
                node == self.active
                and model == self.model_run
                and location in {roots["workspace"], roots.get("APM_HOME")}
                for (node, model, _identity), roots in self.domains.items()
            ):
                raise EvidenceError("Lifecycle durable roots changed for the same caller workspace")
            self.domains[key] = {"workspace": location, **physical}
        roots = self.domains[key]
        context = "initial"
        if location != roots["workspace"]:
            if location != roots.get("APM_HOME"):
                raise EvidenceError(f"Unreviewed lifecycle command cwd: {cwd}")
            context = "APM_HOME"
        if context not in self.contexts.get(self.active, set()) | {"initial"}:
            raise EvidenceError(f"Undeclared lifecycle command context: {context}")
        return roots, environment, context

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        """Install observers before test-module imports bind runner/model functions."""
        # Resolve through sys.modules: `from hypothesis import stateful` can return a
        # stale package attribute after pytester restores sys.modules, leaving the
        # module that test code imports unpatched.
        stateful = importlib.import_module("hypothesis.stateful")

        original = ApmLifecycleRunner._run_with_timeout
        model = stateful.run_state_machine_as_test

        def observe(runner: ApmLifecycleRunner, args: Any, **kwargs: Any) -> Any:
            if self.active not in self.nodeids or self.phase != "call":
                return original(runner, args, **kwargs)
            command = runner._command
            if command != (sys.executable, "-m", "apm_cli.cli") and not (
                self.executable and command == (str(self.executable),)
            ):
                raise EvidenceError(f"Unverified source executable: {command}")
            if (
                self.executable and fingerprint(self.executable) != self.executable_hash
            ) or fingerprint(Path(sys.executable).resolve()) != self.python_hash:
                raise EvidenceError("Executable changed during evidence execution")
            cwd, env = kwargs["cwd"], kwargs["env"]
            roots, environment, context = self._domain(cwd, env)
            source = self._source_identity(cwd, env, kwargs["timeout_seconds"], command)
            before = snapshot(roots)
            result = original(runner, args, **kwargs)
            if _physical_roots(cwd, env) != {key: roots[key] for key in environment}:
                raise EvidenceError("Lifecycle durable root identity changed during command")
            self.records[self.active]["events"].append(
                {
                    "command": command_path(list(args), self.inventory),
                    "args": list(args),
                    "returncode": result.returncode,
                    "cwd": str(cwd.resolve()),
                    "roots": roots,
                    "environment": environment,
                    "context": context,
                    "source": source,
                    "before": before,
                    "after": snapshot(roots),
                    "scenario_id": kwargs["scenario_id"],
                    "entrypoint": list(command),
                    "model": self.model_run,
                }
            )
            return result

        def observe_model(*args: Any, **kwargs: Any) -> Any:
            if self.active not in self.nodeids or self.phase != "call":
                return model(*args, **kwargs)
            previous = self.model_run
            self.model_run = self.records[self.active]["models"] + 1
            try:
                result = model(*args, **kwargs)
                self.records[self.active]["models"] += 1
                return result
            finally:
                self.model_run = previous

        self.patch.setattr(ApmLifecycleRunner, "_run_with_timeout", observe)
        self.patch.setattr(stateful, "run_state_machine_as_test", observe_model)

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        """Fail on extra, duplicate, missing or deselected final collected nodes."""
        actual = [item.nodeid for item in session.items]
        if len(actual) != len(self.nodeids) or set(actual) != self.nodeids:
            raise pytest.UsageError("Lifecycle exact collection differs from required witnesses")
        for item in session.items:
            callspec = getattr(item, "callspec", None)
            self.records[item.nodeid] = {
                "collected": True,
                "dimensions": dict(callspec.params) if callspec else {},
                "phases": {},
                "events": [],
                "models": 0,
            }

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None) -> Any:
        """Associate fixture execution with its exact collected node."""
        self.active = item.nodeid
        yield
        self.active, self.phase = None, None

    def pytest_runtest_setup(self, item: pytest.Item) -> None:
        self.phase = "setup"

    def pytest_runtest_call(self, item: pytest.Item) -> None:
        self.phase = "call"

    def pytest_runtest_teardown(self, item: pytest.Item, nextitem: pytest.Item | None) -> None:
        self.phase = "teardown"

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.nodeid in self.records:
            outcome = "xfail" if hasattr(report, "wasxfail") else report.outcome
            phases = self.records[report.nodeid]["phases"]
            phases[report.when] = "repeated" if report.when in phases else outcome

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        """Restore instrumentation after both success and failure."""
        self.patch.undo()
