"""Lifecycle script models, runner, and discovery.

APM supports lifecycle scripts that fire at key moments during install,
update, and uninstall operations.  Scripts are configured via standalone
files discovered from three directories (Copilot CLI pattern):

1. **Policy** -- ``/etc/apm/policy.d/*.json`` (admin-owned, cannot be disabled)
2. **User**   -- ``~/.apm/scripts/*.json``
3. **Project** -- ``apm-scripts.yml`` (repo root, YAML, single file)

Per-tier format split (intentional):
- Project tier uses YAML (``apm-scripts.yml`` at repo root, human-authored,
  trust-audited).
- Admin (policy.d) and user (~/.apm/scripts/) tiers use JSON, suited for
  machine/fleet-managed configuration.

Each file uses ``{ version: 1, scripts: { "<event>": [...] } }``.

Two script types are supported; each entry must declare its kind via a
``type`` field:

- ``type: command`` -- shell command (``bash`` / ``command`` fields)
- ``type: http``    -- HTTPS POST to a URL with optional headers

An optional ``description`` field may be added to any entry as a
free-text annotation (surfaced in dry-run output; otherwise ignored).

Scripts run in source order at each event.  Failures are isolated: a
script error is logged (verbose) but never aborts the CLI operation.
HTTP scripts dispatch asynchronously (daemon thread); ``command`` scripts
run **synchronously** and can therefore delay the operation up to their
timeout.  Use ``APM_NO_SCRIPTS=1`` to disable all scripts for one run.
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

# Supported script action types (Copilot CLI aligned).
SCRIPT_TYPES = ("command", "http")

# Current script-file schema version.
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
    receive it via **stdin** (JSON-encoded).
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
    """One configured lifecycle script action (Copilot CLI schema).

    Attributes:
        script_type: ``command`` or ``http``.
        event:       Lifecycle event name (e.g. ``post-install``).
        bash:        Shell command for Unix (``command`` type).
        command:     Cross-platform fallback command string.
        url:         HTTP endpoint URL (``http`` type).
        headers:     HTTP headers dict; values support ``$ENV_VAR`` expansion.
        timeout_sec: Timeout in seconds (default 30 for commands, 10 for http).
        cwd:         Working directory for the command (relative or absolute).
        env:         Extra environment variables for the command.
        allowed_env_vars: Opt-in allowlist of env var names that may be
                     passed through / expanded even if they match the
                     credential denylist (e.g. ``ANALYTICS_TOKEN``).
        description: Optional free-text annotation for the script entry.
                     Surfaced in dry-run output; otherwise ignored.
        source:      Where this script was defined: ``policy``, ``user``,
                     or ``project``.
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

        On Windows, prefer ``command`` (cross-platform) over ``bash``
        because bash may not be available.  On Unix, prefer ``bash``.
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
    """Normalise the optional ``allowedEnvVars`` field to a str list."""
    if not isinstance(raw, list):
        return None
    names = [str(v) for v in raw if isinstance(v, str) and v.strip()]
    return names or None


def _entries_from_data(data: object, path: Path, source: str) -> list[ScriptEntry]:
    """Build :class:`ScriptEntry` list from an already-parsed data dict.

    Shared by both JSON (admin/user) and YAML (project) loaders so the
    entry-building logic is not duplicated.  Returns an empty list if
    *data* is malformed or uses an unsupported version.
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
            if not isinstance(raw, dict):
                continue
            # Explicit ``type`` field is canonical; infer from key presence as
            # a backward-compatible fallback so legacy fixtures still parse.
            script_type = raw.get("type")
            if script_type is None:
                script_type = "http" if raw.get("url") else "command"
            if script_type not in SCRIPT_TYPES:
                _logger.debug("Ignoring unknown script type %s in %s", script_type, path)
                continue
            entries.append(
                ScriptEntry(
                    script_type=script_type,
                    event=event_name,
                    bash=raw.get("bash"),
                    command=raw.get("command"),
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
            )
    return entries


def parse_script_file(path: Path, source: str = "project") -> list[ScriptEntry]:
    """Parse a single JSON script file into a list of :class:`ScriptEntry`.

    Used for the admin (policy.d) and user (~/.apm/scripts/) tiers which
    remain JSON.  Returns an empty list if the file is malformed or uses
    an unsupported version.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _logger.debug("Failed to load script file %s: %s", path, e)
        return []

    return _entries_from_data(data, path, source)


def parse_project_script_file(path: Path) -> list[ScriptEntry]:
    """Parse the project-tier ``apm-scripts.yml`` YAML file.

    The project tier uses YAML (human-authored, trust-audited).  All
    admin and user tier files remain JSON and use :func:`parse_script_file`.
    Returns an empty list if the file is missing, malformed, or uses an
    unsupported version.
    """
    from apm_cli.utils.yaml_io import load_yaml

    try:
        data = load_yaml(path)
    except Exception as e:
        _logger.debug("Failed to load project script file %s: %s", path, e)
        return []

    return _entries_from_data(data, path, "project")


# -- Script discovery ------------------------------------------------------


def _get_policy_scripts_dir() -> Path:
    """Return the platform-specific policy scripts directory."""
    system = platform.system()
    if system == "Windows":
        return Path(r"C:\ProgramData\APM\policy.d")
    return Path("/etc/apm/policy.d")


def _get_user_scripts_dir() -> Path:
    """Return the user-level scripts directory (~/.apm/scripts/)."""
    apm_home = os.environ.get("APM_HOME")
    if apm_home:
        return Path(apm_home) / "scripts"
    return Path.home() / ".apm" / "scripts"


def _get_project_scripts_file(project_root: str | None = None) -> Path:
    """Return the project-level scripts file (``apm-scripts.yml`` at repo root)."""
    root = Path(project_root) if project_root else Path.cwd()
    return root / "apm-scripts.yml"


def _load_scripts_from_dir(
    directory: Path,
    source: str,
) -> list[ScriptEntry]:
    """Load all ``*.json`` script files from *directory*, sorted by name."""
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
      1. Policy  -- ``/etc/apm/policy.d/*.json`` (directory, JSON)
      2. User    -- ``~/.apm/scripts/*.json`` (directory, JSON)
      3. Project -- ``apm-scripts.yml`` (repo root, YAML)
    """
    scripts: list[ScriptEntry] = []
    scripts.extend(_load_scripts_from_dir(_get_policy_scripts_dir(), source="policy"))
    scripts.extend(_load_scripts_from_dir(_get_user_scripts_dir(), source="user"))

    project_file = _get_project_scripts_file(project_root)
    if project_file.is_file():
        scripts.extend(parse_project_script_file(project_file))

    return scripts


# -- Script runner ---------------------------------------------------------


class LifecycleScriptRunner:
    """Collects scripts and fires them for lifecycle events.

    Scripts run with error isolation: a failure in one script never blocks
    the CLI or the remaining scripts.  ``http`` scripts dispatch
    asynchronously; ``command`` scripts run synchronously and may delay
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
            "Run 'apm scripts trust' to enable them."
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
        """Execute all scripts registered for *event_name*.

        Each script runs in isolation -- a failure in one script does not
        prevent subsequent scripts from running.

        Returns a list of daemon threads started by HTTP scripts so
        callers can optionally join them (e.g. for test/dry-run).
        """
        from apm_cli.core.script_executors import execute_script

        self._emit_skip_notice()

        matching = [s for s in self._scripts if s.event == event_name]
        if not matching:
            return []

        threads: list[threading.Thread] = []
        for script in matching:
            try:
                thread = execute_script(
                    script,
                    event,
                    logger=self._logger,
                    verbose=self._verbose,
                    project_root=self._project_root,
                )
                if thread is not None:
                    threads.append(thread)
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
        return threads

    def scripts_for_event(self, event_name: str) -> list[ScriptEntry]:
        """Return scripts registered for *event_name* (public API)."""
        return [s for s in self._scripts if s.event == event_name]


# -- Convenience: build runner from file-based discovery -------------------


def build_runner_from_context(
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
    project_root: str | None = None,
) -> LifecycleScriptRunner:
    """Create a :class:`LifecycleScriptRunner` via file-based discovery.

    Scans policy (JSON), user (JSON), and project (YAML) script sources.

    Three safeguards are applied at this firing boundary:

    - ``APM_NO_SCRIPTS`` (env, any non-empty value) disables ALL scripts
      for the current invocation -- a blanket escape hatch for CI and
      untrusted clones.
    - Org ``executables.deny_all`` (org policy kill-switch): when set,
      suppresses all lifecycle scripts as a one-directional safety
      ceiling. Best-effort: any discovery error is silently ignored so
      the install flow is never blocked.
    - Project-source scripts (``apm-scripts.yml``) are dropped unless
      their exact contents have been explicitly trusted via ``apm scripts
      trust`` (see :mod:`apm_cli.core.script_trust`). Policy and user
      scripts come from developer-controlled locations and are never gated.
    """
    if os.environ.get("APM_NO_SCRIPTS"):
        return LifecycleScriptRunner(
            scripts=[], logger=logger, verbose=verbose, project_root=project_root
        )

    from apm_cli.core.script_trust import is_project_scripts_trusted

    scripts = discover_scripts(project_root=project_root)

    # Short-circuit: skip the org policy network call when there are no scripts
    # to run. This avoids a potential RPC on cold cache in the no-scripts case,
    # which is the common case for most projects.
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

    project_file = _get_project_scripts_file(project_root)
    project_trusted = project_file.is_file() and is_project_scripts_trusted(project_file)

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
        skipped_project_file=str(project_file) if skipped else None,
    )
