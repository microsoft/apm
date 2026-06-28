"""Lifecycle script models, runner, and discovery.

APM supports lifecycle scripts that fire at key moments during install,
update, and uninstall operations.  Scripts are configured in well-known
locations discovered from three tiers:

1. Policy  -- /etc/apm/policy.d/*.json (admin-owned, JSON drop-ins, unchanged)
2. User    -- ~/.apm/apm.yml (or $APM_HOME/apm.yml) lifecycle: key
3. Project -- apm.yml lifecycle: key (repo root)

Admin tier uses {version:1, scripts:{...}} JSON files in a directory.
Project and user tiers embed scripts under a top-level lifecycle: key
in apm.yml -- the manifest is the envelope, so there is no version/scripts
wrapper inside the lifecycle block.

Two script types are supported; each entry must declare its kind via a
type field:

- type: command -- shell command (bash / command / run fields)
- type: http    -- HTTPS POST to a URL with optional headers

An optional description field may be added to any entry as a
free-text annotation (surfaced in dry-run output; otherwise ignored).

Scripts run in source order at each event.  Failures are isolated: a
script error is logged (verbose) but never aborts the CLI operation.
HTTP scripts dispatch asynchronously (daemon thread); command scripts
run synchronously and can therefore delay the operation up to their
timeout.  Use APM_NO_SCRIPTS=1 to disable all scripts for one run.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.core.command_logger import CommandLogger

_logger = logging.getLogger(__name__)

# Supported lifecycle event names.
LIFECYCLE_EVENTS = (
    "pre-install",
    "post-install",
    "pre-update",
    "post-update",
    "pre-uninstall",
    "post-uninstall",
)

# Supported script action types.
SCRIPT_TYPES = ("command", "http")

# Current script-file schema version (used by admin/user JSON tier only).
SCRIPT_FILE_VERSION = 1


# -- Event model -----------------------------------------------------------


@dataclass
class PackageInfo:
    """Minimal package metadata carried in lifecycle events."""

    name: str
    reference: str | None = None


@dataclass
class LifecycleEvent:
    """Data payload passed to every lifecycle script.

    HTTP scripts receive this as a JSON POST body.  Command scripts
    receive it via stdin (JSON-encoded).
    """

    schema_version: int = 1
    event: str = ""
    packages: list[PackageInfo] = field(default_factory=list)
    scope: str = "project"
    timestamp: str = ""
    cli_version: str = ""
    working_directory: str = ""

    def to_json(self) -> str:
        """Serialise the event to a compact JSON string."""
        return json.dumps(asdict(self), separators=(",", ":"))

    @staticmethod
    def create(
        event: str,
        packages: list[PackageInfo] | None = None,
        scope: str = "project",
        working_directory: str | None = None,
    ) -> LifecycleEvent:
        """Factory that auto-fills timestamp and CLI version."""
        from apm_cli.version import get_version

        return LifecycleEvent(
            event=event,
            packages=packages or [],
            scope=scope,
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            cli_version=get_version(),
            working_directory=working_directory or str(Path.cwd()),
        )


# -- Script entry (one action inside a script file) ------------------------


@dataclass
class ScriptEntry:
    """One configured lifecycle script action.

    Attributes:
        script_type: command or http.
        event:       Lifecycle event name (e.g. post-install).
        bash:        Shell command for Unix (command type).
        command:     Cross-platform fallback command string.
        url:         HTTP endpoint URL (http type).
        headers:     HTTP headers dict; values support $ENV_VAR expansion.
        timeout_sec: Timeout in seconds (default 30 for commands, 10 for http).
        cwd:         Working directory for the command (relative or absolute).
        env:         Extra environment variables for the command.
        allowed_env_vars: Opt-in allowlist of env var names that may be
                     passed through / expanded even if they match the
                     credential denylist (e.g. ANALYTICS_TOKEN).
        description: Optional free-text annotation for the script entry.
                     Surfaced in dry-run output; otherwise ignored.
        source:      Where this script was defined: policy, user, or project.
        source_file: Path of the file that declared this script.
    """

    script_type: str
    event: str
    bash: str | None = None
    command: str | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    timeout_sec: int | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    allowed_env_vars: list[str] | None = None
    description: str | None = None
    source: str = "project"
    source_file: str | None = None

    @property
    def effective_command(self) -> str | None:
        """Resolve the command to run on the current platform.

        On Windows, prefer command (cross-platform) over bash
        because bash may not be available.  On Unix, prefer bash.
        """
        if platform.system() == "Windows":
            return self.command or self.bash
        return self.bash or self.command

    @property
    def effective_timeout(self) -> int:
        """Return timeout_sec with sensible defaults per type."""
        if self.timeout_sec is not None:
            return self.timeout_sec
        return 10 if self.script_type == "http" else 30


# -- Script file parsing ---------------------------------------------------


def _parse_allowed_env_vars(raw: object) -> list[str] | None:
    """Normalise the optional allowedEnvVars field to a str list."""
    if not isinstance(raw, list):
        return None
    names = [str(v) for v in raw if isinstance(v, str) and v.strip()]
    return names or None


def _build_entry(raw: object, event_name: str, path: Path, source: str) -> ScriptEntry | None:
    """Build a single ScriptEntry from a raw mapping.

    Returns None if raw is not a dict or has an unknown type.
    """
    if not isinstance(raw, dict):
        return None
    script_type = raw.get("type")
    if script_type is None:
        script_type = "http" if raw.get("url") else "command"
    if script_type not in SCRIPT_TYPES:
        _logger.debug("Ignoring unknown script type %s in %s", script_type, path)
        return None
    # run: is an accepted alias for bash/command
    run_val = raw.get("run")
    bash_val = raw.get("bash") or (run_val if run_val and not raw.get("bash") else None)
    command_val = raw.get("command") or (run_val if run_val and not raw.get("command") else None)
    return ScriptEntry(
        script_type=script_type,
        event=event_name,
        bash=bash_val,
        command=command_val,
        url=raw.get("url"),
        headers=raw.get("headers"),
        timeout_sec=raw.get("timeoutSec") or raw.get("timeout"),
        cwd=raw.get("cwd"),
        env=raw.get("env"),
        allowed_env_vars=_parse_allowed_env_vars(raw.get("allowedEnvVars")),
        description=raw.get("description"),
        source=source,
        source_file=str(path),
    )


def _entries_from_data(data: object, path: Path, source: str) -> list[ScriptEntry]:
    """Build ScriptEntry list from an already-parsed data dict.

    Used by the admin/user JSON tier (version+scripts wrapper required).
    Returns an empty list if data is malformed or uses an unsupported version.
    """
    if not isinstance(data, dict):
        return []

    version = data.get("version")
    if version != SCRIPT_FILE_VERSION:
        _logger.debug("Skipping script file %s: unsupported version %s", path, version)
        return []

    scripts_dict = data.get("scripts")
    if not isinstance(scripts_dict, dict):
        return []

    entries: list[ScriptEntry] = []
    for event_name, script_list in scripts_dict.items():
        if event_name not in LIFECYCLE_EVENTS:
            _logger.debug("Ignoring unknown lifecycle event %s in %s", event_name, path)
            continue
        if not isinstance(script_list, list):
            continue
        for raw in script_list:
            entry = _build_entry(raw, event_name, path, source)
            if entry is not None:
                entries.append(entry)
    return entries


def _entries_from_lifecycle_map(lifecycle: object, path: Path, source: str) -> list[ScriptEntry]:
    """Build ScriptEntry list from the lifecycle: subtree of an apm.yml dict.

    No version check -- apm.yml has no version field for the lifecycle block.
    Returns an empty list if lifecycle is not a dict.
    """
    if not isinstance(lifecycle, dict):
        return []

    entries: list[ScriptEntry] = []
    for event_name, script_list in lifecycle.items():
        if event_name not in LIFECYCLE_EVENTS:
            _logger.debug("Ignoring unknown lifecycle event %s in %s", event_name, path)
            continue
        if not isinstance(script_list, list):
            continue
        for raw in script_list:
            entry = _build_entry(raw, event_name, path, source)
            if entry is not None:
                entries.append(entry)
    return entries


def parse_apm_yml_lifecycle(path: Path, source: str) -> list[ScriptEntry]:
    """Parse the lifecycle: subtree from an apm.yml file.

    Used for both project (apm.yml at repo root) and user
    (~/.apm/apm.yml) tiers.  Returns an empty list if the file is
    missing, malformed, or has no lifecycle: key.
    """
    return parse_apm_yml_lifecycle_with_fingerprint(path, source)[0]


def parse_apm_yml_lifecycle_with_fingerprint(
    path: Path, source: str
) -> tuple[list[ScriptEntry], str | None]:
    """Parse the lifecycle: subtree AND fingerprint it from a single read.

    Returning both from one ``load_yaml`` call lets the trust gate
    fingerprint the EXACT content that will execute -- closing the
    TOCTOU window that exists when discovery and the trust check read
    the file independently. A non-dict top-level degrades to empty
    (no crash on a malformed/hostile manifest).
    """
    from apm_cli.core.script_trust import fingerprint_lifecycle_subtree
    from apm_cli.utils.yaml_io import load_yaml

    try:
        data = load_yaml(path)
    except Exception as e:
        _logger.debug("Failed to load apm.yml lifecycle from %s: %s", path, e)
        return [], None

    if not isinstance(data, dict):
        return [], None

    lifecycle = data.get("lifecycle")
    entries = _entries_from_lifecycle_map(lifecycle, path, source)
    return entries, fingerprint_lifecycle_subtree(lifecycle)


def parse_script_file(path: Path, source: str = "project") -> list[ScriptEntry]:
    """Parse a single JSON script file into a list of ScriptEntry.

    Used for JSON-backed sources such as the admin policy tier
    (/etc/apm/policy.d/*.json). Returns an empty list if the file is
    malformed or uses an unsupported version.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        _logger.debug("Failed to load script file %s: %s", path, e)
        return []

    return _entries_from_data(data, path, source)


def parse_project_script_file(path: Path) -> list[ScriptEntry]:
    """Parse the project-tier apm.yml lifecycle: key.

    Thin alias for parse_apm_yml_lifecycle kept for backward compatibility.
    """
    return parse_apm_yml_lifecycle(path, "project")


# -- Script discovery ------------------------------------------------------


def _get_policy_scripts_dir() -> Path:
    """Return the platform-specific policy scripts directory."""
    system = platform.system()
    if system == "Windows":
        return Path(r"C:\ProgramData\APM\policy.d")
    return Path("/etc/apm/policy.d")


def _get_user_apm_yml() -> Path:
    """Return the user-level apm.yml path (~/.apm/apm.yml or $APM_HOME/apm.yml)."""
    apm_home = os.environ.get("APM_HOME")
    if apm_home:
        return Path(apm_home) / "apm.yml"
    return Path.home() / ".apm" / "apm.yml"


def _get_project_apm_yml(project_root: str | None = None) -> Path:
    """Return the project-level apm.yml path at the repo root."""
    root = Path(project_root) if project_root else Path.cwd()
    return root / "apm.yml"


def _load_scripts_from_dir(
    directory: Path,
    source: str,
) -> list[ScriptEntry]:
    """Load all *.json script files from directory, sorted by name."""
    if not directory.is_dir():
        return []
    entries: list[ScriptEntry] = []
    for json_file in sorted(directory.glob("*.json")):
        if json_file.is_file():
            entries.extend(parse_script_file(json_file, source=source))
    return entries


def discover_scripts(
    project_root: str | None = None,
) -> list[ScriptEntry]:
    """Discover and merge scripts from all three sources.

    Load order (all additive, policy first):
      1. Policy  -- /etc/apm/policy.d/*.json (directory, JSON)
      2. User    -- ~/.apm/apm.yml (or $APM_HOME/apm.yml) lifecycle: key
      3. Project -- apm.yml lifecycle: key (repo root)
    """
    scripts = _discover_non_project_scripts(project_root)
    project_yml = _get_project_apm_yml(project_root)
    if project_yml.is_file():
        scripts.extend(parse_apm_yml_lifecycle(project_yml, "project"))
    return scripts


def _discover_non_project_scripts(
    project_root: str | None = None,
) -> list[ScriptEntry]:
    """Discover policy + user scripts (everything except the project tier).

    Kept separate so the firing path can read the project tier ONCE (for
    both execution and the trust fingerprint) without re-reading it here.
    """
    scripts: list[ScriptEntry] = []
    scripts.extend(_load_scripts_from_dir(_get_policy_scripts_dir(), source="policy"))

    user_yml = _get_user_apm_yml()
    if user_yml.is_file():
        scripts.extend(parse_apm_yml_lifecycle(user_yml, "user"))

    return scripts


# -- Script runner ---------------------------------------------------------


class LifecycleScriptRunner:
    """Collects scripts and fires them for lifecycle events.

    Scripts run with error isolation: a failure in one script never blocks
    the CLI or the remaining scripts.  http scripts dispatch
    asynchronously; command scripts run synchronously and may delay
    the operation up to their per-script timeout.
    """

    def __init__(
        self,
        scripts: list[ScriptEntry] | None = None,
        logger: CommandLogger | None = None,
        verbose: bool = False,
        project_root: str | None = None,
        skipped_project_scripts: int = 0,
        skipped_project_file: str | None = None,
    ) -> None:
        self._scripts = scripts or []
        self._logger = logger
        self._verbose = verbose
        self._project_root = project_root
        self._skipped_project_scripts = skipped_project_scripts
        self._skipped_project_file = skipped_project_file
        self._skip_notice_emitted = False

    def _emit_skip_notice(self) -> None:
        """Warn once that untrusted project scripts were skipped."""
        if self._skip_notice_emitted or self._skipped_project_scripts <= 0:
            return
        self._skip_notice_emitted = True
        count = self._skipped_project_scripts
        msg = (
            f"[!] Skipped {count} untrusted project script(s). "
            "Run 'apm lifecycle trust' to enable them."
        )
        if self._logger is not None:
            emit = getattr(self._logger, "warning", None) or getattr(
                self._logger, "verbose_detail", None
            )
            if emit is not None:
                emit(msg)
        else:
            _logger.warning("%s", msg)

    def fire(self, event_name: str, event: LifecycleEvent) -> list[threading.Thread]:
        """Execute all scripts registered for event_name.

        Each script runs in isolation -- a failure in one script does not
        prevent subsequent scripts from running.

        Returns a list of daemon threads started by HTTP scripts so
        callers can optionally join them (e.g. for test/dry-run).
        """
        from apm_cli.core.script_executors import dispatch_http_batch, execute_script

        self._emit_skip_notice()

        matching = [s for s in self._scripts if s.event == event_name]
        if not matching:
            return []

        # command scripts run synchronously (in declared order); http
        # scripts dispatch through a bounded worker pool so a file with
        # many http entries cannot spawn an unbounded number of threads.
        http_scripts: list[ScriptEntry] = []
        for script in matching:
            if script.script_type == "http":
                http_scripts.append(script)
                continue
            try:
                execute_script(
                    script,
                    event,
                    logger=self._logger,
                    verbose=self._verbose,
                    project_root=self._project_root,
                )
            except Exception:
                _logger.debug(
                    "Lifecycle script failed (type=%s, event=%s)",
                    script.script_type,
                    event_name,
                    exc_info=True,
                )
                if self._verbose and self._logger:
                    self._logger.verbose_detail(
                        f"[i] Lifecycle script failed: {script.script_type} for {event_name}"
                    )

        return dispatch_http_batch(
            http_scripts,
            event,
            logger=self._logger,
            verbose=self._verbose,
        )

    def scripts_for_event(self, event_name: str) -> list[ScriptEntry]:
        """Return scripts registered for event_name (public API)."""
        return [s for s in self._scripts if s.event == event_name]


# -- Convenience: build runner from file-based discovery -------------------


def build_runner_from_context(
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
    project_root: str | None = None,
) -> LifecycleScriptRunner:
    """Create a LifecycleScriptRunner via file-based discovery.

    Scans policy (JSON), user (apm.yml lifecycle:), and project (apm.yml
    lifecycle:) script sources.

    Three safeguards are applied at this firing boundary:

    - APM_NO_SCRIPTS (env, any non-empty value) disables ALL scripts
      for the current invocation -- a blanket escape hatch for CI and
      untrusted clones.
    - Org executables.deny_all (org policy kill-switch): when set,
      suppresses all lifecycle scripts as a one-directional safety
      ceiling. Best-effort: any discovery error is silently ignored so
      the install flow is never blocked.
    - Project-source scripts (apm.yml lifecycle:) are dropped unless
      their exact lifecycle: subtree has been explicitly trusted via
      apm lifecycle trust (see apm_cli.core.script_trust). Policy and
      user scripts come from developer-controlled locations and are
      never gated.
    """
    if os.environ.get("APM_NO_SCRIPTS"):
        return LifecycleScriptRunner(
            scripts=[], logger=logger, verbose=verbose, project_root=project_root
        )

    from apm_cli.core.script_trust import is_fingerprint_trusted

    # Single read of the project tier: the entries that EXECUTE and the
    # fingerprint that GATES them come from one parse, so a swap between
    # an independent discovery read and trust read cannot run malicious
    # scripts under a stale "trusted" verdict (TOCTOU).
    scripts = _discover_non_project_scripts(project_root)
    project_yml = _get_project_apm_yml(project_root)
    project_entries: list[ScriptEntry] = []
    project_fp: str | None = None
    if project_yml.is_file():
        project_entries, project_fp = parse_apm_yml_lifecycle_with_fingerprint(
            project_yml, "project"
        )
    scripts.extend(project_entries)

    if not scripts:
        return LifecycleScriptRunner(
            scripts=[], logger=logger, verbose=verbose, project_root=project_root
        )

    # Org deny_all ceiling: best-effort, never raises into the install flow.
    org_deny_all = False
    try:
        from apm_cli.policy.discovery import discover_policy_with_chain

        fetch_result = discover_policy_with_chain(project_root)
        if fetch_result and fetch_result.policy:
            org_deny_all = bool(fetch_result.policy.executables.deny_all)
    except Exception:
        pass

    if org_deny_all:
        if logger is not None:
            emit = getattr(logger, "warning", None) or getattr(logger, "verbose_detail", None)
            if emit is not None:
                emit("[!] Lifecycle scripts suppressed by org policy (executables.deny_all).")
        return LifecycleScriptRunner(
            scripts=[], logger=logger, verbose=verbose, project_root=project_root
        )

    project_trusted = is_fingerprint_trusted(project_yml, project_fp)

    kept: list[ScriptEntry] = []
    skipped = 0
    for script in scripts:
        if script.source == "project" and not project_trusted:
            skipped += 1
            continue
        kept.append(script)

    return LifecycleScriptRunner(
        scripts=kept,
        logger=logger,
        verbose=verbose,
        project_root=project_root,
        skipped_project_scripts=skipped,
        skipped_project_file=str(project_yml) if skipped else None,
    )
