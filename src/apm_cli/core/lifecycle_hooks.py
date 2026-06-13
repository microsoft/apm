"""Lifecycle hook models, runner, and discovery.

APM supports lifecycle hooks that fire at key moments during install,
update, and uninstall operations.  Hooks are configured at three levels:

1. **Project** -- ``lifecycle_hooks:`` section in ``apm.yml``
2. **Global** -- ``lifecycle_hooks`` key in ``~/.apm/config.json``
3. **Policy** -- ``lifecycle_hooks:`` in ``apm-policy.yml`` (org-enforced)

Three action types are supported:

- ``command`` -- shell command executed via subprocess
- ``webhook`` -- HTTP POST to a URL with bearer-token auth
- ``script``  -- executable script file under the project root

All hooks are **fire-and-forget**: failures are logged in verbose mode
but never block the CLI.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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

# Supported hook action types.
HOOK_TYPES = ("command", "webhook", "script")


# -- Event model -----------------------------------------------------------


@dataclass
class PackageInfo:
    """Minimal package metadata carried in lifecycle events."""

    name: str
    reference: str | None = None


@dataclass
class LifecycleEvent:
    """Data payload passed to every lifecycle hook.

    Webhooks receive this as a JSON body.  Commands and scripts receive
    it via the ``APM_HOOK_EVENT`` environment variable (JSON-encoded).
    """

    schema_version: int = 1
    event: str = ""
    packages: list[PackageInfo] = field(default_factory=list)
    scope: str = "project"
    timestamp: str = ""
    cli_version: str = ""

    def to_json(self) -> str:
        """Serialise the event to a compact JSON string."""
        return json.dumps(asdict(self), separators=(",", ":"))

    @staticmethod
    def create(
        event: str,
        packages: list[PackageInfo] | None = None,
        scope: str = "project",
    ) -> LifecycleEvent:
        """Factory that auto-fills timestamp and CLI version."""
        from apm_cli.version import get_version

        return LifecycleEvent(
            event=event,
            packages=packages or [],
            scope=scope,
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            cli_version=get_version(),
        )


# -- Hook definition -------------------------------------------------------


@dataclass
class HookDefinition:
    """One configured lifecycle hook action.

    Attributes:
        hook_type: ``command``, ``webhook``, or ``script``.
        event:     Lifecycle event name (e.g. ``post-install``).
        run:       Shell command string (for ``command`` type).
        url:       Webhook endpoint URL (for ``webhook`` type).
        token_env: Name of the env var holding the bearer token
                   (for ``webhook`` type).
        path:      Script file path relative to project root
                   (for ``script`` type).
        source:    Where this hook was defined: ``project``, ``global``,
                   or ``policy``.
    """

    hook_type: str
    event: str
    run: str | None = None
    url: str | None = None
    token_env: str | None = None
    path: str | None = None
    source: str = "project"

    @property
    def identity_key(self) -> tuple[str, str, str]:
        """Deduplication key: (event, type, identifier)."""
        if self.hook_type == "command":
            return (self.event, self.hook_type, self.run or "")
        if self.hook_type == "webhook":
            return (self.event, self.hook_type, self.url or "")
        return (self.event, self.hook_type, self.path or "")


# -- Hook discovery ---------------------------------------------------------


def parse_hooks_from_config(
    raw: dict[str, Any],
    source: str = "project",
) -> list[HookDefinition]:
    """Parse a ``lifecycle_hooks`` mapping into :class:`HookDefinition` list.

    Accepts the shape used in both ``apm.yml`` and ``config.json``::

        lifecycle_hooks:
          post-install:
            - type: webhook
              url: https://...
              token_env: MY_TOKEN
            - type: command
              run: echo done
    """
    hooks: list[HookDefinition] = []
    if not isinstance(raw, dict):
        return hooks
    for event_name, entries in raw.items():
        if event_name not in LIFECYCLE_EVENTS:
            _logger.debug("Ignoring unknown lifecycle event: %s", event_name)
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            hook_type = entry.get("type", "")
            if hook_type not in HOOK_TYPES:
                _logger.debug("Ignoring unknown hook type: %s", hook_type)
                continue
            hooks.append(
                HookDefinition(
                    hook_type=hook_type,
                    event=event_name,
                    run=entry.get("run"),
                    url=entry.get("url"),
                    token_env=entry.get("token_env"),
                    path=entry.get("path"),
                    source=source,
                )
            )
    return hooks


def collect_hooks(
    project_hooks_raw: dict[str, Any] | None = None,
    global_hooks_raw: dict[str, Any] | None = None,
    policy_hooks_raw: dict[str, Any] | None = None,
) -> list[HookDefinition]:
    """Merge hooks from all three levels with deduplication.

    Merge order (policy first, then global, then project):
    - Policy hooks run first and cannot be removed by project.
    - Duplicates (same event + type + identifier) are skipped.
    """
    hooks: list[HookDefinition] = []
    seen: set[tuple[str, str, str]] = set()

    for raw, source in [
        (policy_hooks_raw, "policy"),
        (global_hooks_raw, "global"),
        (project_hooks_raw, "project"),
    ]:
        if raw is None:
            continue
        for hook in parse_hooks_from_config(raw, source=source):
            key = hook.identity_key
            if key not in seen:
                seen.add(key)
                hooks.append(hook)
    return hooks


# -- Hook runner ------------------------------------------------------------


class LifecycleHookRunner:
    """Collects hooks and fires them for lifecycle events.

    All execution is fire-and-forget with error isolation.
    """

    def __init__(
        self,
        hooks: list[HookDefinition] | None = None,
        logger: CommandLogger | None = None,
        verbose: bool = False,
        project_root: str | None = None,
    ) -> None:
        self._hooks = hooks or []
        self._logger = logger
        self._verbose = verbose
        self._project_root = project_root

    def fire(self, event_name: str, event: LifecycleEvent) -> None:
        """Execute all hooks registered for *event_name*.

        Each hook runs in isolation -- a failure in one hook does not
        prevent subsequent hooks from running.
        """
        from apm_cli.core.hook_executors import execute_hook

        matching = [h for h in self._hooks if h.event == event_name]
        if not matching:
            return

        for hook in matching:
            try:
                execute_hook(
                    hook,
                    event,
                    logger=self._logger,
                    verbose=self._verbose,
                    project_root=self._project_root,
                )
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


# -- Convenience: build runner from standard sources -----------------------


def build_runner_from_context(
    *,
    project_hooks_raw: dict[str, Any] | None = None,
    logger: Any = None,
    verbose: bool = False,
    project_root: str | None = None,
) -> LifecycleHookRunner:
    """Create a :class:`LifecycleHookRunner` using the standard 3-source merge.

    Collects hooks from:
      1. *project_hooks_raw* (apm.yml ``lifecycle_hooks`` already parsed)
      2. Global user config (``~/.apm/config.json``)
      3. Cached policy (if loaded)

    This is the single canonical path so that install, uninstall, and
    update do not duplicate the collection + policy look-up logic.
    """
    import contextlib

    from apm_cli.config import get_lifecycle_hooks

    global_hooks_raw = get_lifecycle_hooks()

    policy_hooks_raw = None
    with contextlib.suppress(Exception):
        from apm_cli.policy.discovery import get_cached_policy

        policy = get_cached_policy()
        if policy and hasattr(policy, "lifecycle_hooks"):
            policy_hooks_raw = getattr(policy.lifecycle_hooks, "require", None)

    hooks = collect_hooks(
        project_hooks_raw=project_hooks_raw,
        global_hooks_raw=global_hooks_raw,
        policy_hooks_raw=policy_hooks_raw,
    )

    return LifecycleHookRunner(
        hooks=hooks,
        logger=logger,
        verbose=verbose,
        project_root=project_root,
    )
