"""Executable approval gate helpers for the install pipeline.

Extracted from ``services.py`` to stay within the LOC budget.
These helpers are used by ``integrate_package_primitives`` to enforce
the npm v12-style ``allowExecutables`` default-deny policy.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    # Pinned hook items admit per file at the source plan (hook_file_admission).
    approved[EXEC_TYPE_HOOKS] = approved[EXEC_TYPE_HOOKS] or hooks_package_admissible(
        allow_executables, tuple(candidate_keys)
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


def _is_transitive(package_info: Any, ctx: InstallContext | None) -> bool:
    """Whether the resolved graph places this package below a direct dependency."""
    dep_ref = getattr(package_info, "dependency_ref", None)
    graph = getattr(ctx, "dependency_graph", None)
    tree = getattr(graph, "dependency_tree", None)
    if dep_ref is None or tree is None:
        return False
    depth = getattr(tree.get_node(dep_ref.get_unique_key()), "depth", 1)
    return isinstance(depth, int) and depth > 1


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


def plugin_admission(
    package_name: str,
    package_info: Any,
    allow_executables: builtins.dict[str, builtins.dict[str, bool]] | None,
    *,
    ctx: InstallContext | None = None,
):
    """The trust owner's verdict for a native install, else ``None`` (gate off)."""
    from apm_cli.security.executables import exec_map_authorizes_all

    if not native_install(package_info) or exec_map_authorizes_all(allow_executables):
        return None
    return native_plugin_verdict(package_name, package_info, allow_executables, ctx=ctx)


def native_plugin_verdict(
    package_name: str,
    package_info: Any,
    allow_executables: builtins.dict[str, builtins.dict[str, bool]] | None,
    *,
    ctx: InstallContext | None = None,
):
    """Whole-plugin verdict on the ``apm_modules`` source, else ``None`` (local).

    Claude's user-scope route (#3290) asks this directly: whether that route
    applies depends on targets and scope, which :func:`native_install` cannot see.
    With the gate off the trust owner still refuses a transitive plugin with
    MCP servers unless ``--trust-transitive-mcp`` is set, as it refuses a
    loose transitive server.
    """
    from apm_cli.security.executables import (
        AUTHORIZED_GATE_DISABLED,
        FIRST_PARTY_PACKAGE,
        closed_exec_map,
        exec_map_authorizes_all,
    )

    if package_name == FIRST_PARTY_PACKAGE:
        return None
    from apm_cli.security.trust import admit_native_plugin

    # A gate-off map still asks the owner (transitive MCP needs trust); replay
    # of already-authorized content has no context; a missing map admits nothing.
    if exec_map_authorizes_all(allow_executables):
        source = (
            getattr(ctx, "exec_trust_ctx", None)
            if getattr(allow_executables, "authorized_by", None) == AUTHORIZED_GATE_DISABLED
            else None
        )
    else:
        source = closed_exec_map(allow_executables)

    pkg_key = resolve_package_key(package_info, package_name)
    keys = tuple(dict.fromkeys((pkg_key, pkg_key.split("#", 1)[0])))
    return admit_native_plugin(
        source,
        keys,
        Path(package_info.install_path),
        transitive=_is_transitive(package_info, ctx),
        one_off=bool(getattr(ctx, "trust_transitive_mcp", False)),
    )


def hook_file_admission(
    package_name: str,
    package_info: Any,
    allow_executables: builtins.dict[str, builtins.dict[str, bool]] | None,
):
    """Return the per-hook-file admission predicate for pinned hook grants.

    ``None`` means every hook file follows the package-level decision.
    """
    from apm_cli.security.executables import FIRST_PARTY_PACKAGE, exec_map_authorizes_all

    if package_name == FIRST_PARTY_PACKAGE or exec_map_authorizes_all(allow_executables):
        return None
    from apm_cli.security.trust import hook_file_filter

    pkg_key = resolve_package_key(package_info, package_name)
    keys = tuple(dict.fromkeys((pkg_key, pkg_key.split("#", 1)[0])))
    return hook_file_filter(allow_executables, keys, Path(package_info.install_path))


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
    from apm_cli.security.executables import scan_package_executables
    from apm_cli.security.trust import native_plugin_exec_types, package_exec_decision

    pkg_key = resolve_package_key(package_info, package_name)
    install = Path(package_info.install_path)
    native = native_install(package_info)
    if native:
        exec_types = native_plugin_exec_types(install)
    else:
        exec_types = scan_package_executables(
            install, package_name, "", approval_identity=pkg_key
        ).exec_types
    if not exec_types:
        return None, ()
    return package_exec_decision(
        trust_ctx_provider(), tuple(_candidate_keys(pkg_key)), install, exec_types, native=native
    )


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
    install gate to deploy or retire executables and rewrite the lock. A row
    recorded while the gate was off is compared against the effective gate,
    org policy included, so a policy that newly gates executables re-gates
    it as ``apm install`` does (#3990). Org policy is fetched only when a
    locked package declares executables. Packages are read the way audit replay reads them, under the *targets*'
    native registration capability. Trust (and *policy_provider*) resolve
    only when a package declares executables. An unreadable package or a
    provider raising :class:`ExecPolicyUnavailable` fails toward running
    the gate, which applies the policy fetch-failure posture.
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
    with native_registration_scope(targets):
        for dep in rows:
            install_path = dep.to_dependency_ref().get_install_path(modules_dir)
            if not install_path.is_dir():
                continue
            try:
                package_info = _build_package_info(dep, install_path)
                status, allowed_types = current_exec_decision(
                    _trust_ctx, package_info, dep.get_unique_key()
                )
            except ExecPolicyUnavailable:
                return True
            except (OSError, ValueError):
                # An unreadable package: let install, which reports it, decide.
                return True
            if recorded_exec_status_is_stale(
                dep.exec_status,
                status,
                recorded_allowed=dep.exec_allowed_types,
                current_allowed=allowed_types,
            ):
                return True
    return False


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


def resolve_bin_skip(
    bin_approved: bool,
    trust_bin: bool | None,
    *,
    non_interactive: bool,
) -> tuple[bool, str | None]:
    """Combine executable approval with the ``--trust-bin`` posture (trust owner)."""
    from apm_cli.security.executables import compose_trust_bin

    return compose_trust_bin(bin_approved, trust_bin, non_interactive=non_interactive)


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


def bin_deploy_denial(package_info: Any, policy: Any) -> str | None:
    """Name the org ``bin_deploy`` rule that blocks this package's bin/, else ``None``."""
    from apm_cli.security.executables import normalize_bin_deploy_deny_key

    bin_policy = getattr(policy, "bin_deploy", None)
    if bin_policy is None:
        return None
    if bin_policy.deny_all:
        return "bin_deploy.deny_all"
    package_key = normalize_bin_deploy_deny_key(package_info.get_canonical_dependency_string())
    denied = {normalize_bin_deploy_deny_key(item) for item in bin_policy.deny}
    return "bin_deploy.deny" if package_key in denied else None


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
    approve = approve_command(label, user_scope=user_scope)
    return f"{kind} skipped (not approved in executables.allow). Run '{approve}' to approve."


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
            f"Run 'apm install --trust-bin {package_label}' to deploy."
        )
    elif skill_result.bin_skipped_reason == "not_retrusted_on_uninstall":
        log_fn(
            "  |-- bin/ executables not re-deployed during uninstall cleanup. "
            f"Run 'apm install --trust-bin {package_label}' to restore them."
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
        from apm_cli.core.scope import manifest_root
        from apm_cli.security.executables import (
            exec_trust_context_for_project,
            materialize_exec_map,
            project_manifest_path,
        )

        if self._policy_provider is not None:
            policy = self._policy_provider()
        else:
            from apm_cli.policy.outcome_routing import resolve_trust_policy

            # The replaying command reports the policy outcome itself.
            policy = resolve_trust_policy(self._project_root, announce=False)
        # Audit is read-only: never migrate legacy user consent here.
        # Grants come from the scope's manifest (APM_HOME/apm.yml under -g,
        # #3954); policy stays anchored at the deploy root.
        manifest = project_manifest_path(
            manifest_root(self._project_root, user_scope=self._user_scope)
        )
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
