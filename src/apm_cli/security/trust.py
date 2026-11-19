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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .executables import (
    EXEC_TYPE_HOOKS,
    EXEC_TYPE_MCP,
    ITEM_EXEC_TYPES,
    LAYER_DEFAULT_DENY,
    LAYER_GATE_DISABLED,
    TRUST_DENIED,
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
)

# Park reasons recorded as lockfile evidence (``mcp_omissions[*].reason``).
REASON_DENIED = "denied"
REASON_DEFINITION_CHANGED = "definition-changed"
REASON_UNTRUSTED_TRANSITIVE = "untrusted-transitive"
REASON_UNAPPROVED = "unapproved-executables"

LAYER_PROJECT_ITEM = "project-allow"
LAYER_USER_ITEM = "user-allow"
LAYER_ONE_OFF = "one-off"
LAYER_NOT_GATED = "not-gated"


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


@dataclass(frozen=True)
class Admission:
    """The admission verdict for one :class:`TrustSubject`."""

    allowed: bool
    reason: str | None
    layer: str
    one_off: bool = False


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
                                                          (a one-off run overrides)
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
    if not needs_trust and not (subject.exec_gated and gate_on):
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
        if not (one_off and needs_trust):
            return Admission(False, REASON_DEFINITION_CHANGED, layer)
        # The pin is the project's package-level consent; the one-off run
        # accepts the changed definition for this run only.
        return Admission(True, None, LAYER_ONE_OFF, one_off=True)

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
    ) -> None:
        self._trust = trust
        self._resolved = not callable(trust)
        self._ctx = None if callable(trust) else trust
        self.one_off = one_off
        self.permissive = permissive
        self._registry_resolver = registry_resolver or resolve_registry_entry

    @classmethod
    def admit_all(cls) -> McpAdmission:
        """Admission for paths that never deploy a new server (uninstall)."""
        return cls(None, one_off=True, permissive=True)

    @classmethod
    def for_project(
        cls,
        project_root: Path,
        *,
        one_off: bool = False,
        no_policy: bool = False,
        include_user: bool = True,
        cache_only_policy: bool = False,
        logger: Any = None,
        trust_context: ExecTrustContext | None = None,
    ) -> McpAdmission:
        """Resolve org, project and (optionally) user layers for *project_root*."""
        if trust_context is not None:
            return cls(trust_context, one_off=one_off)

        def resolve() -> ExecTrustContext:
            from .executables import exec_trust_context_for_project

            policy = None
            if not no_policy:
                from apm_cli.policy.discovery import discover_policy_with_chain

                kwargs = {"cache_only": True} if cache_only_policy else {}
                try:
                    policy = getattr(
                        discover_policy_with_chain(project_root, **kwargs), "policy", None
                    )
                except Exception:
                    policy = None
            return exec_trust_context_for_project(
                project_root, policy=policy, logger=logger, include_user=include_user
            )

        return cls(resolve, one_off=one_off)

    @property
    def context(self) -> ExecTrustContext | None:
        if not self._resolved:
            self._resolved = True
            self._ctx = self._trust()  # type: ignore[operator]
        return self._ctx

    def subject(self, locked_dependency: Any, declaration: Any, package_keys: tuple[str, ...]):
        """Build the subject for one dependency-declared MCP server."""
        needs_trust = bool(declaration.requires_transitive_trust) and locked_dependency.depth > 1
        exec_gated = bool(declaration.is_self_defined)
        fingerprint: str | None = None
        if needs_trust or exec_gated:
            fingerprint = self._fingerprint(declaration, package_keys, needs_trust)
        return TrustSubject(
            package_keys=package_keys,
            exec_type=EXEC_TYPE_MCP,
            item=declaration.name,
            fingerprint=fingerprint,
            needs_transitive_trust=needs_trust,
            exec_gated=exec_gated,
        )

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
        if not subject.needs_transitive_trust and not subject.exec_gated:
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

    def __call__(self, hook_file: Path) -> bool:
        verdict = admit(self._ctx, hook_subject(self._keys, self._path, hook_file))
        if not verdict.allowed:
            self.parked.append((hook_item_key(self._path, hook_file), str(verdict.reason)))
        return verdict.allowed


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


def parked_hook_message(package: str, item: str, reason: str) -> str:
    """Return the warning for one pinned hook file the trust owner parked."""
    if reason == "stray":
        return (
            f"Parked hooks of {package}: '{item}' is not a hook file or a script one "
            "references, and the plugin loads it live\n"
            f"    Review: apm approve --pending  |  Re-approve: apm approve {package}"
        )
    if reason == REASON_DEFINITION_CHANGED:
        why = "definition changed since approval"
    elif reason == REASON_DENIED:
        why = "denied by policy"
    else:
        why = "not approved"
    return (
        f"Parked hook file '{item}' ({package}): {why}\n"
        f"    Review: apm approve --pending  |  Re-approve: apm approve {package}"
    )


@dataclass(frozen=True)
class NativeHooksAdmission:
    """Verdict for a live-loaded plugin's hooks.

    ``parked`` lists hook files whose pin does not admit them; ``stray`` lists
    files under the plugin's ``hooks/`` directory that no hook file references.
    """

    allowed: bool
    parked: tuple[tuple[str, str], ...] = ()
    stray: tuple[str, ...] = ()


def native_hooks_admission(
    allow_executables: Any,
    package_keys: tuple[str, ...],
    install_path: Path,
) -> NativeHooksAdmission | None:
    """Admit a natively registered plugin's hooks file by file.

    A native plugin is loaded live from its install directory, so it is
    registered only when EVERY hook file there is admitted by its own pin and
    every file under the plugin's root ``hooks/`` directory is a hook file or
    a script a hook file references. ``None`` means the package has no pinned
    hook items and the type-level decision applies.
    """
    import logging

    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.integration.hook_source_selection import (
        _parse_hook_json,
        _referenced_hook_source_files,
    )

    ctx = getattr(allow_executables, "trust_context", None)
    if ctx is None or not has_item_pins(ctx, package_keys, EXEC_TYPE_HOOKS):
        return None
    root = install_path.resolve()
    parked: list[tuple[str, str]] = []
    known: set[Path] = set()
    for hook_file in HookIntegrator().find_hook_files(root):
        known.add(hook_file.resolve())
        data = _parse_hook_json(hook_file, logger=logging.getLogger(__name__)) or {}
        known |= {
            path.resolve() for path in _referenced_hook_source_files(data, root, hook_file.parent)
        }
        verdict = admit(ctx, hook_subject(package_keys, root, hook_file))
        if not verdict.allowed:
            parked.append((hook_item_key(root, hook_file), str(verdict.reason)))
    stray: list[str] = []
    hooks_dir = root / "hooks"
    if hooks_dir.is_dir():
        stray = [
            path.relative_to(root).as_posix()
            for path in sorted(hooks_dir.rglob("*"))
            if (path.is_file() or path.is_symlink()) and path.resolve() not in known
        ]
    return NativeHooksAdmission(not parked and not stray, tuple(parked), tuple(stray))


def hooks_package_admissible(allow_executables: Any, package_keys: tuple[str, ...]) -> bool:
    """Return whether pinned hook items may admit some hook file of the package."""
    ctx = getattr(allow_executables, "trust_context", None)
    if ctx is None or not has_item_pins(ctx, package_keys, EXEC_TYPE_HOOKS):
        return False
    return not any(
        resolve_exec_decision(ctx, key, EXEC_TYPE_HOOKS).trust_state == TRUST_DENIED
        for key in package_keys
    )


def _package_mcp_subjects(package_keys: tuple[str, ...], install_path: Path) -> list[TrustSubject]:
    """Return exec-gated (self-defined) MCP subjects a package manifest declares."""
    from apm_cli.models.dependency.mcp import MCPDependency
    from apm_cli.utils.yaml_io import load_yaml

    manifest = install_path / "apm.yml"
    try:
        data = load_yaml(manifest) if manifest.is_file() and not manifest.is_symlink() else {}
    except Exception:
        data = {}
    deps = data.get("dependencies") if isinstance(data, dict) else None
    entries = deps.get("mcp") if isinstance(deps, dict) else None
    subjects = []
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, dict) or entry.get("registry") is not False:
            continue
        try:
            dependency = MCPDependency.from_dict(entry)
            fingerprint = mcp_definition_fingerprint(dependency)
        except (ValueError, TypeError):
            continue
        subjects.append(TrustSubject(package_keys, EXEC_TYPE_MCP, dependency.name, fingerprint))
    return subjects


def pinned_exec_status(
    ctx: ExecTrustContext | None,
    package_keys: tuple[str, ...],
    install_path: Path,
    exec_types: tuple[str, ...] | list[str],
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
            subjects = _package_mcp_subjects(package_keys, root)
        verdicts = [admit(ctx, subject) for subject in subjects]
        if any(verdict.reason == REASON_DENIED for verdict in verdicts):
            result[exec_type] = TRUST_DENIED
        elif all(verdict.allowed for verdict in verdicts):
            result[exec_type] = TRUST_DEPLOYED
        else:
            result[exec_type] = TRUST_GATED
    return result


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
) -> list[PinnedItem]:
    """Return every hook file and gated MCP server a package brings, pinned.

    Self-defined MCP servers are always pinned. A registry-resolved server
    with an overlay or its own registry is pinned only for a transitive
    package (a direct dependency's is trusted as today), and pinning it
    resolves the registry entry, so it fails offline rather than pinning
    something unverified.
    """
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.models.dependency.mcp import MCPDependency
    from apm_cli.utils.yaml_io import load_yaml

    root = install_path.resolve()
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

    manifest = root / "apm.yml"
    try:
        data = load_yaml(manifest) if manifest.is_file() and not manifest.is_symlink() else {}
    except Exception:
        data = {}
    deps = data.get("dependencies") if isinstance(data, dict) else None
    entries = deps.get("mcp") if isinstance(deps, dict) else None
    resolver = registry_resolver or resolve_registry_entry
    for entry in entries if isinstance(entries, list) else ():
        try:
            dependency = (
                MCPDependency.from_string(entry)
                if isinstance(entry, str)
                else MCPDependency.from_dict(entry)
            )
        except (ValueError, TypeError):
            continue
        needs_registry = not dependency.is_self_defined
        if needs_registry and not (transitive and dependency.requires_transitive_trust):
            continue
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
    from apm_cli.utils.content_hash import compute_package_hash

    excluded = carried_license_paths(tuple(locked_dependency.carried_licenses or ()))
    try:
        actual = compute_package_hash(package_dir, exclude=excluded)
    except (OSError, ValueError):
        return False
    return actual == expected


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
