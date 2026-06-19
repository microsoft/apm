"""Lifecycle hook models, runner, and discovery.

APM supports lifecycle hooks that fire at key moments during install,
update, and uninstall operations.  Hooks are configured via standalone
JSON files discovered from three directories (Copilot CLI pattern):

1. **Policy** -- ``/etc/apm/policy.d/*.json`` (admin-owned, cannot be disabled)
2. **User**   -- ``~/.apm/hooks/*.json``
3. **Project** -- ``.apm/hooks.json`` (single file)

Each file uses ``{ "version": 1, "hooks": { "<event>": [...] } }``.

Two hook types are supported:

- ``command`` -- shell command (``bash`` / ``command`` fields)
- ``http``    -- HTTPS POST to a URL with optional headers

All hooks are **fire-and-forget**: failures are logged in verbose mode
but never block the CLI.
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
from typing import TYPE_CHECKING, Any

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

# Supported hook action types (Copilot CLI aligned).
HOOK_TYPES = ("command", "http")

# Current hook-file schema version.
HOOK_FILE_VERSION = 1


# -- Event model -----------------------------------------------------------


@dataclass
class PackageInfo:
    """Minimal package metadata carried in lifecycle events."""

    name: str
    reference: str | None = None


@dataclass
class LifecycleEvent:
    """Data payload passed to every lifecycle hook.

    HTTP hooks receive this as a JSON POST body.  Command hooks
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


# -- Hook entry (one action inside a hook file) ----------------------------


@dataclass
class HookEntry:
    """One configured lifecycle hook action (Copilot CLI schema).

    Attributes:
        hook_type:   ``command`` or ``http``.
        event:       Lifecycle event name (e.g. ``post-install``).
        bash:        Shell command for Unix (``command`` type).
        command:     Cross-platform fallback command string.
        url:         HTTP endpoint URL (``http`` type).
        headers:     HTTP headers dict; values support ``$ENV_VAR`` expansion.
        timeout_sec: Timeout in seconds (default 30 for commands, 10 for http).
        cwd:         Working directory for the command (relative or absolute).
        env:         Extra environment variables for the command.
        source:      Where this hook was defined: ``policy``, ``user``,
                     or ``project``.
        source_file: Path of the JSON file that declared this hook.
    """

    hook_type: str
    event: str
    bash: str | None = None
    command: str | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    timeout_sec: int | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    source: str = "project"
    source_file: str | None = None

    @property
    def effective_command(self) -> str | None:
        """Resolve the command to run on the current platform."""
        return self.bash or self.command

    @property
    def effective_timeout(self) -> int:
        """Return timeout_sec with sensible defaults per type."""
        if self.timeout_sec is not None:
            return self.timeout_sec
        return 10 if self.hook_type == "http" else 30


# -- Hook file parsing -----------------------------------------------------


def parse_hook_file(path: Path, source: str = "project") -> list[HookEntry]:
    """Parse a single JSON hook file into a list of :class:`HookEntry`.

    Returns an empty list if the file is malformed or uses an
    unsupported version.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _logger.debug("Failed to load hook file %s: %s", path, e)
        return []

    if not isinstance(data, dict):
        return []

    version = data.get("version")
    if version != HOOK_FILE_VERSION:
        _logger.debug("Skipping hook file %s: unsupported version %s", path, version)
        return []

    hooks_dict = data.get("hooks")
    if not isinstance(hooks_dict, dict):
        return []

    entries: list[HookEntry] = []
    for event_name, hook_list in hooks_dict.items():
        if event_name not in LIFECYCLE_EVENTS:
            _logger.debug("Ignoring unknown lifecycle event %s in %s", event_name, path)
            continue
        if not isinstance(hook_list, list):
            continue
        for raw in hook_list:
            if not isinstance(raw, dict):
                continue
            hook_type = raw.get("type", "command")
            if hook_type not in HOOK_TYPES:
                _logger.debug("Ignoring unknown hook type %s in %s", hook_type, path)
                continue
            entries.append(
                HookEntry(
                    hook_type=hook_type,
                    event=event_name,
                    bash=raw.get("bash"),
                    command=raw.get("command"),
                    url=raw.get("url"),
                    headers=raw.get("headers"),
                    timeout_sec=raw.get("timeoutSec") or raw.get("timeout"),
                    cwd=raw.get("cwd"),
                    env=raw.get("env"),
                    source=source,
                    source_file=str(path),
                )
            )
    return entries


# -- Hook discovery ---------------------------------------------------------


def _get_policy_hooks_dir() -> Path:
    """Return the platform-specific policy hooks directory."""
    system = platform.system()
    if system == "Windows":
        return Path(r"C:\ProgramData\APM\policy.d")
    return Path("/etc/apm/policy.d")


def _get_user_hooks_dir() -> Path:
    """Return the user-level hooks directory (~/.apm/hooks/)."""
    apm_home = os.environ.get("APM_HOME")
    if apm_home:
        return Path(apm_home) / "hooks"
    return Path.home() / ".apm" / "hooks"


def _get_project_hooks_file(project_root: str | None = None) -> Path:
    """Return the project-level hooks file (``.apm/hooks.json``)."""
    root = Path(project_root) if project_root else Path.cwd()
    return root / ".apm" / "hooks.json"


def _load_hooks_from_dir(
    directory: Path,
    source: str,
) -> list[HookEntry]:
    """Load all ``*.json`` hook files from *directory*, sorted by name."""
    if not directory.is_dir():
        return []
    entries: list[HookEntry] = []
    for json_file in sorted(directory.glob("*.json")):
        if json_file.is_file():
            entries.extend(parse_hook_file(json_file, source=source))
    return entries


def discover_hooks(
    project_root: str | None = None,
) -> list[HookEntry]:
    """Discover and merge hooks from all three sources.

    Load order (all additive, policy first):
      1. Policy  -- ``/etc/apm/policy.d/*.json`` (directory)
      2. User    -- ``~/.apm/hooks/*.json`` (directory)
      3. Project -- ``.apm/hooks.json`` (single file)
    """
    hooks: list[HookEntry] = []
    hooks.extend(_load_hooks_from_dir(_get_policy_hooks_dir(), source="policy"))
    hooks.extend(_load_hooks_from_dir(_get_user_hooks_dir(), source="user"))

    project_file = _get_project_hooks_file(project_root)
    if project_file.is_file():
        hooks.extend(parse_hook_file(project_file, source="project"))

    return hooks


# -- Hook runner ------------------------------------------------------------


class LifecycleHookRunner:
    """Collects hooks and fires them for lifecycle events.

    All execution is fire-and-forget with error isolation.
    """

    def __init__(
        self,
        hooks: list[HookEntry] | None = None,
        logger: CommandLogger | None = None,
        verbose: bool = False,
        project_root: str | None = None,
    ) -> None:
        self._hooks = hooks or []
        self._logger = logger
        self._verbose = verbose
        self._project_root = project_root

    def fire(self, event_name: str, event: LifecycleEvent) -> list[threading.Thread]:
        """Execute all hooks registered for *event_name*.

        Each hook runs in isolation -- a failure in one hook does not
        prevent subsequent hooks from running.

        Returns a list of daemon threads started by HTTP hooks so
        callers can optionally join them (e.g. for test/dry-run).
        """
        from apm_cli.core.hook_executors import execute_hook

        matching = [h for h in self._hooks if h.event == event_name]
        if not matching:
            return []

        threads: list[threading.Thread] = []
        for hook in matching:
            try:
                thread = execute_hook(
                    hook,
                    event,
                    logger=self._logger,
                    verbose=self._verbose,
                    project_root=self._project_root,
                )
                if thread is not None:
                    threads.append(thread)
            except Exception:
                # Fire-and-forget: swallow all errors.
                _logger.debug(
                    "Lifecycle hook failed (type=%s, event=%s)",
                    hook.hook_type,
                    event_name,
                    exc_info=True,
                )
                if self._verbose and self._logger:
                    self._logger.verbose_detail(
                        f"[i] Lifecycle hook failed: {hook.hook_type} for {event_name}"
                    )
        return threads


# -- Convenience: build runner from file-based discovery -------------------


def build_runner_from_context(
    *,
    logger: Any = None,
    verbose: bool = False,
    project_root: str | None = None,
) -> LifecycleHookRunner:
    """Create a :class:`LifecycleHookRunner` via file-based discovery.

    Scans policy, user, and project hook directories for JSON files.
    """
    hooks = discover_hooks(project_root=project_root)

    return LifecycleHookRunner(
        hooks=hooks,
        logger=logger,
        verbose=verbose,
        project_root=project_root,
    )
