"""Scoped fresh pytest observations, adapted from be73ed139.

No smoke deferral, synthetic evidence ingestion, or global pytest registration.
"""

from __future__ import annotations

import hashlib
import importlib
import itertools
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest

from scripts.lifecycle_contracts import EvidenceError, command_inventory, command_path
from tests.lifecycle.model.expected import exit_contract
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


# ---------------------------------------------------------------------------
# Global output oracle (#4113): I6, I8 and I9 on every APM invocation.
#
# ``ApmLifecycleRunner.run`` and ``run_sequence`` pass every result of an APM
# entry point through ``check_cli_output``. A violation fails the command
# unless a STRICT known cell (``KNOWN_OUTPUT_CELLS``) owns it or the test is
# itself about that violation (``expect_output_violation``).
# ---------------------------------------------------------------------------

_TRACEBACK = "Traceback (most recent call last)"
_POLICY_CACHE_AGE = re.compile(r"fetched \d+[smhd] ago")
_STATUS_PREFIXES = ("[+]", "[!]", "[x]", "[i]", "[*]", "[>]")
_TREE_PREFIXES = ("|--", "+--", "`--", "\\--")
# The narrowest console width Rich wraps at when stdout is not a terminal.
_WRAP_WIDTH = 79
# Starts of a printed line (status, tree or box row) as opposed to a wrap continuation.
_LINE_OPENERS = (*_STATUS_PREFIXES, "|", "+", "`", "\\")
_ALLOWED_CONTROL = frozenset("\n\r\t")


@dataclass(frozen=True)
class OutputViolation:
    law: str
    message: str


@dataclass(frozen=True)
class KnownOutputCell:
    """A STRICT expected output failure of the product, tracked by *issue*.

    Where ``applies(args, scenario_id)`` holds and stdout matches *output*
    (a multiline regex naming the output shape that triggers the defect), a
    violation of *law* whose message matches *message* is excused; if none
    occurs the cell fails, because the defect is fixed and the cell must be
    removed.
    """

    id: str
    law: str
    issue: str
    applies: Callable[[tuple[str, ...], str], bool]
    message: str
    output: str = ""

    def covers(self, args: tuple[str, ...], scenario_id: str, stdout: str) -> bool:
        return self.applies(args, scenario_id) and (
            not self.output or re.search(self.output, stdout, re.MULTILINE | re.DOTALL) is not None
        )


def _runs(command: str, *flags: str) -> Callable[[tuple[str, ...], str], bool]:
    """Cell predicate: the command is *command* and every flag in *flags* is passed."""
    return lambda args, _scenario: _command_name(args) == command and all(f in args for f in flags)


def _runs_any(*commands: str) -> Callable[[tuple[str, ...], str], bool]:
    """Cell predicate: the command is one of *commands*."""
    return lambda args, _scenario: _command_name(args) in commands


def _scenario(*ids: str) -> Callable[[tuple[str, ...], str], bool]:
    """Cell predicate: the lifecycle scenario id is one of *ids*."""
    return lambda _args, scenario_id: scenario_id in ids


def _scenario_runs(command: str, *ids: str) -> Callable[[tuple[str, ...], str], bool]:
    """Cell predicate: *command* runs inside one of the lifecycle scenarios *ids*."""
    return lambda args, scenario_id: _command_name(args) == command and scenario_id in ids


_FROZEN_MCP_TARGETING = r"\[i\] (?:Targeting (?:declared targets?|specific runtimes?)|Skipped targets without MCP support)"
_POLICY_PHASE_BANNER = (
    r"Policy enforcement disabled by --no-policy|Org policy at \S+ is present but empty"
)

# Product defects the oracle observes on main; each one names its issue and
# is keyed by the surface family (command, flags, output shape) it occurs in.
KNOWN_OUTPUT_CELLS: tuple[KnownOutputCell, ...] = (
    KnownOutputCell(
        "audit-json-replay-error-has-no-json",
        "I6",
        "#4287",
        _scenario_runs("audit", "root-source-replacement"),
        r"JSON mode printed invalid JSON",
        output=r"\A\Z",
    ),
    KnownOutputCell(
        "install-policy-banner-per-phase",
        "I9",
        "#4241",
        _runs("install"),
        _POLICY_PHASE_BANNER,
        # The APM and MCP phases each print the policy banner (--no-policy or an empty org policy).
        output=rf"^\[!\] (?:{_POLICY_PHASE_BANNER}).*^  \[\+\] .*^\+- MCP Servers",
    ),
    KnownOutputCell(
        "install-org-policy-banner-per-phase",
        "I9",
        "#4249",
        _runs("install"),
        r"\[!\] Policy: org:",
        output=r"\A(?=.*^\[!\] Policy: org:).*^  \[\+\] .*^\+- MCP Servers",
    ),
    KnownOutputCell(
        "install-org-policy-banner-when-mcp-blocked",
        "I9",
        "#4267",
        _runs("install"),
        r"\[!\] Policy: org:",
        output=r"\A(?=.*^\[!\] Policy: org:).*^  \[\+\] .*^  \[x\] \d+ dependenc\w* blocked by org policy",
    ),
    KnownOutputCell(
        "approve-org-policy-warning-twice",
        "I9",
        "#4178",
        _runs("approve"),
        r"\[!\] (Org policy not applied|Using stale cached org policy)",
        # Only runs that print the same warning line twice (a single warning is correct).
        output=r"^(\[!\] (?:Org policy not applied|Using stale cached org policy)[^\n]*)$.*^\1$",
    ),
    KnownOutputCell(
        "install-frozen-mcp-targeting-twice",
        "I9",
        "#4196",
        _runs("install", "--frozen"),
        _FROZEN_MCP_TARGETING,
        # Auto-detected targets print no targeting line, so there is nothing to repeat.
        output=rf"\A(?=.*^\+- MCP Servers)(?=.*^{_FROZEN_MCP_TARGETING})",
    ),
    KnownOutputCell(
        "uninstall-unattributed-survivor-lines",
        "I9",
        "#4219",
        _runs("uninstall"),
        r"repeats a (line|diagnostic): '\|-- ",
        # Survivor tree lines with no package line naming them, one of them repeated.
        output=r"\A(?!.*^  \[\+\] ).*^  (\|-- [^\n]*)$.*^  \1$",
    ),
    KnownOutputCell(
        "pack-dry-run-plugin-notes-twice",
        "I9",
        "#4291",
        _runs("pack", "--dry-run"),
        r"Left out of the plugin|Would write plugin manifest",
        output=r"^\[!\] Left out of the plugin: ",
    ),
    KnownOutputCell(
        "install-frozen-parked-mcp-notice-per-phase",
        "I9",
        "#4299",
        _runs("install", "--frozen"),
        r"Parked MCP server .*: denied",
        output=r"^\[!\] Parked MCP server .*: denied.*^\+- MCP Servers",
    ),
    KnownOutputCell(
        "install-hook-line-per-target",
        "I9",
        "#4243",
        _runs("install"),
        r"repeats a (line|diagnostic): '\|   ",
        # One hook integrated into two targets that render it the same way (Claude
        # and Windsurf, or a hook file) is listed once per target, back to back.
        output=(
            r"^  \|-- 2 hook\(s\) integrated -> \.claude/settings\.json, \.windsurf/hooks\.json$"
            r"|^  \|-- 2 hook\(s\) integrated -> [^,\n]+, [^,\n]+\n  \|   Hook file integrated: "
        ),
    ),
    KnownOutputCell(
        "install-registry-lookup-per-registry",
        "I9",
        "#4244",
        _runs_any("install", "update"),
        r"Looking up 1 MCP server in registry",
        output=r"^\[i\] Using MCP registry: .*^\[i\] Using MCP registry: ",
    ),
    KnownOutputCell(
        "install-mcp-passthrough-warning-per-parse",
        "I9",
        "#4242",
        _runs("install", "hermes"),
        r"'hermes-stdio': unknown key\(s\) preserved in extra",
        output=(
            r"^\+- MCP Servers.*'hermes-stdio': unknown key"
            r"|^\[\*\] Validating 1 package.*'hermes-stdio': unknown key"
        ),
    ),
    KnownOutputCell(
        "install-mcp-failure-line-per-runtime",
        "I9",
        "#4288",
        _runs("install"),
        r"Failed to install ",
        output=r"^\|\s+\S+\s+-- failed for [^,\n]+, ",
    ),
    KnownOutputCell(
        "install-left-existing-server-per-shared-file",
        "I9",
        "#4289",
        _runs("install"),
        r"Left existing MCP server",
        # Claude Code and Copilot CLI share .mcp.json; each warns about it.
        output=r"Configuring for [^\n]*Claude Code[^\n]*Copilot CLI.*^\[!\] Left existing MCP server",
    ),
    KnownOutputCell(
        "install-verbose-ado-auth-attempt-repeats",
        "I9",
        "#4290",
        _runs("install", "--verbose"),
        r"Auth-only attempt for ado host|credential fill|Without a credential",
        output=r"^  \[i\] Auth-only attempt for ado host \S+ \(no credential\)",
    ),
    KnownOutputCell(
        "registry-environment-notice-per-server",
        "I9",
        "#4320",
        _runs_any("install", "update"),
        r"repeats a line: '(E2E test mode|CI environment) detected'",
        # Two registry lookups back to back each print the bare environment notice.
        output=r"^((?:E2E test mode|CI environment) detected)\n\1$",
    ),
    KnownOutputCell(
        "install-missing-required-package-warning-twice",
        "I9",
        "#4307",
        _runs("install"),
        r"required by org policy but not declared",
        # The warning block lists one missing required package back to back.
        output=r"^(    \+- \[[^\]\n]+\] required by org policy but not declared)\n\1$",
    ),
    KnownOutputCell(
        "self-update-installer-color-codes",
        "I6",
        "#4308",
        _runs("self-update"),
        r"stdout has characters outside printable ASCII \['0x1b'\]",
        # self-update passes the installer's output through; install.sh always colors it.
        output=r"^\[\*\] Running installer\.\.\.\n\x1b\[",
    ),
    KnownOutputCell(
        "command-echoes-non-ascii-path",
        "I6",
        "#4330",
        # Any command that echoes a user path (install, deps list, export-patch, ...).
        lambda _args, _scenario: True,
        r"stdout has characters outside printable ASCII",
        # A path (after a separator) with a non-ASCII character the test's own
        # working directory does not explain; the cwd echo itself passes I6.
        output=r"[/\\][^\n]*?[^\x00-\x7f]",
    ),
    KnownOutputCell(
        "uninstall-duplicate-declaration-lines",
        "I9",
        "#4331",
        _runs("uninstall"),
        r"repeats a (line|diagnostic): '\[(\+|i)\] .*(found in apm\.yml|from dependencies\.apm in apm\.yml)",
        output=r"^\s*(\[(?:\+|i)\] [^\n]*(?:found in apm\.yml|from dependencies\.apm in apm\.yml))$.*^\s*\1$",
    ),
    KnownOutputCell(
        "install-adapter-unreadable-config-warning-twice",
        "I9",
        "#4332",
        _runs("install"),
        r"stderr repeats a line: 'WARNING apm_cli\.adapters\.client\.\w+ Could not read",
        output=r"as a JSON object -- skipping config write",
    ),
    KnownOutputCell(
        "update-deny-all-lifecycle-notice-repeats",
        "I9",
        "#4333",
        _runs("update"),
        r"Lifecycle scripts suppressed by org policy",
        output=r"^\s*(\[!\] Lifecycle scripts suppressed by org policy[^\n]*)$.*^\s*\1$",
    ),
    KnownOutputCell(
        "install-verbose-diagnostics-repeat",
        "I9",
        "#4334",
        _runs("install", "--verbose"),
        r"repeats a (line|diagnostic): '(Package target restriction|\[i\] perf: )",
        output=r"^\s*((?:Package target restriction|\[i\] perf: )[^\n]*)$.*^\s*\1$",
    ),
    KnownOutputCell(
        "install-hook-summary-line-twice",
        "I9",
        "#4336",
        _runs("install"),
        r"repeats a line: '\|   \w+: runs ",
        # The same hook summary line printed twice back to back in one package block.
        output=r"^  (\|   \w+: runs [^\n]*)\n  \1$",
    ),
    KnownOutputCell(
        "pack-plugin-manifest-notes-twice",
        "I9",
        "#4291",
        _runs("pack"),
        r"repeats a diagnostic: '(\[i\]'|\[!\] Secrets withheld from plugin\.json)",
        output=r"^\s*(\[i\]|\[!\] Secrets withheld from plugin\.json[^\n]*)\s*$.*^\s*\1\s*$",
    ),
    KnownOutputCell(
        "install-frozen-trust-notice-repeats",
        "I9",
        "#4256",
        _runs("install", "--frozen"),
        r"Trusting direct dependency MCP",
        # Only runs that print the same trust notice twice (one notice is correct).
        output=r"^(\[i\] Trusting direct dependency MCP[^\n]*)$.*^\1$",
    ),
)

_EXPECTED_VIOLATIONS: ContextVar[frozenset[str]] = ContextVar(
    "apm_expected_output_violations", default=frozenset()
)


class OutputOracleError(AssertionError):
    """An APM command broke the global output contract."""


@contextmanager
def expect_output_violation(*laws: str, reason: str) -> Iterator[None]:
    """Suspend *laws* for commands run inside the block.

    Only for a test that is ABOUT that violation (for example a test that
    provokes a crash to assert its rendering). Product defects belong in
    ``KNOWN_OUTPUT_CELLS`` instead.
    """
    unknown = set(laws) - {"I6", "I8", "I9"}
    if not laws or unknown or not reason.strip():
        raise ValueError("expect_output_violation needs output laws (I6, I8, I9) and a reason")
    token = _EXPECTED_VIOLATIONS.set(_EXPECTED_VIOLATIONS.get() | frozenset(laws))
    try:
        yield
    finally:
        _EXPECTED_VIOLATIONS.reset(token)


def is_apm_entry_point(prefix: Sequence[str]) -> bool:
    """True when *prefix* runs APM.

    That is ``-m apm_cli.cli``, an ``apm`` binary, or a recorded mutant
    launcher under ``tests/fixtures/*_mutants/`` (it runs the real CLI).
    """
    if not prefix:
        return False
    last = Path(prefix[-1])
    return (
        prefix[-1] == "apm_cli.cli"
        or last.name.lower() in {"apm", "apm.exe"}
        or (last.suffix == ".py" and last.parent.name.endswith("_mutants"))
    )


def _command_name(args: Sequence[str]) -> str:
    return next((arg for arg in args if not arg.startswith("-")), "")


def _json_mode(args: Sequence[str]) -> bool:
    """True when the JSON document goes to stdout (``--output FILE`` sends it to a file)."""
    if any(a in {"-o", "--output"} or a.startswith("--output=") for a in args):
        return False
    return (
        "--json" in args
        or "--format=json" in args
        or any(a == "--format" and b == "json" for a, b in itertools.pairwise(args))
    )


def _ascii_violations(stream: str, text: str, echoed: str = "") -> list[OutputViolation]:
    """Characters outside printable ASCII, except those of *echoed* (the test's own paths)."""
    allowed = _ALLOWED_CONTROL | frozenset(echoed)
    bad = sorted({hex(ord(c)) for c in text if not (" " <= c <= "~" or c in allowed)})
    if not bad:
        return []
    return [OutputViolation("I6", f"{stream} has characters outside printable ASCII {bad[:5]}")]


def _logical_lines(text: str) -> list[str]:
    """Non-blank lines with terminal wraps undone.

    The console wraps a long status or tree line at its width: the broken
    line ends with the space it broke at, or fills the width when a word is
    split, and the continuation starts at column 0 with no status, tree or
    box prefix. Joining it back keeps two different wrapped paths from
    comparing equal on their shared first half ("[>] Resolving"); a short
    line is never joined, so plain lines such as "Phase: ..." stay apart.
    """
    lines: list[str] = []
    previous_raw = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        wrapped = previous_raw.endswith(" ") or len(previous_raw) >= _WRAP_WIDTH
        previous_raw = raw
        if not line.strip():
            continue
        if (
            lines
            and wrapped
            and not line[:1].isspace()
            and not line.startswith(_LINE_OPENERS)
            and lines[-1].strip().startswith(_LINE_OPENERS)
        ):
            lines[-1] = f"{lines[-1]} {line}"
            continue
        lines.append(line)
    return lines


def _noise_violations(stream: str, text: str) -> list[OutputViolation]:
    """One I9 violation per distinct duplicated line (one per cause), plus tracebacks."""
    found: list[OutputViolation] = []
    if _TRACEBACK in text:
        found.append(OutputViolation("I9", f"{stream} has a Python traceback"))
    # A cached-policy age ticks between phases; the same banner must still compare equal.
    lines = [_POLICY_CACHE_AGE.sub("fetched <AGE> ago", line) for line in _logical_lines(text)]
    # Rules and table borders legitimately repeat; diagnostics carry words.
    consecutive = dict.fromkeys(
        first.strip()
        for first, second in itertools.pairwise(lines)
        if first == second and any(c.isalnum() for c in first)
    )
    found.extend(
        OutputViolation("I9", f"{stream} repeats a line: {line!r}") for line in consecutive
    )
    # A top-level status line opens a block; indented status and tree lines are
    # compared only within their block, because per-package blocks repeat them.
    seen: set[tuple[int, str]] = set()
    repeated: dict[str, None] = {}
    block = 0
    for line in lines:
        stripped = line.strip()
        top_level = not line[:1].isspace()
        if top_level and stripped.startswith(_STATUS_PREFIXES):
            block += 1
            key = (0, stripped)
        elif not top_level and stripped.startswith("[+]"):
            # An indented package line ("  [+] <package>") opens that package's block.
            block += 1
            key = (0, stripped)
        elif not top_level and stripped.startswith(_STATUS_PREFIXES + _TREE_PREFIXES):
            key = (block, stripped)
        else:
            continue
        if key in seen and stripped not in consecutive:
            repeated[stripped] = None
        seen.add(key)
    found.extend(
        OutputViolation("I9", f"{stream} repeats a diagnostic: {line!r}") for line in repeated
    )
    return found


def output_violations(args: Sequence[str], result: Any) -> list[OutputViolation]:
    """Every I6, I8 and I9 violation in one APM command result."""
    found: list[OutputViolation] = []
    # A path-corner world puts non-ASCII characters in the test's own paths;
    # echoing a user path back is not APM output, so its characters pass.
    echoed = str(getattr(result, "cwd", ""))
    for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        found.extend(_ascii_violations(stream, text, echoed))
    if _json_mode(args) and result.returncode in (0, 1):
        try:
            json.loads(result.stdout)
        except ValueError as exc:
            found.append(OutputViolation("I6", f"JSON mode printed invalid JSON on stdout: {exc}"))
    contract = exit_contract(_command_name(args))
    if result.returncode not in contract:
        found.append(
            OutputViolation("I8", f"exit code {result.returncode} is outside {sorted(contract)}")
        )
    for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        if stream == "stderr" and _diagnostic_run(args):
            # A verbose or debug run asked for Python logging records; they are
            # diagnostics, not user output, so I9 does not judge their repeats.
            text = "\n".join(line for line in text.splitlines() if not _LOG_RECORD.match(line))
        found.extend(_noise_violations(stream, text))
    return found


# A Python logging record on stderr: "DEBUG apm_cli.core.token_manager ...".
_LOG_RECORD = re.compile(r"^(?:DEBUG|INFO) [A-Za-z_][\w.]* ")


def _diagnostic_run(args: Sequence[str]) -> bool:
    """True when the run itself enabled verbose or debug output."""
    return bool({"-v", "--verbose"} & set(args)) or bool(os.environ.get("APM_DEBUG"))


def apm_args(command: Sequence[str]) -> tuple[str, ...]:
    """The APM arguments of a full command line (after ``-m apm_cli.cli`` or the binary)."""
    command = tuple(command)
    for index in range(len(command), 0, -1):
        if is_apm_entry_point(command[:index]):
            return command[index:]
    return ()


def owned_duplicate_lines(result: Any, scenario_id: str = "") -> frozenset[str]:
    """Duplicated lines of *result* that a STRICT I9 known cell owns.

    The family I9 check (``tests/lifecycle/oracles.py``) excuses exactly these,
    so one product defect has one owner; the global hook on the same command
    still fails the cell once the defect is fixed.
    """
    args = apm_args(result.command)
    violations = [v for v in output_violations(args, result) if v.law == "I9"]
    owned: set[str] = set()
    for cell in KNOWN_OUTPUT_CELLS:
        if cell.law != "I9" or not cell.covers(args, scenario_id, result.stdout):
            continue
        for violation in violations:
            line = re.search(r": '(.*)'$", violation.message)
            if line and re.search(cell.message, violation.message):
                owned.add(line.group(1))
    return frozenset(owned)


def _without_echoed_non_ascii(stdout: str, echoed: str) -> str:
    """*stdout* without the non-ASCII characters of the test's own path (they pass I6)."""
    return stdout.translate({ord(c): None for c in echoed if not " " <= c <= "~"})


def check_cli_output(
    args: Sequence[str],
    result: Any,
    *,
    scenario_id: str = "single-command",
    known_cells: Sequence[KnownOutputCell] | None = None,
) -> None:
    """Fail on any unowned output violation of an APM command; non-APM commands pass."""
    args = tuple(args)
    prefix = result.command[: len(result.command) - len(args)]
    if not is_apm_entry_point(prefix):
        return
    expected = _EXPECTED_VIOLATIONS.get()
    violations = [v for v in output_violations(args, result) if v.law not in expected]
    cell_stdout = _without_echoed_non_ascii(result.stdout, str(getattr(result, "cwd", "")))
    problems: list[str] = []
    for cell in KNOWN_OUTPUT_CELLS if known_cells is None else known_cells:
        if cell.law in expected or not cell.covers(args, scenario_id, cell_stdout):
            continue
        owned = [v for v in violations if v.law == cell.law and re.search(cell.message, v.message)]
        if not owned:
            problems.append(
                f"{cell.law}: known cell {cell.id} ({cell.issue}) did not fail; "
                "the defect now passes, so remove the cell"
            )
        violations = [v for v in violations if v not in owned]
    problems[:0] = [f"{v.law}: {v.message}" for v in violations]
    if problems:
        raise OutputOracleError(
            "output oracle (#4113) failed:\n"
            + "\n".join(problems)
            + f"\nscenario={scenario_id!r}\ncommand={result.command!r}"
            + f"\nreturncode={result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
