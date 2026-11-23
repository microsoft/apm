"""Single owner of executable admission (#3902).

``admit()`` answers ONE question for every dependency-declared executable
item -- one MCP server or one hook file -- across install, update,
``install --frozen``, ``apm audit --ci``, ``apm approve`` and the install
prompt:

    admitted = org/user/project deny ladder AND manifest approval
               AND (for pinned approvals) a matching definition fingerprint

The package-by-type rung stays :func:`resolve_exec_decision`; this module adds
the item rung on top. The lockfile is never an input: it records evidence of
what was deployed or parked, never consent.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apm_cli.core.scope import manifest_file

from .executables import (
    EXEC_TYPE_BIN,
    EXEC_TYPE_HOOKS,
    EXEC_TYPE_MCP,
    ITEM_EXEC_TYPES,
    LAYER_DEFAULT_DENY,
    LAYER_GATE_DISABLED,
    PLUGIN_GRANT_KEY,
    PLUGIN_ITEM,
    TRUST_DENIED,
    TRUST_DEPLOYED,
    TRUST_GATED,
    ExecTrustContext,
    _is_content_bound_key,
    _strip_version,
    resolve_exec_decision,
)
from .trust_fingerprint import (
    UnfingerprintableError,
    hook_file_fingerprint,
    hook_item_key,
    mcp_definition_fingerprint,
    native_plugin_fingerprint,
)

# Park reasons recorded as lockfile evidence (``mcp_omissions[*].reason``).
REASON_DENIED = "denied"
REASON_DEFINITION_CHANGED = "definition-changed"
REASON_UNTRUSTED_TRANSITIVE = "untrusted-transitive"
REASON_POLICY_TRANSITIVE = "policy-transitive"
REASON_UNAPPROVED = "unapproved-executables"
# A native plugin's executable surface points into APM-owned paths that the
# plugin pin does not cover (root ``.apm/`` or ``apm.yml``).
REASON_APM_REFERENCE = "apm-owned-reference"

LAYER_PROJECT_ITEM = "project-allow"
LAYER_USER_ITEM = "user-allow"
LAYER_ONE_OFF = "one-off"
LAYER_NOT_GATED = "not-gated"
LAYER_ORG_TRUST_TRANSITIVE = "org-trust-transitive"


@dataclass(frozen=True)
class TrustSubject:
    """One dependency-declared executable item awaiting admission.

    Attributes:
        package_keys: Lock-derived trust keys of the declaring package; never
            the package's self-declared name.
        exec_type: ``hooks`` or ``mcp``.
        item: Server name, or package-relative hook file path.
        fingerprint: Definition fingerprint, or ``None`` when the definition
            cannot be fingerprinted (it then never matches a pin).
        needs_transitive_trust: A depth>1 declaration that is more than a
            consumer-registry reference.
        exec_gated: Whether the executables gate decides this item.
    """

    package_keys: tuple[str, ...]
    exec_type: str
    item: str
    fingerprint: str | None
    needs_transitive_trust: bool = False
    exec_gated: bool = True
    # Declared by a dependency at depth > 1 (org ``mcp.trust_transitive``).
    transitive: bool = False


@dataclass(frozen=True)
class Admission:
    """The admission verdict for one :class:`TrustSubject`."""

    allowed: bool
    reason: str | None
    layer: str
    one_off: bool = False
    # Human-readable cause when the reason alone does not name it.
    detail: str | None = None
    # The whole-plugin pin that admitted a native plugin; deploy re-checks it.
    pin: str | None = None


def _names(package_keys: tuple[str, ...]) -> set[str]:
    return {_strip_version(key) for key in package_keys if key and not _is_content_bound_key(key)}


def item_pin(
    items: dict[str, dict[str, dict[str, str]]],
    package_keys: tuple[str, ...],
    exec_type: str,
    item: str,
) -> str | None:
    """Return the pinned fingerprint for one item, matched by exact identity."""
    names = _names(package_keys)
    for stored_key, entry in items.items():
        if _strip_version(stored_key) not in names or not isinstance(entry, dict):
            continue
        pinned = entry.get(exec_type)
        if isinstance(pinned, dict) and item in pinned:
            return pinned[item]
    return None


def has_item_pins(
    ctx: ExecTrustContext | None, package_keys: tuple[str, ...], exec_type: str
) -> bool:
    """Return whether any layer pins items of *exec_type* for this package."""
    if ctx is None:
        return False
    names = _names(package_keys)
    return any(
        _strip_version(stored_key) in names
        and isinstance(entry, dict)
        and isinstance(entry.get(exec_type), dict)
        for layer in (ctx.project_items, ctx.user_items)
        for stored_key, entry in layer.items()
    )


def pinned_layer(ctx: ExecTrustContext | None, subject: TrustSubject) -> tuple[str, str] | None:
    """Return ``(layer, pin)`` of the deciding item grant; project decides alone."""
    if ctx is None:
        return None
    for layer, items in (
        (LAYER_PROJECT_ITEM, ctx.project_items),
        (LAYER_USER_ITEM, ctx.user_items),
    ):
        pin = item_pin(items, subject.package_keys, subject.exec_type, subject.item)
        if pin is not None:
            return layer, pin
    return None


def admit(
    ctx: ExecTrustContext | None,
    subject: TrustSubject,
    *,
    one_off: bool = False,
) -> Admission:
    """Decide whether one dependency-declared executable item may deploy.

    Ladder (first match wins):

    1. not gated and no transitive trust needed       -> admit
    2. exec gate on: org / user / project deny         -> park ``denied``
    3. item pin (project layer decides alone, then user):
       fingerprint matches                             -> admit
       mismatch                                        -> park ``definition-changed``
                                                          (even a pin on a server that
                                                          no longer needs approval)
    4. transitive trust needed, no pin match:
       ``--trust-transitive-mcp`` this run             -> continue as one-off
       otherwise                                       -> park ``untrusted-transitive``
    5. exec gate on: type-level grant / org recommend  -> admit, else
                                                          park ``unapproved-executables``
    6. gate off                                        -> admit

    A legacy type-level ``mcp: true`` never satisfies rung 4: it is not pinned
    to content.
    """
    gate_on = ctx is not None and ctx.gate_enabled
    needs_trust = subject.needs_transitive_trust
    if (
        not needs_trust
        and not (subject.exec_gated and gate_on)
        and pinned_layer(ctx, subject) is None
    ):
        layer = LAYER_GATE_DISABLED if subject.exec_gated else LAYER_NOT_GATED
        return Admission(True, None, layer)

    # Deny wins over every grant, including a pin or the one-off flag.
    if gate_on:
        for key in subject.package_keys:
            decision = resolve_exec_decision(ctx, key, subject.exec_type)
            if decision.trust_state == TRUST_DENIED:
                return Admission(False, REASON_DENIED, decision.deciding_layer)

    pinned = pinned_layer(ctx, subject)
    if pinned is not None:
        layer, pin = pinned
        if subject.fingerprint is not None and pin == subject.fingerprint:
            return Admission(True, None, layer)
        # A changed definition always needs a re-approval; the one-off flag
        # never overrides a pin (req-sc-016).
        return Admission(False, REASON_DEFINITION_CHANGED, layer)

    if needs_trust and not one_off:
        return Admission(False, REASON_UNTRUSTED_TRANSITIVE, LAYER_DEFAULT_DENY)

    if subject.exec_gated and gate_on:
        for key in subject.package_keys:
            decision = resolve_exec_decision(ctx, key, subject.exec_type)
            if decision.allowed:
                return Admission(True, None, decision.deciding_layer, one_off=needs_trust)
        return Admission(False, REASON_UNAPPROVED, LAYER_DEFAULT_DENY)

    return Admission(True, None, LAYER_ONE_OFF, one_off=needs_trust)


# -------------------------------------------------------------------
# MCP admission (one instance per command run)
# -------------------------------------------------------------------


def resolve_registry_entry(dependency: Any) -> dict[str, Any] | None:
    """Return the stable registry entry a registry-resolved server deploys from.

    ``None`` when the registry cannot be reached or does not know the server;
    the caller then cannot verify a pin and parks the server.
    """
    from apm_cli.integration.mcp_integrator_install import _registry_group_key
    from apm_cli.integration.mcp_transport_resolution import _fetch_registry_infos

    _url, infos = _fetch_registry_infos(_registry_group_key(dependency), [dependency.name])
    info = infos.get(dependency.name)
    if not isinstance(info, dict):
        return None
    return {
        key: copy.deepcopy(info[key])
        for key in ("name", "version", "packages", "remotes")
        if key in info
    }


def transitive_server_listed(name: str, mcp: Any) -> bool:
    """Whether org ``mcp.allow`` lists transitive server *name* (req-sc-016).

    The one eligibility decision under ``mcp.trust_transitive: false``, for
    admission and the policy check alike. Only the allow list decides it;
    ``mcp.deny`` is judged by its own check.
    """
    from apm_cli.policy.matcher import first_matching_pattern

    return mcp.allow is not None and first_matching_pattern(name, mcp.allow) is not None


def withholds_transitive(policy: Any) -> bool:
    """Whether org policy admits transitive MCP servers only through ``mcp.allow``.

    True when the policy enforces (``block`` or ``warn``) and does not set
    ``mcp.trust_transitive: true``; the caller applies the enforcement mode.
    """
    mcp = getattr(policy, "mcp", None)
    if mcp is None or mcp.trust_transitive:
        return False
    return getattr(policy, "enforcement", None) in ("block", "warn")


def unlisted_transitive_servers(names: Iterable[str], policy: Any) -> tuple[str, ...]:
    """The transitive servers in *names* org ``mcp.trust_transitive`` withholds."""
    if not withholds_transitive(policy):
        return ()
    return tuple(name for name in names if not transitive_server_listed(name, policy.mcp))


class McpAdmission:
    """MCP admission inputs for one command run.

    The trust context is resolved lazily, once, and only when a
    dependency-declared server needs a decision, so commands that never meet a
    gated server never discover org policy.
    """

    def __init__(
        self,
        trust: Callable[[], ExecTrustContext | None] | ExecTrustContext | None = None,
        *,
        one_off: bool = False,
        registry_resolver: Callable[[Any], dict[str, Any] | None] | None = None,
        permissive: bool = False,
        pins: Callable[[], ExecTrustContext | None] | None = None,
        org_policy: Callable[[], Any] | None = None,
    ) -> None:
        self._trust = trust
        # Lazy org policy for the mcp.trust_transitive rung; None: no policy.
        self._org_policy = org_policy
        self._org_policy_value: Any = None
        self._org_policy_resolved = org_policy is None
        self._resolved = not callable(trust)
        self._ctx = None if callable(trust) else trust
        # Policy-free source of item pins, so checking for an existing pin
        # never forces org policy discovery.
        self._pins = pins
        self._pins_ctx: ExecTrustContext | None = None
        self._pins_resolved = False
        self.one_off = one_off
        self.permissive = permissive
        self._registry_resolver = registry_resolver or resolve_registry_entry
        # Why org policy could not be read, when it could not; callers surface it.
        self.policy_unavailable: str | None = None

    @classmethod
    def admit_all(cls) -> McpAdmission:
        """Admission for paths that never deploy a new server (uninstall)."""
        return cls(None, one_off=True, permissive=True)

    @classmethod
    def for_project(
        cls,
        project_root: Path,
        *,
        user_scope: bool,
        one_off: bool = False,
        no_policy: bool = False,
        include_user: bool = True,
        logger: Any = None,
        trust_context: ExecTrustContext | None = None,
        migrate_user_legacy: bool = True,
    ) -> McpAdmission:
        """Resolve org, project and (optionally) user layers for *project_root*.

        Org policy and the grants both come from the scope's manifest root
        (``$APM_HOME`` at user scope). A preview passes
        ``migrate_user_legacy=False`` to read a legacy ``approvals.yml``
        without migrating it.
        """
        from apm_cli.core.scope import manifest_root, scope_manifest_path

        policy_root = manifest_root(project_root, user_scope=user_scope)
        routes: list[Any] = []

        def route() -> Any:
            if not routes:
                from apm_cli.policy.outcome_routing import classify_project_policy

                route = classify_project_policy(policy_root)
                routes.append(route)
            return routes[0]

        def org_policy() -> Any:
            return None if no_policy else route().policy

        if trust_context is not None:
            return cls(trust_context, one_off=one_off, org_policy=org_policy)
        grants_manifest = scope_manifest_path(project_root, user_scope=user_scope)
        admission: McpAdmission

        def resolve() -> ExecTrustContext:
            from .executables import exec_trust_context_for_project

            policy = None
            if not no_policy:
                routed = route()
                policy = routed.policy
                if policy is None and (routed.blocks or routed.unresolved):
                    admission.policy_unavailable = routed.outcome
                elif routed.unavailable_source:
                    # A --policy source that did not load leaves the policy partial (#1925).
                    admission.policy_unavailable = routed.unavailable_source
            return exec_trust_context_for_project(
                grants_manifest,
                policy=policy,
                logger=logger,
                include_user=include_user,
                migrate_user_legacy=migrate_user_legacy,
            )

        def pins() -> ExecTrustContext:
            from .executables import exec_trust_context_for_project

            return exec_trust_context_for_project(
                grants_manifest,
                policy=None,
                logger=None,
                include_user=include_user,
                migrate_user_legacy=migrate_user_legacy,
            )

        admission = cls(resolve, one_off=one_off, pins=pins, org_policy=org_policy)
        return admission

    @property
    def context(self) -> ExecTrustContext | None:
        if not self._resolved:
            self._resolved = True
            self._ctx = self._trust()  # type: ignore[operator]
        return self._ctx

    def _has_pin(self, subject: TrustSubject) -> bool:
        if self._resolved or self._pins is None:
            return pinned_layer(self.context, subject) is not None
        if not self._pins_resolved:
            self._pins_resolved = True
            try:
                self._pins_ctx = self._pins()
            except ValueError:
                # A malformed grant: use the full context, which reports it.
                self._pins = None
                return pinned_layer(self.context, subject) is not None
        return pinned_layer(self._pins_ctx, subject) is not None

    def subject(self, locked_dependency: Any, declaration: Any, package_keys: tuple[str, ...]):
        """Build the subject for one dependency-declared MCP server."""
        needs_trust = bool(declaration.requires_transitive_trust) and locked_dependency.depth > 1
        exec_gated = bool(declaration.is_self_defined)
        fingerprint: str | None = None
        probe = TrustSubject(package_keys, EXEC_TYPE_MCP, declaration.name, None)
        # An existing pin is always checked, even when the new declaration
        # would not need an approval (for example self-defined -> registry).
        if needs_trust or exec_gated or self._has_pin(probe):
            fingerprint = self._fingerprint(declaration, package_keys, needs_trust)
        return TrustSubject(
            package_keys=package_keys,
            exec_type=EXEC_TYPE_MCP,
            item=declaration.name,
            fingerprint=fingerprint,
            needs_transitive_trust=needs_trust,
            exec_gated=exec_gated,
            transitive=(getattr(locked_dependency, "depth", None) or 1) > 1,
        )

    def org_policy(self) -> Any:
        """The effective org policy (resolved once), or ``None`` without one."""
        if not self._org_policy_resolved:
            self._org_policy_resolved = True
            self._org_policy_value = self._org_policy()  # type: ignore[misc]
        return self._org_policy_value

    def refuses_transitive(self, name: str) -> bool:
        """Whether org ``mcp.trust_transitive: false`` withholds transitive server *name*.

        Under that policy with ``enforcement: block`` a server a dependency at
        depth > 1 declares deploys only when ``mcp.allow`` admits it: a
        registry reference, an approval pin and ``--trust-transitive-mcp``
        alike (req-sc-016).
        """
        policy = self.org_policy()
        # Under enforcement: warn the server deploys and the merged-set
        # preflight warns, as for mcp.deny (policy_checks.mcp_policy_checks).
        if getattr(policy, "enforcement", None) != "block":
            return False
        return bool(unlisted_transitive_servers((name,), policy))

    def _fingerprint(self, declaration: Any, package_keys: tuple[str, ...], needs_trust: bool):
        registry_entry = None
        if not declaration.is_self_defined and needs_trust:
            # Only a pinned registry-resolved server pays for a registry lookup.
            probe = TrustSubject(package_keys, EXEC_TYPE_MCP, declaration.name, None)
            if pinned_layer(self.context, probe) is None:
                return None
            registry_entry = self._registry_resolver(declaration)
            if registry_entry is None:
                return None
        try:
            return mcp_definition_fingerprint(declaration, registry_entry=registry_entry)
        except UnfingerprintableError:
            return None

    def admit(self, subject: TrustSubject) -> Admission:
        if self.permissive:
            return Admission(True, None, LAYER_ONE_OFF)
        if subject.transitive and self.refuses_transitive(subject.item):
            return Admission(False, REASON_POLICY_TRANSITIVE, LAYER_ORG_TRUST_TRANSITIVE)
        if (
            not subject.needs_transitive_trust
            and not subject.exec_gated
            and not self._has_pin(subject)
        ):
            return Admission(True, None, LAYER_NOT_GATED)
        return admit(self.context, subject, one_off=self.one_off)


# -------------------------------------------------------------------
# Hook admission (per hook file)
# -------------------------------------------------------------------


def hook_subject(
    package_keys: tuple[str, ...], package_path: Path, hook_file: Path
) -> TrustSubject:
    """Build the subject for one hook file of a dependency package."""
    try:
        fingerprint = hook_file_fingerprint(package_path, hook_file)
    except (UnfingerprintableError, OSError, ValueError):
        fingerprint = None
    return TrustSubject(
        package_keys=package_keys,
        exec_type=EXEC_TYPE_HOOKS,
        item=hook_item_key(package_path, hook_file),
        fingerprint=fingerprint,
    )


class HookFileAdmission:
    """Per-hook-file admission predicate; remembers what it parked and why."""

    def __init__(self, ctx: ExecTrustContext, package_keys: tuple[str, ...], package_path: Path):
        self._ctx = ctx
        self._keys = package_keys
        self._path = package_path
        self.parked: list[tuple[str, str]] = []
        self._verdicts: dict[str, bool] = {}

    def __call__(self, hook_file: Path) -> bool:
        item = hook_item_key(self._path, hook_file)
        if item not in self._verdicts:
            verdict = admit(self._ctx, hook_subject(self._keys, self._path, hook_file))
            if not verdict.allowed:
                self.parked.append((item, str(verdict.reason)))
            self._verdicts[item] = verdict.allowed
        return self._verdicts[item]

    def admits_any(self) -> bool:
        """Return whether some hook file of the package is admitted on its pin."""
        from apm_cli.integration.hook_integrator import HookIntegrator

        verdicts = [self(hook_file) for hook_file in HookIntegrator().find_hook_files(self._path)]
        return any(verdicts)


def hook_file_filter(
    allow_executables: Any,
    package_keys: tuple[str, ...],
    package_path: Path,
) -> HookFileAdmission | None:
    """Return a per-hook-file admission predicate when items are pinned.

    ``None`` means the package-level decision applies to every hook file
    (no pinned items for this package).
    """
    ctx = getattr(allow_executables, "trust_context", None)
    if ctx is None or not has_item_pins(ctx, package_keys, EXEC_TYPE_HOOKS):
        return None
    return HookFileAdmission(ctx, package_keys, package_path)


def _shell_argument(argument: str) -> str:
    """One shell word for a package identity; options such as ``--pending`` pass through."""
    import shlex

    return argument if argument.startswith("-") else shlex.quote(argument)


def approve_command(argument: str, *, user_scope: bool = False, user_store: bool = False) -> str:
    """Return the ``apm approve`` command that clears a parked executable.

    Remediation catalog ids ``executables-unapproved`` (project) and
    ``executables-unapproved-user-scope`` (``-g``): a package installed with
    ``apm install -g`` is approved with ``apm approve -g`` (#4154). ``-g``
    selects the installation; ``--user`` (*user_store*) only the store.
    """
    flags = ("-g " if user_scope else "") + ("--user " if user_store else "")
    return f"apm approve {flags}{_shell_argument(argument)}"


def warn_one_off_trust(one_off: tuple[tuple[str, str], ...], logger: Any) -> None:
    """Say that --trust-transitive-mcp consent lasts for this run only."""
    if not one_off:
        return
    noun = "server" if len(one_off) == 1 else "servers"
    shown = "; ".join(f"{name}, {package}" for name, package in one_off[:3])
    more = f"; and {len(one_off) - 3} more" if len(one_off) > 3 else ""
    first_package = one_off[0][1]
    logger.warning(
        f"--trust-transitive-mcp deployed {len(one_off)} unapproved MCP {noun} for this run "
        f"only ({shown}{more})\n"
        "    It is not saved: the next install without the flag removes it. "
        f"Keep it: apm approve {first_package}",
        symbol="warning",
    )


def explain_command(package: str, *, user_scope: bool = False) -> str:
    """Return the ``apm policy explain`` command for a parked package's scope."""
    flag = "-g " if user_scope else ""
    return f"apm policy explain {flag}{_shell_argument(package)}"


def parked_hook_message(package: str, item: str, reason: str, *, user_scope: bool = False) -> str:
    """Return the warning for one pinned hook file the trust owner parked."""
    if reason == REASON_DENIED:
        # apm approve cannot clear a deny; point at the deciding layer instead.
        return (
            f"Parked hook file '{item}' ({package}): denied\n"
            f"    Explain: {explain_command(package, user_scope=user_scope)}"
        )
    why = (
        "definition changed since approval"
        if reason == REASON_DEFINITION_CHANGED
        else ("not approved")
    )
    return (
        f"Parked hook file '{item}' ({package}): {why}\n"
        f"    Review: {approve_command('--pending', user_scope=user_scope)}  |  "
        f"Re-approve: {approve_command(package, user_scope=user_scope)}"
    )


# -------------------------------------------------------------------
# Native plugins: ONE approval item over the whole plugin root
# -------------------------------------------------------------------

_PLUGIN_MANIFESTS = ("plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json")
# Plugin-root directories a harness auto-loads to run commands (Claude Code).
_COMMAND_RUNNING_ROOTS = ("monitors", "workflows")
_MANIFEST_EXEC_KEYS = {"hooks": EXEC_TYPE_HOOKS, "mcpServers": EXEC_TYPE_MCP, "lspServers": "lsp"}


def native_root_exec_types(plugin_root: Path | None) -> tuple[str, ...]:
    """Exec trust types implied by NATIVE-layout components at a plugin root.

    ``scan_package_executables`` only understands APM's ``.apm/`` layout. A
    native plugin keeps hooks, LSP, extensions and prompt payloads at the
    ROOT, and the harness loads them live. The mapping follows the Agent
    Plugins loader's own constant, so an unmapped component fails closed as
    hooks. Prompt payloads (agents/commands/instructions) map to the hooks
    gate rather than silently to nothing. Claude Code also auto-runs the
    commands in ``monitors/`` and ``workflows/``; they gate as hooks for every
    harness, so an unvetted command surface is never ``not-gated``.
    """
    from apm_cli.agent_plugins.loader import _IGNORED_PORTABLE_COMPONENT_PATHS

    from .executables import EXEC_TYPE_CANVAS, EXEC_TYPE_LSP

    if plugin_root is None:
        return ()
    root = Path(plugin_root)
    mapping = {
        "extensions": EXEC_TYPE_CANVAS,
        "lsp.json": EXEC_TYPE_LSP,
        "hooks": EXEC_TYPE_HOOKS,
        "agents": EXEC_TYPE_HOOKS,
        "commands": EXEC_TYPE_HOOKS,
        "instructions": EXEC_TYPE_HOOKS,
    }
    seen: dict[str, None] = {}
    for name in (*_IGNORED_PORTABLE_COMPONENT_PATHS, *_COMMAND_RUNNING_ROOTS):
        try:
            if not (root / name).exists():
                continue
        except OSError:
            continue
        seen.setdefault(mapping.get(name, EXEC_TYPE_HOOKS), None)
    return tuple(seen)


def agent_plugin_exec_types(plugin_root: Path | None) -> tuple[str, ...]:
    """Exec types an Agent Plugin's root ``mcp.json`` declares, per the loader's IR.

    ``mcp`` for any server, plus ``bin`` when a server runs a command. A root
    ``mcp.json`` the loader cannot read still counts as ``mcp`` (fail closed):
    the harness loads the plugin directory live.
    """
    if plugin_root is None:
        return ()
    from apm_cli.agent_plugins.errors import AgentPluginError
    from apm_cli.agent_plugins.loader import detect_agent_plugin

    root = Path(plugin_root)
    try:
        detection = detect_agent_plugin(root)
    except (AgentPluginError, OSError):
        detection = None
    if detection is None:
        return ()
    components = getattr(detection.plugin, "components", None)
    servers = getattr(components, "mcp_servers", ()) if components is not None else ()
    types: list[str] = []
    if servers or (root / "mcp.json").is_file():
        types.append(EXEC_TYPE_MCP)
    if any(getattr(server, "executables", ()) for server in servers):
        types.append(EXEC_TYPE_BIN)
    return tuple(types)


def native_plugin_exec_types(plugin_root: Path) -> tuple[str, ...]:
    """Executable types a natively loaded plugin declares (the native gate's union).

    The single source for every native path: install admission, lockfile
    status, ``apm approve`` and ``apm policy explain`` all read this set.
    """
    import json

    from .executables import scan_package_executables

    root = Path(plugin_root)
    found: dict[str, None] = dict.fromkeys(scan_package_executables(root, "", "").exec_types)
    found.update(dict.fromkeys(agent_plugin_exec_types(root)))
    found.update(dict.fromkeys(native_root_exec_types(root)))
    markers = {
        "hooks": EXEC_TYPE_HOOKS,
        ".mcp.json": EXEC_TYPE_MCP,
        ".lsp.json": "lsp",
        "lsp.json": "lsp",
        "bin": "bin",
        "extensions": "canvas",
    }
    for name, exec_type in markers.items():
        if (root / name).exists():
            found[exec_type] = None
    for name in _PLUGIN_MANIFESTS:
        manifest = root / name
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            for key, exec_type in _MANIFEST_EXEC_KEYS.items():
                if data.get(key):
                    found[exec_type] = None
    return tuple(found)


# Executable surfaces of a native plugin whose text may reference other files.
_NATIVE_SURFACES = (
    "hooks",
    "bin",
    ".mcp.json",
    ".lsp.json",
    "lsp.json",
    *_COMMAND_RUNNING_ROOTS,
    *_PLUGIN_MANIFESTS,
)
_APM_OWNED_REFERENCE = re.compile(r"(?<![\w.-])(\.apm/[^\s\"']*|apm\.yml)(?![\w.-])")


def apm_owned_reference(plugin_root: Path) -> str | None:
    """Name the first reference from an executable surface into ``.apm/`` or ``apm.yml``.

    Those root paths are APM normalization output and sit outside the plugin
    pin, so a plugin that runs anything from them cannot be pinned.
    """
    root = Path(plugin_root)
    for name in _NATIVE_SURFACES:
        base = root / name
        try:
            if base.is_file():
                files = [base]
            elif base.is_dir():
                files = sorted(p for p in base.rglob("*") if p.is_file())
            else:
                continue
        except OSError:
            continue
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            match = _APM_OWNED_REFERENCE.search(text)
            if match:
                return f"{path.relative_to(root).as_posix()} references '{match.group(1)}'"
    return None


def unpinned_apm_copy(plugin_root: Path) -> str | None:
    """Name a root ``.apm/`` file the plugin pin does not bind (fail closed)."""
    from apm_cli.utils.content_hash import unpinned_apm_copy as find

    return find(Path(plugin_root))


def plugin_pin(
    ctx: ExecTrustContext | None, package_keys: tuple[str, ...]
) -> tuple[str, str] | None:
    """Return ``(layer, pin)`` of the package's whole-plugin approval, if any."""
    return pinned_layer(ctx, TrustSubject(package_keys, PLUGIN_GRANT_KEY, PLUGIN_ITEM, None))


def _trust_context(allow_executables: Any) -> ExecTrustContext | None:
    if isinstance(allow_executables, ExecTrustContext):
        return allow_executables
    ctx = getattr(allow_executables, "trust_context", None)
    if ctx is None and isinstance(allow_executables, dict):
        # A bare grant map (gate on, no routed context) fails closed: only
        # its own type-level grants apply.
        from .executables import build_exec_trust_context

        ctx = build_exec_trust_context(
            policy=None,
            project_data={"executables": {"allow": dict(allow_executables)}},
            include_user=False,
            migrate_user_legacy=False,
        )
    return ctx


def admit_native_plugin(
    allow_executables: Any,
    package_keys: tuple[str, ...],
    plugin_root: Path,
    *,
    transitive: bool = False,
    one_off: bool = False,
) -> Admission:
    """Decide whether a natively loaded plugin may run, as one unit.

    The harness loads the plugin directory intact, so approval binds ONE pin
    (``executables.allow.<pkg>.plugin``) to :func:`native_plugin_fingerprint`
    of *plugin_root* (the ``apm_modules`` source). *allow_executables* is an
    ``EffectiveExecMap``, an :class:`ExecTrustContext`, or ``None`` (gate off).
    *one_off* is ``--trust-transitive-mcp`` for this run.

    - transitive plugin with MCP servers, gate off:
      decided by :func:`admit` rungs 3-4 (pin, one-off, else parked
      ``untrusted-transitive``), as for a loose transitive server
    - gate off                             -> admitted
    - no declared executables and no pin   -> admitted, no pin needed
    - org / user / project deny            -> parked ``denied``
    - executable surface references root
      ``.apm/`` or ``apm.yml``             -> parked ``apm-owned-reference``
    - plugin pin matches                   -> admitted
    - plugin pin differs (any byte/mode)   -> parked ``definition-changed``
    - transitive with MCP, not one-off     -> parked ``untrusted-transitive``
    - no pin, every declared type granted  -> admitted (legacy, warned)
    - otherwise                            -> parked ``unapproved-executables``
    """
    ctx = _trust_context(allow_executables)
    gate_on = ctx is not None and ctx.gate_enabled
    if not gate_on and not transitive:
        return Admission(True, None, LAYER_GATE_DISABLED)
    exec_types = native_plugin_exec_types(plugin_root)
    needs_trust = transitive and EXEC_TYPE_MCP in exec_types
    if not gate_on:
        if not needs_trust:
            return Admission(True, None, LAYER_GATE_DISABLED)
        try:
            fingerprint: str | None = native_plugin_fingerprint(Path(plugin_root))
        except (OSError, ValueError):
            fingerprint = None
        subject = TrustSubject(
            package_keys,
            PLUGIN_GRANT_KEY,
            PLUGIN_ITEM,
            fingerprint,
            needs_transitive_trust=True,
            exec_gated=False,
        )
        return admit(ctx, subject, one_off=one_off)
    pinned = plugin_pin(ctx, package_keys)
    if not exec_types and pinned is None:
        return Admission(True, None, LAYER_NOT_GATED)
    # A pin is checked against deny and content even if no type is detected.
    for exec_type in exec_types or (EXEC_TYPE_HOOKS,):
        for key in package_keys:
            decision = resolve_exec_decision(ctx, key, exec_type)
            if decision.trust_state == TRUST_DENIED:
                return Admission(False, REASON_DENIED, decision.deciding_layer)
    reference = apm_owned_reference(Path(plugin_root)) or unpinned_apm_copy(Path(plugin_root))
    if reference is not None:
        return Admission(False, REASON_APM_REFERENCE, LAYER_DEFAULT_DENY, detail=reference)
    if pinned is not None:
        layer, pin = pinned
        try:
            current = native_plugin_fingerprint(Path(plugin_root))
        except (OSError, ValueError):
            current = None
        if current is not None and current == pin:
            return Admission(True, None, layer, pin=pin)
        return Admission(False, REASON_DEFINITION_CHANGED, layer)
    if needs_trust and not one_off:
        # D2: a type-level grant never satisfies transitive MCP trust.
        return Admission(False, REASON_UNTRUSTED_TRANSITIVE, LAYER_DEFAULT_DENY)
    decisions = [_type_decision(ctx, package_keys, exec_type) for exec_type in exec_types]
    if decisions and all(decision.allowed for decision in decisions):
        return Admission(True, None, decisions[0].deciding_layer, one_off=needs_trust)
    missing = [t for t, decision in zip(exec_types, decisions, strict=True) if not decision.allowed]
    # Name the gap only when some type is granted: the grant is incomplete.
    detail = ", ".join(missing) if len(missing) < len(decisions) else None
    return Admission(False, REASON_UNAPPROVED, LAYER_DEFAULT_DENY, detail=detail)


def native_declaration(declaration: Any, plugin_root: Path) -> Any:
    """Return *declaration* carrying the native gate's type set for *plugin_root*."""
    from dataclasses import replace

    return replace(declaration, native_exec_types=native_plugin_exec_types(plugin_root))


def parked_plugin_message(
    package: str, reason: str, detail: str | None = None, *, user_scope: bool = False
) -> str:
    """Return the warning for a native plugin the trust owner parked."""
    if reason == REASON_APM_REFERENCE:
        return (
            f"Parked native plugin {package}: {detail or 'an executable references .apm/'}; "
            "APM cannot pin files under .apm/ or apm.yml, so this plugin cannot be approved\n"
            "    Ask the publisher to move them out of .apm/ and apm.yml"
        )
    if reason == REASON_DENIED:
        return (
            f"Parked native plugin {package}: denied\n"
            f"    Explain: {explain_command(package, user_scope=user_scope)}"
        )
    if reason == REASON_UNTRUSTED_TRANSITIVE:
        return (
            f"Parked native plugin {package}: a transitive plugin with MCP servers needs "
            "a pinned approval; 'mcp: true' is not enough\n"
            f"    Pin: {approve_command(package, user_scope=user_scope)}  |  "
            "This run only: --trust-transitive-mcp"
        )
    why = (
        "content changed since approval" if reason == REASON_DEFINITION_CHANGED else "not approved"
    )
    if reason == REASON_UNAPPROVED and detail:
        why = f"not approved (missing grant: {detail})"
    return (
        f"Parked native plugin {package}: {why}\n"
        f"    Review: {approve_command('--pending', user_scope=user_scope)}  |  "
        f"Re-approve: {approve_command(package, user_scope=user_scope)}"
    )


def hooks_package_admissible(
    allow_executables: Any, package_keys: tuple[str, ...], package_info: Any
) -> bool:
    """Return whether pinned hook items admit some hook file of the package.

    A package none of whose hook files matches its pin is not admissible, so
    install retires the hooks it deployed earlier, as for a revoked approval.
    """
    ctx = getattr(allow_executables, "trust_context", None)
    if ctx is None or not has_item_pins(ctx, package_keys, EXEC_TYPE_HOOKS):
        return False
    # admit() applies the deny ladder first, so a denied package admits nothing.
    return HookFileAdmission(ctx, package_keys, Path(package_info.install_path)).admits_any()


@dataclass(frozen=True)
class HookPinMismatch:
    """A hook file the lockfile records as deployed that its approval no longer admits."""

    package: str
    item: str
    reason: str

    def describe(self) -> str:
        why = (
            "its definition changed since approval"
            if self.reason == REASON_DEFINITION_CHANGED
            else "it is not approved"
        )
        return (
            f"{self.package}: hook file '{self.item}' is deployed per apm.lock.yaml but "
            f"{why}; re-approve with 'apm approve {self.package}' or retire it with 'apm install'"
        )


def recorded_hook_pin_mismatches(
    ctx: ExecTrustContext | Callable[[], ExecTrustContext | None] | None,
    package_keys: tuple[str, ...],
    install_path: Path,
    *,
    package: str,
    recorded_status: str | None,
    recorded_allowed: tuple[str, ...] = (),
) -> list[HookPinMismatch]:
    """Return the recorded-deployed hook files whose pinned approval no longer admits them.

    ``apm install --frozen`` (before anything deploys) and ``apm audit --ci``
    both ask this, so they reach one verdict (#4188). A deny is not reported
    here: the deny ladder owns that verdict. *ctx* may be a provider; it is
    called only for a package the lock records as deploying hook files.
    """
    from apm_cli.integration.hook_integrator import HookIntegrator

    from .executables import TRUST_DEPLOYED

    recorded_deployed = (
        recorded_status in (None, TRUST_DEPLOYED) or EXEC_TYPE_HOOKS in recorded_allowed
    )
    root = install_path.resolve()
    hook_files = HookIntegrator().find_hook_files(root) if recorded_deployed else []
    if not hook_files:
        return []
    if callable(ctx):
        ctx = ctx()
    if ctx is None or not ctx.gate_enabled or not has_item_pins(ctx, package_keys, EXEC_TYPE_HOOKS):
        return []
    mismatches: list[HookPinMismatch] = []
    for hook_file in hook_files:
        verdict = admit(ctx, hook_subject(package_keys, root, hook_file))
        if not verdict.allowed and verdict.reason != REASON_DENIED:
            mismatches.append(
                HookPinMismatch(package, hook_item_key(root, hook_file), str(verdict.reason))
            )
    return mismatches


def package_mcp_declarations(install_path: Path) -> list[Any]:
    """Parse a package manifest's MCP entries once, through the canonical model."""
    from apm_cli.models.dependency.mcp import MCPDependency
    from apm_cli.utils.yaml_io import load_yaml

    manifest = manifest_file(install_path)
    try:
        data = load_yaml(manifest) if manifest.is_file() and not manifest.is_symlink() else {}
    except Exception:
        data = {}
    deps = data.get("dependencies") if isinstance(data, dict) else None
    entries = deps.get("mcp") if isinstance(deps, dict) else None
    declarations = []
    for entry in entries if isinstance(entries, list) else ():
        try:
            declarations.append(
                MCPDependency.from_string(entry)
                if isinstance(entry, str)
                else MCPDependency.from_dict(entry)
            )
        except (ValueError, TypeError):
            continue
    return declarations


def is_pinnable_mcp(declaration: Any, *, transitive: bool) -> bool:
    """Whether a dependency MCP server needs an approval (and so can be pinned)."""
    return bool(declaration.is_self_defined) or (
        transitive and bool(declaration.requires_transitive_trust)
    )


def _package_mcp_subjects(
    ctx: ExecTrustContext | None,
    package_keys: tuple[str, ...],
    install_path: Path,
    *,
    transitive: bool,
    registry_resolver: Callable[[Any], dict[str, Any] | None] | None,
) -> list[TrustSubject]:
    """Subjects for every pinnable MCP server, built exactly as install builds them."""
    admission = McpAdmission(ctx, registry_resolver=registry_resolver)
    locked = _Depth(2 if transitive else 1)
    return [
        admission.subject(locked, declaration, package_keys)
        for declaration in package_mcp_declarations(install_path)
        if is_pinnable_mcp(declaration, transitive=transitive)
    ]


@dataclass(frozen=True)
class _Depth:
    depth: int


def _parked_mcp_state(
    ctx: ExecTrustContext | None,
    package_keys: tuple[str, ...],
    install_path: Path,
    transitive: bool,
    registry_resolver: Callable[[Any], dict[str, Any] | None] | None,
) -> str | None:
    """``gated``/``denied`` when install would park one of the package's MCP servers."""
    from .executables import TRUST_GATED

    if not transitive:
        return None
    subjects = _package_mcp_subjects(
        ctx, package_keys, install_path, transitive=True, registry_resolver=registry_resolver
    )
    verdicts = [admit(ctx, subject) for subject in subjects]
    if any(verdict.reason == REASON_DENIED for verdict in verdicts):
        return TRUST_DENIED
    if any(not verdict.allowed for verdict in verdicts):
        return TRUST_GATED
    return None


def pinned_exec_status(
    ctx: ExecTrustContext | None,
    package_keys: tuple[str, ...],
    install_path: Path,
    exec_types: tuple[str, ...] | list[str],
    *,
    transitive: bool = False,
    registry_resolver: Callable[[Any], dict[str, Any] | None] | None = None,
) -> dict[str, str]:
    """Lockfile ``exec_status`` for each declared type whose grants are pinned.

    Returns ``{exec_type: deployed | gated_pending_approval | denied}`` only for
    types the package pins; other types keep the type-level resolution.
    """
    from apm_cli.integration.hook_integrator import HookIntegrator

    from .executables import TRUST_DEPLOYED, TRUST_GATED

    result: dict[str, str] = {}
    for exec_type in ITEM_EXEC_TYPES:
        if exec_type not in exec_types or not has_item_pins(ctx, package_keys, exec_type):
            continue
        root = install_path.resolve()
        if exec_type == EXEC_TYPE_HOOKS:
            subjects = [
                hook_subject(package_keys, root, hook_file)
                for hook_file in HookIntegrator().find_hook_files(root)
            ]
        else:
            subjects = _package_mcp_subjects(
                ctx,
                package_keys,
                root,
                transitive=transitive,
                registry_resolver=registry_resolver,
            )
        verdicts = [admit(ctx, subject) for subject in subjects]
        if any(verdict.reason == REASON_DENIED for verdict in verdicts):
            result[exec_type] = TRUST_DENIED
        elif all(verdict.allowed for verdict in verdicts):
            result[exec_type] = TRUST_DEPLOYED
        else:
            result[exec_type] = TRUST_GATED
    return result


@dataclass(frozen=True)
class TypeVerdict:
    """The verdict for one executable type of one package, pins included."""

    allowed: bool
    state: str
    layer: str
    pinned: bool = False
    content_blind: bool = False
    shadowed: tuple[str, ...] = ()


def _type_decision(ctx: ExecTrustContext, package_keys: tuple[str, ...], exec_type: str):
    """Best decision across the candidate keys (allowed > denied > gated)."""
    decisions = [resolve_exec_decision(ctx, key, exec_type) for key in package_keys]
    for wanted in (lambda d: d.allowed, lambda d: d.trust_state == TRUST_DENIED):
        for decision in decisions:
            if wanted(decision):
                return decision
    return decisions[0]


def package_grants_type(
    ctx: ExecTrustContext, package_keys: tuple[str, ...], exec_type: str
) -> bool:
    """Whether any of a package's approval keys still grants *exec_type*."""
    return bool(package_keys) and _type_decision(ctx, package_keys, exec_type).allowed


def package_denies_type(
    ctx: ExecTrustContext | None, package_keys: tuple[str, ...], exec_type: str
) -> bool:
    """Whether a deny rule, not a missing approval, withholds *exec_type* from the package."""
    if ctx is None or not package_keys:
        return False
    return _type_decision(ctx, package_keys, exec_type).trust_state == TRUST_DENIED


def package_type_verdicts(
    ctx: ExecTrustContext,
    package_keys: tuple[str, ...],
    install_path: Path | None,
    exec_types: tuple[str, ...] | list[str],
    *,
    transitive: bool = False,
    registry_resolver: Callable[[Any], dict[str, Any] | None] | None = None,
    native: bool = False,
) -> dict[str, TypeVerdict]:
    """Per-type verdicts for one package: the single reader for every status view.

    ``apm policy explain``, ``apm approve --list/--pending``, ``apm doctor``,
    the exec gate's lockfile ``exec_status`` and native plugin status all read
    this, so a pinned approval reads the same everywhere it is shown.

    A whole-plugin pin decides every type at once. For a *native* plugin,
    per-file item pins never admit: the harness loads the whole root.
    """
    from .executables import LAYER_PROJECT_ALLOW, LAYER_USER_ALLOW

    if native and install_path is not None:
        verdict = admit_native_plugin(ctx, package_keys, install_path, transitive=transitive)
        pinned_plugin = plugin_pin(ctx, package_keys) is not None
        state = (
            TRUST_DEPLOYED
            if verdict.allowed
            else (TRUST_DENIED if verdict.reason == REASON_DENIED else TRUST_GATED)
        )
        return {
            exec_type: TypeVerdict(
                verdict.allowed,
                state,
                verdict.layer,
                pinned=pinned_plugin,
                content_blind=verdict.allowed
                and not pinned_plugin
                and verdict.layer in (LAYER_PROJECT_ALLOW, LAYER_USER_ALLOW),
            )
            for exec_type in exec_types
        }
    pinned = (
        pinned_exec_status(
            ctx,
            package_keys,
            install_path,
            exec_types,
            transitive=transitive,
            registry_resolver=registry_resolver,
        )
        if install_path is not None and not native
        else {}
    )
    result: dict[str, TypeVerdict] = {}
    for exec_type in exec_types:
        decision = _type_decision(ctx, package_keys, exec_type)
        is_pinned = has_item_pins(ctx, package_keys, exec_type) and not native
        if decision.trust_state == TRUST_DENIED:
            shadowed = decision.shadowed_layers + ((LAYER_PROJECT_ALLOW,) if is_pinned else ())
            result[exec_type] = TypeVerdict(
                False,
                TRUST_DENIED,
                decision.deciding_layer,
                pinned=is_pinned,
                shadowed=tuple(dict.fromkeys(shadowed)),
            )
        elif exec_type in pinned:
            state = pinned[exec_type]
            result[exec_type] = TypeVerdict(
                state == "deployed", state, LAYER_PROJECT_ALLOW, pinned=True
            )
        elif (
            exec_type == EXEC_TYPE_MCP
            and decision.allowed
            and install_path is not None
            and not native
            and (
                parked := _parked_mcp_state(
                    ctx, package_keys, install_path, transitive, registry_resolver
                )
            )
        ):
            # A type-level grant never satisfies transitive trust (D2):
            # report what install will park.
            result[exec_type] = TypeVerdict(False, parked, LAYER_DEFAULT_DENY)
        else:
            result[exec_type] = TypeVerdict(
                decision.allowed,
                decision.trust_state,
                decision.deciding_layer,
                content_blind=(
                    decision.allowed
                    and exec_type in ITEM_EXEC_TYPES
                    and decision.deciding_layer in (LAYER_PROJECT_ALLOW, LAYER_USER_ALLOW)
                ),
                shadowed=decision.shadowed_layers,
            )
    return result


def package_exec_status(
    ctx: ExecTrustContext | None,
    package_keys: tuple[str, ...],
    install_path: Path | None,
    exec_types: tuple[str, ...] | list[str],
    *,
    native: bool = False,
) -> str | None:
    """Worst-case lockfile ``exec_status`` for a package's declared executables."""
    return package_exec_decision(ctx, package_keys, install_path, exec_types, native=native)[0]


def package_exec_decision(
    ctx: ExecTrustContext | None,
    package_keys: tuple[str, ...],
    install_path: Path | None,
    exec_types: tuple[str, ...] | list[str],
    *,
    native: bool = False,
) -> tuple[str | None, tuple[str, ...]]:
    """Return the worst-case ``exec_status`` and the declared types still allowed.

    The allowed types are what a gated or denied row records as
    ``exec_allowed_types`` (#3991): the worst case alone cannot show a partial
    approval. They are empty when the gate is off or every type agrees.
    """
    from .executables import more_severe_exec_status

    if ctx is None or not exec_types or not ctx.gate_enabled:
        return None, ()
    status: str | None = None
    allowed: set[str] = set()
    verdicts = package_type_verdicts(ctx, package_keys, install_path, exec_types, native=native)
    for exec_type, verdict in verdicts.items():
        status = more_severe_exec_status(status, "deployed" if verdict.allowed else verdict.state)
        if verdict.allowed:
            allowed.add(exec_type)
    partial = allowed and len(allowed) < len(verdicts)
    return status, tuple(sorted(allowed)) if partial else ()


def package_withholding_layers(
    ctx: ExecTrustContext | None,
    package_keys: tuple[str, ...],
    install_path: Path | None,
    exec_types: tuple[str, ...] | list[str],
    *,
    native: bool = False,
) -> tuple[str, ...]:
    """Return the deciding layers that withhold this package's executables now.

    Empty when the gate is off or every declared type is allowed. The layers
    name where the decision came from (org policy, a local deny, or no
    approval), which ``install --frozen`` and ``--dry-run`` report (#4043).
    """
    withheld = package_withheld_verdicts(ctx, package_keys, install_path, exec_types, native=native)
    return tuple(sorted({verdict.layer for verdict in withheld.values()}))


def package_withheld_verdicts(
    ctx: ExecTrustContext | None,
    package_keys: tuple[str, ...],
    install_path: Path | None,
    exec_types: tuple[str, ...] | list[str],
    *,
    native: bool = False,
) -> dict[str, TypeVerdict]:
    """Return the verdict of each declared type the gate withholds now, by type.

    Empty when the gate is off or every declared type is allowed; ``--dry-run``
    names each withheld surface with its verdict (#4454).
    """
    if ctx is None or not exec_types or not ctx.gate_enabled:
        return {}
    verdicts = package_type_verdicts(ctx, package_keys, install_path, exec_types, native=native)
    return {exec_type: verdict for exec_type, verdict in verdicts.items() if not verdict.allowed}


# -------------------------------------------------------------------
# Legacy content-blind grants (warned in 0.37)
# -------------------------------------------------------------------


def content_blind_grants(ctx: ExecTrustContext | None) -> list[tuple[str, str]]:
    """Return ``(package, type)`` pairs approved for any content (``true``)."""
    if ctx is None:
        return []
    found = {
        (_strip_version(package), exec_type)
        for layer in (ctx.project_allow, ctx.user_allow)
        for package, entry in layer.items()
        if isinstance(entry, dict) and not _is_content_bound_key(package)
        for exec_type in ITEM_EXEC_TYPES
        if entry.get(exec_type) is True
    }
    return sorted(found)


_CONTENT_BLIND_WARNED = False


def warn_content_blind_grants_once(ctx: ExecTrustContext | None, logger: Any) -> None:
    """Warn once per run about approvals that are not pinned to content."""
    global _CONTENT_BLIND_WARNED
    grants = content_blind_grants(ctx)
    if _CONTENT_BLIND_WARNED or not grants or logger is None:
        return
    _CONTENT_BLIND_WARNED = True
    shown = ", ".join(f"{package} ({exec_type})" for package, exec_type in grants[:3])
    more = f" and {len(grants) - 3} more" if len(grants) > 3 else ""
    noun = "approval is" if len(grants) == 1 else "approvals are"
    logger.warning(
        f"{len(grants)} executable {noun} not pinned to content: {shown}{more}. "
        "Upstream changes deploy without review.\n"
        "    Review: apm approve --list  |  Pin: apm approve <package>",
        symbol="warning",
    )


# -------------------------------------------------------------------
# Recording approvals (apm approve and the install prompt)
# -------------------------------------------------------------------

# The first APM release that reads pinned approvals; older clients fail closed.
PINNED_GRANTS_MIN_APM = "0.37"


def has_any_pins(allow: dict[str, Any]) -> bool:
    """Whether a stored grant map already holds a pinned item map."""
    return any(
        isinstance(entry, dict)
        and any(isinstance(value, dict) or key == PLUGIN_GRANT_KEY for key, value in entry.items())
        for entry in allow.values()
    )


# Env keys whose literal value changes what code loads; always shown in full.
_CODE_LOADING_ENV = frozenset(
    {
        "NODE_OPTIONS",
        "NODE_PATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "BASH_ENV",
        "ENV",
        "PERL5OPT",
        "PERL5LIB",
        "RUBYOPT",
        "RUBYLIB",
    }
)


@dataclass(frozen=True)
class PinnedItem:
    """One executable item ``apm approve`` would pin, with what it runs.

    ``fingerprint`` is ``None`` when the item cannot be pinned; ``error`` then
    says why (for example, the registry is unreachable).
    """

    exec_type: str
    item: str
    fingerprint: str | None
    summary: str
    error: str | None = None


def _ascii(text: str) -> str:
    from apm_cli.utils.diagnostics import printable_ascii_text

    return printable_ascii_text(" ".join(str(text).split()))


def _env_summary(env: Any) -> str:
    if not isinstance(env, dict) or not env:
        return ""
    parts = []
    for key in sorted(env, key=str):
        value = env[key]
        text = str(value)
        if str(key) in _CODE_LOADING_ENV or str(key).startswith("DYLD_") or text.startswith("$"):
            parts.append(f"{key}={text}")
        else:
            parts.append(str(key))
    return "env: " + ", ".join(parts)


def mcp_summary(dependency: Any, registry_entry: dict[str, Any] | None = None) -> str:
    """One-line description of what an MCP server runs (values hidden)."""
    transport = dependency.transport or ("stdio" if dependency.command else "http")
    if dependency.url:
        target = str(dependency.url)
    elif dependency.command:
        args = dependency.args if isinstance(dependency.args, list) else []
        target = " ".join([str(dependency.command), *(str(a) for a in args)])
    elif registry_entry is not None:
        version = registry_entry.get("version")
        target = f"registry {registry_entry.get('name', dependency.name)}" + (
            f"@{version}" if version else ""
        )
    else:
        target = f"registry {dependency.name}"
    extras = [_env_summary(dependency.env)]
    if isinstance(dependency.headers, dict) and dependency.headers:
        extras.append("headers: " + ", ".join(sorted(str(k) for k in dependency.headers)))
    detail = "; ".join(_ascii(part) for part in extras if part)
    return f"{_ascii(transport)}  {_ascii(target)}" + (f"  ({detail})" if detail else "")


def _hook_summary(hook_file: Path) -> str:
    import logging

    from apm_cli.hook_contract import walk_hook_commands
    from apm_cli.integration.hook_source_selection import _parse_hook_json

    data = _parse_hook_json(hook_file, logger=logging.getLogger(__name__)) or {}
    commands = list(dict.fromkeys(d.command for d in walk_hook_commands(data) if d.command))
    if not commands:
        return "no commands"
    first = _ascii(commands[0])
    if len(first) > 72:
        first = first[:69] + "..."
    more = f" (+{len(commands) - 1} more)" if len(commands) > 1 else ""
    return first + more


def package_pins(
    install_path: Path,
    *,
    transitive: bool,
    registry_resolver: Callable[[Any], dict[str, Any] | None] | None = None,
    native: bool = False,
) -> list[PinnedItem]:
    """Return every hook file and gated MCP server a package brings, pinned.

    Self-defined MCP servers are always pinned. A registry-resolved server
    with an overlay or its own registry is pinned only for a transitive
    package (a direct dependency's is trusted as today), and pinning it
    resolves the registry entry, so it fails offline rather than pinning
    something unverified.
    """
    from apm_cli.integration.hook_integrator import HookIntegrator

    root = install_path.resolve()
    if native:
        # The plugin pin admits the native install; item pins cover a split
        # install of the same package (best effort: an unpinnable item parks).
        split = package_pins(root, transitive=transitive, registry_resolver=registry_resolver)
        return [native_plugin_pin(root), *(pin for pin in split if pin.fingerprint is not None)]
    pins: list[PinnedItem] = []
    for hook_file in HookIntegrator().find_hook_files(root):
        try:
            fingerprint = hook_file_fingerprint(root, hook_file)
            error = None
        except (UnfingerprintableError, OSError, ValueError) as exc:
            fingerprint, error = None, str(exc)
        pins.append(
            PinnedItem(
                EXEC_TYPE_HOOKS,
                hook_item_key(root, hook_file),
                fingerprint,
                _hook_summary(hook_file),
                error,
            )
        )

    resolver = registry_resolver or resolve_registry_entry
    for dependency in package_mcp_declarations(root):
        if not is_pinnable_mcp(dependency, transitive=transitive):
            continue
        needs_registry = not dependency.is_self_defined
        registry_entry = None
        if needs_registry:
            registry_entry = resolver(dependency)
            if registry_entry is None:
                pins.append(
                    PinnedItem(
                        EXEC_TYPE_MCP,
                        dependency.name,
                        None,
                        mcp_summary(dependency),
                        "needs registry access to pin; retry when the MCP registry is reachable",
                    )
                )
                continue
        try:
            fingerprint = mcp_definition_fingerprint(dependency, registry_entry=registry_entry)
            error = None
        except UnfingerprintableError as exc:
            fingerprint, error = None, str(exc)
        pins.append(
            PinnedItem(
                EXEC_TYPE_MCP,
                dependency.name,
                fingerprint,
                mcp_summary(dependency, registry_entry),
                error,
            )
        )
    return pins


def native_plugin_pin(plugin_root: Path) -> PinnedItem:
    """The single whole-plugin item ``apm approve`` records for a native plugin."""
    from apm_cli.utils.content_hash import native_plugin_files

    types = native_plugin_exec_types(plugin_root)
    try:
        files = len(native_plugin_files(plugin_root))
    except (OSError, ValueError):
        files = 0
    summary = f"whole plugin, {files} files" + (f" ({', '.join(types)})" if types else "")
    try:
        return PinnedItem(
            PLUGIN_GRANT_KEY, PLUGIN_ITEM, native_plugin_fingerprint(plugin_root), summary
        )
    except (OSError, ValueError) as exc:
        return PinnedItem(PLUGIN_GRANT_KEY, PLUGIN_ITEM, None, summary, str(exc))


def approval_scan_dir(source_dir: Path, installed_dir: Path) -> Path:
    """Return the directory ``apm approve`` scans for a local dependency.

    A local dependency is the consumer's own source, so approve reads it
    there. A native plugin has no ``apm.yml`` at its source; install gates the
    normalized copy whose synthesized manifest carries the plugin's MCP
    servers, so approve reads that same copy (#4154).
    """
    from apm_cli.core.scope import manifest_file

    if not manifest_file(source_dir).is_file() and manifest_file(installed_dir).is_file():
        return installed_dir
    return source_dir


def installed_content_matches_lock(locked_dependency: Any, package_dir: Path) -> bool:
    """Return whether installed bytes still match the locked content hash.

    ``apm approve`` hashes what is in ``apm_modules``; this refuses to pin a
    package whose files were edited after install. Local path dependencies
    are the consumer's own files, and rows without a hash cannot be checked.
    """
    expected = getattr(locked_dependency, "content_hash", None)
    if locked_dependency is None or not expected or locked_dependency.is_local_source:
        return True
    from apm_cli.deps.carried_license import carried_license_paths
    from apm_cli.utils.content_hash import verify_package_hash

    excluded = carried_license_paths(tuple(locked_dependency.carried_licenses or ()))
    try:
        return verify_package_hash(
            package_dir,
            expected,
            exclude=excluded,
            framed=getattr(locked_dependency, "content_hash_framed", True),
        )
    except (OSError, ValueError):
        return False


def record_pins(
    allow: dict[str, dict[str, Any]],
    identity: str,
    exec_types: list[str] | tuple[str, ...],
    pins: list[PinnedItem],
) -> list[str]:
    """Write one package's approval: pinned hooks/mcp, ``true`` for other types.

    Every stored spelling of the package (for example ``owner/repo#v1``)
    folds into the version-blind *identity* key. Returns the types whose
    previous content-blind ``true`` grant is now narrowed to pinned items.
    """
    previous: dict[str, Any] = {}
    for key in [k for k in allow if _strip_version(k) == identity and not _is_content_bound_key(k)]:
        entry = allow.pop(key)
        if isinstance(entry, dict):
            previous.update(entry)
    entry: dict[str, Any] = dict(previous)
    narrowed: list[str] = []
    plugin = next((pin for pin in pins if pin.exec_type == PLUGIN_GRANT_KEY), None)
    entry.pop(PLUGIN_GRANT_KEY, None)
    if plugin is not None and plugin.fingerprint is not None:
        # One pin over the whole plugin admits its native install.
        entry[PLUGIN_GRANT_KEY] = plugin.fingerprint
    for exec_type in exec_types:
        if exec_type in ITEM_EXEC_TYPES:
            items = {
                pin.item: pin.fingerprint
                for pin in pins
                if pin.exec_type == exec_type and pin.fingerprint is not None
            }
            if previous.get(exec_type) is True:
                narrowed.append(exec_type)
            entry[exec_type] = dict(sorted(items.items()))
        else:
            entry[exec_type] = True
    allow[identity] = entry
    return narrowed
