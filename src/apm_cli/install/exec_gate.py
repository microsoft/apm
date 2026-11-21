"""Executable approval gate helpers for the install pipeline.

Extracted from ``services.py`` to stay within the LOC budget.
These helpers are used by ``integrate_package_primitives`` to enforce
the npm v12-style ``allowExecutables`` default-deny policy.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from apm_cli.core.remediation import (
    BIN_EXECUTABLES_NOT_RETRUSTED,
    BIN_EXECUTABLES_UNTRUSTED,
)
from apm_cli.install.plan_admission import (  # noqa: F401 -- re-exported for callers
    _declared_exec_types,
    _is_transitive,
    _recorded_deployed_types,
    bin_deploy_denial,
    hook_file_admission,
    native_plugin_verdict,
    plugin_admission,
    resolve_bin_skip,
)

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def check_executable_approval(
    package_name: str,
    package_info: Any,
    allow_executables: builtins.dict[str, builtins.dict[str, bool]] | None,
    *,
    ctx: InstallContext | None = None,
) -> tuple[bool, bool, bool, bool, bool]:
    """Return hook, bin, MCP, canvas, and LSP approval for a package.

    An adapter over the trust owners: first-party content (``_local``) and a
    map that explicitly authorizes every executable (gate disabled, or audit
    replay of already-authorized content) approve everything. Any other map
    -- including ``None`` -- approves only what the executable-trust owner
    grants and item admission admits.

    When *ctx* is provided and a package is blocked, the declaration is
    recorded on ``ctx.blocked_executables`` for the post-loop prompt.
    """
    from apm_cli.security.executables import (
        EXEC_TYPE_BIN,
        EXEC_TYPE_CANVAS,
        EXEC_TYPE_HOOKS,
        EXEC_TYPE_LSP,
        EXEC_TYPE_MCP,
        FIRST_PARTY_PACKAGE,
        exec_map_authorizes_all,
        package_type_approvals,
    )

    if package_name == FIRST_PARTY_PACKAGE or exec_map_authorizes_all(allow_executables):
        return True, True, True, True, True

    # Executable authorization is bound to the resolver-owned dependency
    # identity. Package manifest metadata is display-only and must not become
    # an alternate security principal.
    pkg_key = resolve_package_key(package_info, package_name)
    candidate_keys = [pkg_key]

    name_blind = pkg_key.split("#", 1)[0]
    if name_blind not in candidate_keys:
        candidate_keys.append(name_blind)

    from apm_cli.security.trust import hooks_package_admissible

    # A NATIVE install (the harness loads the plugin intact) is decided once,
    # for every executable type, by the trust owner; a split install of the
    # same package uses the per-type and per-item path below.
    if native_install(package_info):
        return _native_approval(
            package_name, package_info, allow_executables, tuple(candidate_keys), pkg_key, ctx
        )

    approved = package_type_approvals(allow_executables, candidate_keys)
    # Pinned hook items admit per file at the source plan (hook_file_admission);
    # a package none of whose hook files matches its pin deploys no hooks.
    approved[EXEC_TYPE_HOOKS] = approved[EXEC_TYPE_HOOKS] or hooks_package_admissible(
        allow_executables, tuple(candidate_keys), package_info
    )
    hooks_ok = approved[EXEC_TYPE_HOOKS]
    bin_ok = approved[EXEC_TYPE_BIN]
    mcp_ok = approved[EXEC_TYPE_MCP]
    canvas_ok = approved[EXEC_TYPE_CANVAS]
    lsp_ok = approved[EXEC_TYPE_LSP]
    # Track blocked packages for the post-loop approval prompt, and record the
    # lockfile exec_status for the audit (Gap B) from the same scan.
    blocked = not all(approved.values())
    needs_status = ctx is not None and getattr(ctx, "exec_trust_ctx", None) is not None
    if ctx is not None and (blocked or needs_status):
        from dataclasses import replace

        from apm_cli.security.executables import scan_package_executables

        _install = Path(package_info.install_path)
        _decl = replace(
            scan_package_executables(
                _install,
                package_name,
                "",
                approval_identity=pkg_key,
            ),
            install_path=str(_install),
        )
        from apm_cli.security.trust import pinned_exec_status

        pinned_status = pinned_exec_status(
            getattr(allow_executables, "trust_context", None),
            tuple(candidate_keys),
            _install,
            _decl.exec_types,
        )
        unapproved = [t for t in _decl.exec_types if t not in pinned_status and not approved[t]]
        from apm_cli.security.executables import (
            TRUST_DENIED,
            TRUST_GATED,
            ExecTrustContext,
            denied_exec_types,
        )

        # A pinned type the item owner denies is reported as denied, like a
        # type-level deny: approving cannot lift it.
        denied = frozenset(t for t, status in pinned_status.items() if status == TRUST_DENIED)
        if unapproved and isinstance(ctx.exec_trust_ctx, ExecTrustContext):
            denied |= denied_exec_types(ctx.exec_trust_ctx, candidate_keys, unapproved)
        if denied:
            ctx.package_denied_exec_types[package_name] = denied
        # Only a declared type that stays unapproved and is not hard-denied
        # (approving cannot lift a deny), or a pinned item awaiting
        # re-approval, parks the package for approval.
        if any(t not in denied for t in unapproved) or any(
            status == TRUST_GATED for status in pinned_status.values()
        ):
            ctx.blocked_executables.append(_decl)
        if _decl.has_executables and needs_status:
            from apm_cli.security.trust import package_exec_decision

            status, allowed_types = package_exec_decision(
                ctx.exec_trust_ctx,
                tuple(candidate_keys),
                _install,
                _decl.exec_types,
                native=False,
            )
            if status is not None:
                ctx.package_exec_status[package_name] = status
            if allowed_types:
                ctx.package_exec_allowed_types[package_name] = allowed_types

    return hooks_ok, bin_ok, mcp_ok, canvas_ok, lsp_ok


def _native_approval(
    package_name: str,
    package_info: Any,
    allow_executables: Any,
    candidate_keys: tuple[str, ...],
    pkg_key: str,
    ctx: InstallContext | None,
) -> tuple[bool, bool, bool, bool, bool]:
    """One trust-owner verdict for a native install's hooks, bin, MCP, canvas and LSP."""
    from dataclasses import replace

    from apm_cli.security.executables import (
        TRUST_DENIED,
        TRUST_DEPLOYED,
        TRUST_GATED,
        closed_exec_map,
        scan_package_executables,
    )
    from apm_cli.security.trust import REASON_DENIED, admit_native_plugin, native_declaration

    install = Path(package_info.install_path)
    verdict = admit_native_plugin(
        closed_exec_map(allow_executables),
        candidate_keys,
        install,
        transitive=_is_transitive(package_info, ctx),
        one_off=bool(getattr(ctx, "trust_transitive_mcp", False)),
    )
    if ctx is not None:
        if not verdict.allowed:
            ctx.blocked_executables.append(
                replace(
                    native_declaration(
                        scan_package_executables(
                            install, package_name, "", approval_identity=pkg_key
                        ),
                        install,
                    ),
                    install_path=str(install),
                )
            )
        if getattr(ctx, "exec_trust_ctx", None) is not None:
            ctx.package_exec_status[package_name] = (
                TRUST_DEPLOYED
                if verdict.allowed
                else TRUST_DENIED
                if verdict.reason == REASON_DENIED
                else TRUST_GATED
            )
    return (verdict.allowed,) * 5


def native_install(package_info: Any) -> bool:
    """Whether install loads this package intact as a native plugin.

    The same predicates the native paths use: an admitted Copilot Agent
    Plugin, or a Codex compatibility bundle (which never splits).
    """
    from apm_cli.codex_plugins.manifest import CodexCompatibilityPlugin
    from apm_cli.copilot_plugins.capability import admits_native_plugin

    package = getattr(package_info, "package", None)
    if isinstance(getattr(package, "codex_plugin", None), CodexCompatibilityPlugin):
        return True
    return admits_native_plugin(package_info)


def _candidate_keys(pkg_key: str) -> list[str]:
    """Return the approval key and its version-blind form."""
    name_blind = pkg_key.split("#", 1)[0]
    return [pkg_key] if name_blind == pkg_key else [pkg_key, name_blind]


class ExecPolicyUnavailable(Exception):
    """Org policy could not be read, so the current trust decision is unknown."""


def current_exec_decision(
    trust_ctx_provider: Callable[[], Any], package_info: Any, package_name: str
) -> tuple[str | None, tuple[str, ...]]:
    """Return ``(exec_status, allowed_types)`` the install gate would decide now.

    Scans the materialized package and resolves its declared executable
    types through the trust owner (:func:`package_type_verdicts`), as
    :func:`check_executable_approval` does during install; a native
    install gates on the native union of types. ``exec_status`` is
    ``None`` when the package declares no executables or the gate is
    disabled. ``allowed_types`` names the types still allowed when others
    are not, as the lock records them (``exec_allowed_types``, #3991).
    *trust_ctx_provider* is called only for a package that declares some.
    """
    from apm_cli.security.trust import package_exec_decision

    pkg_key, install, native, exec_types = _declared_exec_types(package_info, package_name)
    if not exec_types:
        return None, ()
    return package_exec_decision(
        trust_ctx_provider(), tuple(_candidate_keys(pkg_key)), install, exec_types, native=native
    )


_ORG_DENY_LAYERS = frozenset({"org-deny-all", "org-deny"})
_LOCAL_DENY_LAYERS = frozenset({"user-deny", "project-deny"})


@dataclass(frozen=True)
class WithheldExecutables:
    """Why the gate withholds one locked package's executables, and which ones.

    ``types`` pairs each withheld declared type with the gate's verdict state
    (``denied`` or ``gated_pending_approval``).
    """

    source: str
    types: tuple[tuple[str, str], ...]


def withheld_executables(
    lockfile: Any,
    modules_dir: Path,
    manifest_path: Path,
    *,
    policy_provider: Callable[[], Any],
    targets: list[Any] | None = None,
    fallback_allow_executables: builtins.dict[str, builtins.dict[str, bool]] | None = None,
) -> builtins.dict[str, WithheldExecutables]:
    """Say why the gate now withholds each locked package's executables (#4043).

    Keys are dependency keys; each value names the deciding source (``org
    policy denies <pkg>``, ``<pkg> is denied locally`` or ``<pkg> is no
    longer approved locally``) and the withheld types with their verdicts
    (#4454). Only the types the lock records as deployed are judged, so a
    partially admitted package counts when an approved type is revoked.
    Packages the gate still allows are absent, as are packages whose decision
    cannot be read now (an unreadable package, or *policy_provider* raising
    :class:`ExecPolicyUnavailable`). ``install --frozen`` names retired files
    with it and ``--dry-run`` previews them.
    """
    from apm_cli.copilot_plugins.capability import native_registration_scope
    from apm_cli.install.drift import _build_package_info
    from apm_cli.security.executables import TRUST_ABSENT, exec_trust_context_for_project
    from apm_cli.security.trust import package_withheld_verdicts

    trust: list[Any] = []

    def _trust_ctx() -> Any:
        if not trust:
            trust.append(
                exec_trust_context_for_project(
                    manifest_path,
                    policy=policy_provider(),
                    fallback_allow_executables=fallback_allow_executables,
                    migrate_user_legacy=False,
                )
            )
        return trust[0]

    withheld: builtins.dict[str, WithheldExecutables] = {}
    with native_registration_scope(targets):
        for key, dep in sorted((getattr(lockfile, "dependencies", None) or {}).items()):
            if key == "." or dep.exec_status == TRUST_ABSENT:
                continue
            install_path = dep.to_dependency_ref().get_install_path(modules_dir)
            if not install_path.is_dir():
                continue
            try:
                package_key, install, native, declared = _declared_exec_types(
                    _build_package_info(dep, install_path), key
                )
                exec_types = _recorded_deployed_types(dep, declared)
                verdicts = package_withheld_verdicts(
                    _trust_ctx() if exec_types else None,
                    tuple(_candidate_keys(package_key)),
                    install,
                    exec_types,
                    native=native,
                )
            except (ExecPolicyUnavailable, OSError, ValueError):
                continue
            layers = {verdict.layer for verdict in verdicts.values()}
            if layers & _ORG_DENY_LAYERS:
                source = f"org policy denies {key}"
            elif layers & _LOCAL_DENY_LAYERS:
                source = f"{key} is denied locally"
            elif layers:
                source = f"{key} is no longer approved locally"
            else:
                continue
            types = tuple((exec_type, verdicts[exec_type].state) for exec_type in sorted(verdicts))
            withheld[key] = WithheldExecutables(source, types)
    return withheld


def recorded_exec_status_is_stale(
    recorded: str | None,
    current: str | None,
    *,
    recorded_allowed: tuple[str, ...] = (),
    current_allowed: tuple[str, ...] = (),
) -> bool:
    """Return whether a lock row's executables decision may no longer hold.

    A row recorded as gated or denied, or one that is now gated or denied,
    must be re-integrated so install deploys or retires its executables.
    Unrecorded and deployed rows are both ungated, so they agree. A partial
    package also compares the types it deployed (``exec_allowed_types``) with
    the types allowed now: its worst-case status hides an approval removed or
    added for one type (#3991). Once install records the new set it converges.
    """
    from apm_cli.security.executables import TRUST_DEPLOYED

    ungated = {None, TRUST_DEPLOYED}
    if recorded in ungated and current in ungated:
        return False
    return recorded != current or tuple(recorded_allowed) != tuple(current_allowed)


def policy_denied_locked_packages(lockfile: Any, policy: Any) -> dict[str, str]:
    """Return lock key -> reason for each locked dependency the org policy now denies.

    The same dependency check the install policy gate applies
    (:func:`~apm_cli.policy.matcher.check_dependency_allowed`).
    """
    from apm_cli.policy.matcher import check_dependency_allowed

    dependency_policy = getattr(policy, "dependencies", None)
    if dependency_policy is None:
        return {}
    denied: dict[str, str] = {}
    for key, dep in (getattr(lockfile, "dependencies", None) or {}).items():
        if key == ".":
            continue
        allowed, reason = check_dependency_allowed(dep.to_dependency_ref(), dependency_policy)
        if not allowed:
            denied[key] = f"org policy: {reason}"
    return denied


class GateRetirements(NamedTuple):
    """What the current gates no longer allow, by surface (see gate_retirements)."""

    denied_packages: dict[str, str]
    parked_servers: dict[str, str]
    hook_pins: dict[str, str]
    # Lock key -> why the hooks it recorded as deployed are no longer allowed.
    hook_packages: dict[str, str]


def gate_retirements(
    lockfile: Any, modules_dir: Path, apm_package: Any, project_root: Path, *, user_scope: bool
) -> GateRetirements | None:
    """What the current gates no longer allow on a path that skips install's gate.

    The one owner of gate monotonicity for ``apm update`` and ``apm uninstall``
    (#4444, #4445, #4446): locked dependencies the org policy denies, recorded
    MCP servers that install's own MCP admission
    (:class:`~apm_cli.security.trust.McpAdmission`) now parks -- a deny, a
    revoked approval or a changed definition -- recorded hook files whose
    pinned approval no longer admits them (:func:`locked_hook_pin_mismatches`,
    the ``--frozen`` and audit owner), and the packages whose recorded hooks
    install's executables gate no longer deploys (:func:`locked_hooks_retired`).
    ``None`` when the org policy blocks or is unresolved: install's policy
    preflight owns that outcome.
    """
    from apm_cli.core.scope import manifest_root, scope_manifest_path
    from apm_cli.integration.mcp_config_view import CurrentMcpConfigView
    from apm_cli.policy.outcome_routing import classify_project_policy
    from apm_cli.security.executables import exec_trust_context_for_project
    from apm_cli.security.trust import McpAdmission

    if lockfile is None:
        return GateRetirements({}, {}, {}, {})
    route = classify_project_policy(manifest_root(project_root, user_scope=user_scope))
    if route.blocks or route.unresolved:
        return None
    denied = policy_denied_locked_packages(lockfile, route.policy)
    # recorded=None, as install selects: folding recorded omissions in first
    # would put each parked server's locked config back and hide the problem.
    view = CurrentMcpConfigView.select(
        apm_package,
        lockfile,
        modules_dir,
        admission=McpAdmission.for_project(project_root, user_scope=user_scope),
        recorded=None,
    )

    trust: builtins.list[Any] = []

    def _trust_ctx() -> Any:
        if not trust:
            # Reuse the policy routed above: a second resolution would report
            # the same policy miss twice (I9).
            trust.append(
                exec_trust_context_for_project(
                    scope_manifest_path(project_root, user_scope=user_scope),
                    policy=route.policy,
                    fallback_allow_executables=getattr(apm_package, "allow_executables", None),
                )
            )
        return trust[0]

    pins = locked_hook_pin_mismatches(lockfile, modules_dir, _trust_ctx)
    hooks = {f"{item.package}/{item.item}": item.describe() for item in pins}
    retired = locked_hooks_retired(
        lockfile, modules_dir, _trust_ctx, pinned={item.package for item in pins}
    )
    return GateRetirements(denied, view.consent_problems(lockfile), hooks, retired)


def locked_hooks_retired(
    lockfile: Any, modules_dir: Path, trust_ctx: Callable[[], Any], *, pinned: set[str]
) -> builtins.dict[str, str]:
    """Return lock key -> reason for each package whose recorded hooks the gate now refuses.

    A row recorded as deploying hooks (ungated, or ``hooks`` among
    ``exec_allowed_types``) is judged with :func:`current_exec_decision`, the
    install gate's own verdict: a deny, a revoked approval or a newly
    enabled gate leaves hooks out of the types it allows. A package named in
    *pinned* (its pinned hook approval no longer admits a file) is retired
    too. A package that cannot be read is left to install, which reports it.
    """
    from apm_cli.install.drift import _build_package_info
    from apm_cli.security.executables import (
        EXEC_TYPE_HOOKS,
        TRUST_ABSENT,
        TRUST_DENIED,
        TRUST_DEPLOYED,
        _strip_version,
    )

    ungated = {None, TRUST_DEPLOYED}
    retired: builtins.dict[str, str] = {}
    for key, dep in (getattr(lockfile, "dependencies", None) or {}).items():
        if key == "." or dep.exec_status == TRUST_ABSENT:
            continue
        recorded_allowed = tuple(dep.exec_allowed_types or ())
        if dep.exec_status not in ungated and EXEC_TYPE_HOOKS not in recorded_allowed:
            continue
        install_path = dep.to_dependency_ref().get_install_path(modules_dir)
        if not install_path.is_dir():
            continue
        try:
            package_info = _build_package_info(dep, install_path)
            status, allowed = current_exec_decision(trust_ctx, package_info, dep.get_unique_key())
        except (ExecPolicyUnavailable, OSError, ValueError):
            continue
        if _strip_version(resolve_package_key(package_info, dep.get_unique_key())) in pinned:
            retired[key] = "hook definition changed since approval"
        elif status not in ungated and EXEC_TYPE_HOOKS not in allowed:
            retired[key] = "denied" if status == TRUST_DENIED else "not approved"
    return retired


@dataclass(frozen=True)
class StaleExecDecision:
    """A locked package whose recorded ``exec_status`` the gate no longer makes."""

    package: str
    recorded: str | None
    current: str | None
    recorded_allowed: tuple[str, ...] = ()
    current_allowed: tuple[str, ...] = ()

    def describe(self) -> str:
        recorded = self.recorded or "ungated"
        current = self.current or "ungated"
        if self.recorded_allowed != self.current_allowed:
            # A partial package: the worst-case status hides a per-type change.
            recorded += f" ({', '.join(self.recorded_allowed) or 'none'} allowed)"
            current += f" ({', '.join(self.current_allowed) or 'none'} allowed)"
        return f"{self.package}: recorded {recorded}, now {current}"


def exec_policy_from_discovery(discover: Callable[[], Any]) -> Any:
    """Return the org policy a discovery call resolved for the gate.

    Raises :class:`ExecPolicyUnavailable` when discovery fails or reports a
    resolution failure (for example no cache and an unreachable source), so
    callers fail toward the install gate's fetch-failure posture instead of
    deciding as if there were no org policy.
    """
    from apm_cli.policy.outcome_routing import classify_discovery_outcome

    try:
        fetch = discover()
    except Exception as exc:
        raise ExecPolicyUnavailable(str(exc)) from exc
    route = classify_discovery_outcome(fetch, fetch_failure_default="warn")
    # A blocking or unresolved outcome is indeterminate, never "no policy".
    if route.blocks or route.unresolved:
        raise ExecPolicyUnavailable(route.outcome)
    return getattr(fetch, "policy", None)


def discovered_exec_policy(deploy_root: Path) -> Any:
    """Return the org policy the gate applies at *deploy_root* (see above)."""
    from apm_cli.policy.discovery import discover_policy_with_chain

    return exec_policy_from_discovery(lambda: discover_policy_with_chain(deploy_root))


def stale_exec_decisions(
    lockfile: Any,
    modules_dir: Path,
    manifest_path: Path,
    *,
    policy_provider: Callable[[], Any],
    targets: list[Any] | None = None,
    fallback_allow_executables: builtins.dict[str, builtins.dict[str, bool]] | None = None,
    first_only: bool = False,
) -> tuple[list[StaleExecDecision], bool]:
    """Return the lock rows whose executables decision no longer holds.

    The second value is true when the answer is indeterminate: an unreadable
    package, or *policy_provider* raising :class:`ExecPolicyUnavailable`.
    ``apm update`` treats that as stale and runs the gate; ``apm install
    --frozen`` and ``apm audit --ci`` report only determinate rows (#4053).
    A row recorded while the gate was off is compared against the effective
    gate, org policy included, so a policy that newly gates executables is
    stale as ``apm install`` would re-gate it (#3990). Packages are read the
    way audit replay reads them, under the *targets*' native registration
    capability. Trust (and *policy_provider*) resolve only when a package
    declares executables.
    """
    from apm_cli.copilot_plugins.capability import native_registration_scope
    from apm_cli.install.drift import _build_package_info
    from apm_cli.security.executables import TRUST_ABSENT, exec_trust_context_for_project

    trust: list[Any] = []

    def _trust_ctx() -> Any:
        if not trust:
            trust.append(
                exec_trust_context_for_project(
                    manifest_path,
                    policy=policy_provider(),
                    fallback_allow_executables=fallback_allow_executables,
                    migrate_user_legacy=False,
                )
            )
        return trust[0]

    rows = [
        dep
        for key, dep in (getattr(lockfile, "dependencies", None) or {}).items()
        if key != "." and dep.exec_status != TRUST_ABSENT
    ]
    stale: list[StaleExecDecision] = []
    with native_registration_scope(targets):
        for dep in rows:
            install_path = dep.to_dependency_ref().get_install_path(modules_dir)
            if not install_path.is_dir():
                continue
            key = dep.get_unique_key()
            try:
                package_info = _build_package_info(dep, install_path)
                status, allowed_types = current_exec_decision(_trust_ctx, package_info, key)
            except ExecPolicyUnavailable:
                return stale, True
            except (OSError, ValueError):
                # An unreadable package: let install, which reports it, decide.
                return stale, True
            if recorded_exec_status_is_stale(
                dep.exec_status,
                status,
                recorded_allowed=dep.exec_allowed_types,
                current_allowed=allowed_types,
            ):
                stale.append(
                    StaleExecDecision(
                        key,
                        dep.exec_status,
                        status,
                        tuple(dep.exec_allowed_types or ()),
                        tuple(allowed_types),
                    )
                )
                if first_only:
                    break
    return stale, False


def lock_exec_decisions_stale(
    lockfile: Any,
    modules_dir: Path,
    manifest_path: Path,
    *,
    policy_provider: Callable[[], Any],
    targets: list[Any] | None = None,
    fallback_allow_executables: builtins.dict[str, builtins.dict[str, bool]] | None = None,
) -> bool:
    """Return whether any lock row's executables decision may no longer hold.

    ``apm update`` short-circuits when no ref moved; this tells it that an
    ``apm approve`` / ``apm deny`` or an org policy change still needs the
    install gate to deploy or retire executables and rewrite the lock (#3990).
    An indeterminate answer fails toward running the gate, which applies the
    policy fetch-failure posture.
    """
    stale, indeterminate = stale_exec_decisions(
        lockfile,
        modules_dir,
        manifest_path,
        policy_provider=policy_provider,
        targets=targets,
        fallback_allow_executables=fallback_allow_executables,
        first_only=True,
    )
    return indeterminate or bool(stale)


def resolve_package_key(package_info: Any, package_name: str) -> str:
    """Build the ``allowExecutables`` lookup key for a package.

    Routes ``dependency_ref`` through the executable-trust owner, then
    falls back to ``name#version`` from the package's own metadata.
    """
    from apm_cli.security.executables import build_approval_key, dependency_approval_key

    dep_ref = getattr(package_info, "dependency_ref", None)
    if dep_ref is not None:
        return dependency_approval_key(dep_ref)

    # Fall back to package metadata
    pkg = getattr(package_info, "package", None)
    if pkg is not None:
        name = getattr(pkg, "name", package_name) or package_name
        version = getattr(pkg, "version", "") or ""
        return build_approval_key(name, version)

    return package_name


def plugin_bin_deployable(
    package_info: Any,
    targets: list[Any],
    *,
    project_root: Path,
    scope: Any,
    policy: Any,
    skip_bin: bool,
) -> bool:
    """Return whether approved marketplace plugin bin files can reach a target."""
    from apm_cli.core.scope import InstallScope
    from apm_cli.models.apm_package import PackageType

    if (
        skip_bin
        or package_info.package_type is not PackageType.MARKETPLACE_PLUGIN
        or scope is not InstallScope.USER
        or not (Path(package_info.install_path) / "bin").is_dir()
        or not any(
            target.name == "claude"
            and target.supports("skills")
            and (target.auto_create or (project_root / target.root_dir).is_dir())
            for target in targets
        )
    ):
        return False
    return bin_deploy_denial(package_info, policy) is None


def executable_skip_reason(
    ctx: InstallContext | None, package_name: str, label: str, exec_type: str, kind: str
) -> str:
    """Return why *kind* of *package_name* was skipped, with the next step.

    The gate records hard-denied types in ``ctx.package_denied_exec_types``;
    a denied type points to ``apm policy explain`` because approving cannot
    lift a deny. Any other skip is approvable.
    """
    from apm_cli.core.scope import is_user_scope
    from apm_cli.security.trust import approve_command, explain_command

    user_scope = is_user_scope(getattr(ctx, "scope", None))
    denied = getattr(ctx, "package_denied_exec_types", None) or {}
    if exec_type in denied.get(package_name, frozenset()):
        explain = explain_command(label, user_scope=user_scope)
        return f"{kind} skipped (denied). Run '{explain}' to see which deny applies."
    from apm_cli.core import remediation
    from apm_cli.security.executables import EXEC_TYPE_BIN, EXEC_TYPE_CANVAS, EXEC_TYPE_HOOKS

    # Only these kinds are skipped as unapproved here; another kind is a caller bug.
    approve = {
        EXEC_TYPE_BIN: remediation.BIN_EXECUTABLES_UNAPPROVED,
        EXEC_TYPE_CANVAS: remediation.CANVAS_SKIPPED_UNAPPROVED,
        EXEC_TYPE_HOOKS: remediation.HOOKS_UNAPPROVED,
    }[exec_type]
    command = approve_command(label, user_scope=user_scope)
    return (
        f"{kind} skipped (not approved in executables.allow). "
        f"{approve.bind(package=label).with_command(command).sentence()}"
    )


def log_bin_status(
    skill_result: Any,
    suffix: str,
    package_name: str,
    package_info: Any,
    log_fn,
    ctx: InstallContext | None = None,
) -> None:
    """Emit integration-tree lines for bin/ deployment or skip reasons."""
    from apm_cli.utils.diagnostics import printable_ascii_text

    package_label = printable_ascii_text(package_name or getattr(package_info, "name", "unknown"))
    if skill_result.bin_deployed > 0:
        log_fn(
            f"  |-- {skill_result.bin_deployed} executable(s) deployed to "
            f"Claude Code's PATH -> {suffix} (invoked without confirmation)"
        )
        log_fn("  |-- run /reload-plugins or restart Claude Code to activate")
    elif skill_result.bin_skipped_reason == "project_scope":
        log_fn(
            "  |-- plugin ships executables; re-run with -g (global) to deploy them to Claude Code"
        )
    elif skill_result.bin_skipped_reason == "no_claude_target":
        log_fn(
            "  |-- plugin ships executables; no active Claude Code skills target to receive them"
        )
    elif skill_result.bin_skipped_reason == "not_approved":
        from apm_cli.security.executables import EXEC_TYPE_BIN

        reason = executable_skip_reason(
            ctx, package_name, package_label, EXEC_TYPE_BIN, "bin/ executables"
        )
        log_fn(f"  |-- {reason}")
    elif skill_result.bin_skipped_reason == "not_trusted":
        log_fn(
            "  |-- bin/ executables skipped (not trusted). "
            f"{BIN_EXECUTABLES_UNTRUSTED.bind(package=package_label).sentence()}"
        )
    elif skill_result.bin_skipped_reason == "not_retrusted_on_uninstall":
        log_fn(
            "  |-- bin/ executables not re-deployed during uninstall cleanup. "
            f"{BIN_EXECUTABLES_NOT_RETRUSTED.bind(package=package_label).sentence()}"
        )


def log_hooks_skip(
    package_name: str,
    package_info: Any,
    logger: Any | None,
    ctx: InstallContext | None = None,
) -> None:
    """Warn about skipped hooks only when the package actually ships them.

    Aligned with :meth:`HookIntegrator.find_hook_files`: checks for
    ``*.json`` in ``.apm/hooks/`` and ``hooks/``. A hard-denied package is
    named as denied; approving it cannot lift the deny.
    """
    install = Path(package_info.install_path)
    if logger is None or not any(
        d.is_dir() and any(d.glob("*.json"))
        for d in (install / ".apm" / "hooks", install / "hooks")
    ):
        return
    from apm_cli.security.executables import EXEC_TYPE_HOOKS

    label = package_name or getattr(package_info, "name", "unknown")
    reason = executable_skip_reason(ctx, package_name, label, EXEC_TYPE_HOOKS, "hooks")
    logger.warning(f"{label}: {reason}", symbol="warning")


class ReplayExecAllowMap:
    """Resolve the install gate's allow-map once, only for gated dependencies.

    A lock entry whose ``exec_status`` is gated or denied had executables
    withheld at install, so audit replay re-derives the per-type decisions
    from the deny-wins trust owner and withholds them too (#3924). Any other
    entry replays ungated unless the trust owner now gates or denies it (an
    ``apm deny`` that no install has reconciled yet), so executables left
    deployed against the current decision surface as drift (#3958).
    The lockfile keeps no per-type evidence, so this is the only re-check
    replay makes.

    *policy_provider* returns the org policy object (or ``None``) the caller
    already fetched, so one audit run consults a single policy.
    """

    def __init__(
        self,
        project_root: Path,
        policy_provider: Callable[[], Any] | None = None,
        *,
        user_scope: bool = False,
    ):
        self._project_root = project_root
        self._policy_provider = policy_provider
        self._user_scope = user_scope
        self._resolved = False
        self._trust_ctx_value: Any = None
        self._map: builtins.dict[str, builtins.dict[str, bool]] = {}

    def _resolve(self) -> None:
        if self._resolved:
            return
        from apm_cli.core.scope import manifest_root, scope_manifest_path
        from apm_cli.security.executables import (
            exec_trust_context_for_project,
            materialize_exec_map,
        )

        if self._policy_provider is not None:
            policy = self._policy_provider()
        else:
            from apm_cli.policy.outcome_routing import resolve_trust_policy

            # The replaying command reports the policy outcome itself.
            policy = resolve_trust_policy(
                manifest_root(self._project_root, user_scope=self._user_scope), announce=False
            )
        # Audit is read-only: never migrate legacy user consent here.
        # Grants come from the scope's manifest (APM_HOME/apm.yml under -g, #3954).
        manifest = scope_manifest_path(self._project_root, user_scope=self._user_scope)
        self._trust_ctx_value = exec_trust_context_for_project(
            manifest, policy=policy, migrate_user_legacy=False
        )
        self._map = materialize_exec_map(self._trust_ctx_value)
        self._resolved = True

    def _trust_ctx(self) -> Any:
        self._resolve()
        return self._trust_ctx_value

    def for_dependency(
        self, lock_dep: Any, package_info: Any | None = None
    ) -> builtins.dict[str, builtins.dict[str, bool]]:
        """Return the replay allow-map; already-authorized rows are explicit."""
        from apm_cli.security.executables import (
            AUTHORIZED_REPLAY,
            TRUST_ABSENT,
            TRUST_DEPLOYED,
            authorized_exec_map,
        )

        status = getattr(lock_dep, "exec_status", None)
        if status == TRUST_ABSENT:
            return authorized_exec_map(AUTHORIZED_REPLAY)
        if status in (None, TRUST_DEPLOYED):
            # The project's own content is always trusted (see the install gate).
            if package_info is None or getattr(lock_dep, "local_path", None) == ".":
                return authorized_exec_map(AUTHORIZED_REPLAY)
            current, _ = current_exec_decision(
                self._trust_ctx, package_info, lock_dep.get_unique_key()
            )
            if not recorded_exec_status_is_stale(status, current):
                return authorized_exec_map(AUTHORIZED_REPLAY)
        self._resolve()
        return self._map


def locked_hook_pin_mismatches(
    lockfile: Any, modules_dir: Path, trust_ctx: Any
) -> builtins.list[Any]:
    """Locked hook files whose pinned approval no longer admits them (#4188).

    ``apm install --frozen`` and ``apm audit --ci`` both route here, and each
    lock row is judged by the trust owner
    (:func:`~apm_cli.security.trust.recorded_hook_pin_mismatches`), so the two
    commands reach one verdict. *trust_ctx* may be a provider, called once and
    only when a locked package deploys hook files. A package not materialized
    under *modules_dir* is left to the checks that report it.
    """
    from apm_cli.install.drift import _build_package_info
    from apm_cli.security.executables import TRUST_ABSENT, _strip_version
    from apm_cli.security.trust import recorded_hook_pin_mismatches

    if trust_ctx is None or lockfile is None:
        return []
    resolved: builtins.list[Any] = []

    def _ctx() -> Any:
        if not resolved:
            resolved.append(trust_ctx() if callable(trust_ctx) else trust_ctx)
        return resolved[0]

    found: builtins.list[Any] = []
    for key, dep in (getattr(lockfile, "dependencies", None) or {}).items():
        if key == "." or dep.exec_status == TRUST_ABSENT:
            continue
        install_path = dep.to_dependency_ref().get_install_path(modules_dir)
        if not install_path.is_dir():
            continue
        try:
            package_info = _build_package_info(dep, install_path)
        except (OSError, ValueError):
            continue
        pkg_key = resolve_package_key(package_info, dep.get_unique_key())
        found.extend(
            recorded_hook_pin_mismatches(
                _ctx,
                tuple(_candidate_keys(pkg_key)),
                install_path,
                package=_strip_version(pkg_key),
                recorded_status=dep.exec_status,
                recorded_allowed=tuple(dep.exec_allowed_types or ()),
            )
        )
    return found
