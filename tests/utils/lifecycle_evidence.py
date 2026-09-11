"""Scoped pytest observer for freshly executed lifecycle contract witnesses."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from scripts.lifecycle_contracts import (
    EvidenceError,
    candidate_contracts,
    command_inventory,
    command_path,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.isolated_apm_environment import DURABLE_ENVIRONMENT_ROOTS


def pytest_addoption(parser: pytest.Parser) -> None:
    """Explicit candidate arguments enable smoke-only deferral, never proof reuse."""
    parser.addoption("--lifecycle-defer-base")
    parser.addoption("--lifecycle-defer-head")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Defer exact deterministic nodes to the mandatory subsequent native gate."""
    base = config.getoption("lifecycle_defer_base")
    head = config.getoption("lifecycle_defer_head")
    if base is None and head is None:
        return
    if not base or not head:
        raise pytest.UsageError("Lifecycle smoke deferral requires both base and head")
    from scripts.check_lifecycle_evidence import candidate

    try:
        root = Path(config.rootpath)
        identity = candidate(root, base, head)
        _, contracts = candidate_contracts(
            root, identity["base"], identity["head"], command_inventory()
        )
    except (EvidenceError, KeyError, TypeError, ValueError, subprocess.CalledProcessError) as exc:
        raise pytest.UsageError(f"Invalid lifecycle smoke deferral: {exc}") from exc
    deferred = {
        witness["nodeid"]
        for contract in contracts
        for witness in contract["witnesses"]
        if witness["kind"] == "deterministic"
    }
    removed = [item for item in items if item.nodeid in deferred]
    items[:] = [item for item in items if item.nodeid not in deferred]
    config.hook.pytest_deselected(items=removed)


def fingerprint(path: Path) -> str:
    """Hash an executable or source artifact without trusting its filename."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(domain: dict[str, str]) -> str:
    """Use the existing open-world oracle for project and isolated user roots."""
    candidates = [Path(value) for value in domain.values()]
    roots: list[Path] = []
    for candidate in sorted({path.resolve() for path in candidates}, key=lambda p: len(p.parts)):
        if not any(candidate.is_relative_to(root) for root in roots):
            roots.append(candidate)
    captured = ArtifactSnapshotSet.capture({str(path): path for path in roots})
    payload = [
        (name, value.root_existed, [asdict(entry) for entry in value.entries])
        for name, value in captured.snapshots
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _physical_roots(cwd: Path, env: dict[str, str]) -> dict[str, str]:
    """Resolve overrides like the child, without mutating the observer's environment."""
    expanded_keys = {"CLAUDE_CONFIG_DIR", "HERMES_HOME", "APM_CACHE_DIR", "XDG_CACHE_HOME"}
    values = {
        key: env[key].strip() if key in expanded_keys else env[key]
        for key in DURABLE_ENVIRONMENT_ROOTS
        if env.get(key)
    }
    tilde = {
        key: value
        for key, value in values.items()
        if key in expanded_keys and value.startswith("~")
    }
    if tilde:
        # Only tilde roots need child HOME/USERPROFILE and platform account lookup.
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
        if probe.returncode != 0:
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
    """Observe exact nodes and runner invocations, never authored outcome strings."""

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
        self.executable = executable.resolve() if executable else None
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
        """Probe actual child import resolution; isolated fixtures replace PYTHONPATH."""
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
                "spec = importlib.util.find_spec('apm_cli.cli'); "
                "path = Path(spec.origin).resolve(); "
                "print(json.dumps({'package': str(Path(package.__file__).resolve().parent), "
                "'cli': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}))",
                str(path_head),
            ],
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=min(timeout, 30),
            check=False,
        )
        if probe.returncode != 0:
            raise EvidenceError(f"Child source identity probe failed: exit {probe.returncode}")
        try:
            source = json.loads(probe.stdout)
        except json.JSONDecodeError as exc:
            raise EvidenceError("Child source identity probe produced invalid output") from exc
        expected = {
            "package": str(self.candidate_source),
            "cli": str(self.candidate_source / "cli.py"),
            "sha256": self.source_hash,
        }
        if source != expected:
            raise EvidenceError("Child source identity does not match the candidate checkout")
        return source

    def _domain(self, cwd: Path, env: dict[str, str]) -> tuple[dict[str, str], dict[str, str], str]:
        """Pin an isolated domain while allowing only reviewed command contexts."""
        if self.active is None:
            raise EvidenceError("Lifecycle context requires an active test node")
        environment = {key: env[key] for key in DURABLE_ENVIRONMENT_ROOTS if env.get(key)}
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
        from hypothesis import stateful

        original = ApmLifecycleRunner._run_with_timeout
        model = stateful.run_state_machine_as_test

        def observe(runner: ApmLifecycleRunner, args: Any, **kwargs: Any) -> Any:
            if self.active not in self.nodeids or self.phase != "call":
                return original(runner, args, **kwargs)
            command = runner._command
            source_command = (sys.executable, "-m", "apm_cli.cli")
            if command != source_command and not (
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
            after = snapshot(roots)
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
                    "after": after,
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
        """Final selection, not an AST function lookup or pre-deselection inventory."""
        for item in session.items:
            if item.nodeid not in self.nodeids:
                continue
            callspec = getattr(item, "callspec", None)
            self.records[item.nodeid] = {
                "collected": True,
                "dimensions": dict(callspec.params) if callspec else {},
                "phases": {},
                "durations": {},
                "events": [],
                "models": 0,
            }

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None) -> Any:
        """Keep fixture execution associated with its exact collected node."""
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
            self.records[report.nodeid]["phases"][report.when] = outcome
            self.records[report.nodeid]["durations"][report.when] = report.duration

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        """Restore instrumentation even when collection or test setup fails."""
        self.patch.undo()
