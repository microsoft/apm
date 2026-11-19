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

    Local project content (``_local``) is always trusted.  Dependency
    packages are checked against the ``allowExecutables`` block.  When
    no ``allowExecutables`` block exists (``None``), all executables are
    considered approved (opt-in enforcement).

    When *ctx* is provided and a package is blocked, the declaration is
    recorded on ``ctx.blocked_executables`` for the post-loop prompt.
    """
    is_local = package_name == "_local"
    if is_local or allow_executables is None:
        return True, True, True, True, True

    from apm_cli.security.executables import (
        EXEC_TYPE_BIN,
        EXEC_TYPE_CANVAS,
        EXEC_TYPE_HOOKS,
        EXEC_TYPE_LSP,
        EXEC_TYPE_MCP,
        is_package_approved,
    )

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

    # Pinned hook items admit per file at the source plan (hook_file_admission).
    hooks_ok = any(
        is_package_approved(allow_executables, k, EXEC_TYPE_HOOKS) for k in candidate_keys
    ) or hooks_package_admissible(allow_executables, tuple(candidate_keys))
    bin_ok = any(is_package_approved(allow_executables, k, EXEC_TYPE_BIN) for k in candidate_keys)
    mcp_ok = any(is_package_approved(allow_executables, k, EXEC_TYPE_MCP) for k in candidate_keys)
    canvas_ok = any(
        is_package_approved(allow_executables, k, EXEC_TYPE_CANVAS) for k in candidate_keys
    )
    lsp_ok = any(is_package_approved(allow_executables, k, EXEC_TYPE_LSP) for k in candidate_keys)

    approved = {
        EXEC_TYPE_HOOKS: hooks_ok,
        EXEC_TYPE_BIN: bin_ok,
        EXEC_TYPE_MCP: mcp_ok,
        EXEC_TYPE_CANVAS: canvas_ok,
        EXEC_TYPE_LSP: lsp_ok,
    }
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
        denied: frozenset[str] = frozenset()
        from apm_cli.security.executables import TRUST_GATED, ExecTrustContext, denied_exec_types

        if unapproved and isinstance(ctx.exec_trust_ctx, ExecTrustContext):
            denied = denied_exec_types(ctx.exec_trust_ctx, candidate_keys, unapproved)
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
            from apm_cli.security.trust import package_exec_status

            status = package_exec_status(
                ctx.exec_trust_ctx, tuple(candidate_keys), _install, _decl.exec_types
            )
            if status is not None:
                ctx.package_exec_status[package_name] = status

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
        scan_package_executables,
    )
    from apm_cli.security.trust import REASON_DENIED, admit_native_plugin

    install = Path(package_info.install_path)
    verdict = admit_native_plugin(
        allow_executables,
        candidate_keys,
        install,
        transitive=_is_transitive(package_info, ctx),
        extra_types=native_declared_types(package_info),
    )
    if ctx is not None:
        if not verdict.allowed:
            ctx.blocked_executables.append(
                replace(
                    scan_package_executables(install, package_name, "", approval_identity=pkg_key),
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


def native_declared_types(package_info: Any) -> tuple[str, ...]:
    """Exec types a native plugin's IR or manifest declares beyond its files."""
    from apm_cli.install.native_plugin_admission import agent_plugin_exec_types
    from apm_cli.security.executables import EXEC_TYPE_HOOKS, EXEC_TYPE_MCP

    types = list(agent_plugin_exec_types(package_info))
    manifest = getattr(getattr(package_info, "package", None), "codex_plugin", None)
    if getattr(manifest, "has_mcp", False):
        types.append(EXEC_TYPE_MCP)
    if getattr(manifest, "has_hooks", False):
        types.append(EXEC_TYPE_HOOKS)
    return tuple(dict.fromkeys(types))


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
    """The trust owner's verdict for a native install, else ``None``."""
    if package_name == "_local" or allow_executables is None:
        return None
    if not native_install(package_info):
        return None
    from apm_cli.security.trust import admit_native_plugin

    pkg_key = resolve_package_key(package_info, package_name)
    keys = tuple(dict.fromkeys((pkg_key, pkg_key.split("#", 1)[0])))
    return admit_native_plugin(
        allow_executables,
        keys,
        Path(package_info.install_path),
        transitive=_is_transitive(package_info, ctx),
        extra_types=native_declared_types(package_info),
    )


def hook_file_admission(
    package_name: str,
    package_info: Any,
    allow_executables: builtins.dict[str, builtins.dict[str, bool]] | None,
):
    """Return the per-hook-file admission predicate for pinned hook grants.

    ``None`` means every hook file follows the package-level decision.
    """
    if package_name == "_local" or allow_executables is None:
        return None
    from apm_cli.security.trust import hook_file_filter

    pkg_key = resolve_package_key(package_info, package_name)
    keys = tuple(dict.fromkeys((pkg_key, pkg_key.split("#", 1)[0])))
    return hook_file_filter(allow_executables, keys, Path(package_info.install_path))


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
    non_interactive: bool = False,
) -> tuple[bool, str | None]:
    """Combine executable approval with the ``--trust-bin`` posture."""
    if not bin_approved:
        return True, "not_approved"
    if trust_bin is False:
        return True, "not_trusted"
    if trust_bin is None and non_interactive:
        return True, "not_trusted"
    return False, None


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
    from apm_cli.security.executables import normalize_bin_deploy_deny_key

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
    bin_policy = getattr(policy, "bin_deploy", None)
    if bin_policy is None:
        return True
    if bin_policy.deny_all:
        return False
    package_key = normalize_bin_deploy_deny_key(package_info.get_canonical_dependency_string())
    denied = {normalize_bin_deploy_deny_key(item) for item in bin_policy.deny}
    return package_key not in denied


def log_bin_status(
    skill_result: Any,
    suffix: str,
    package_name: str,
    package_info: Any,
    log_fn,
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
        log_fn(
            f"  |-- bin/ executables skipped (not approved in allowExecutables). "
            f"Run 'apm approve {package_label}' to approve."
        )
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
    denied = getattr(ctx, "package_denied_exec_types", None) or {}
    if EXEC_TYPE_HOOKS in denied.get(package_name, frozenset()):
        message = (
            f"{label}: hooks skipped (denied). "
            f"Run 'apm policy explain {label}' to see which deny applies."
        )
    else:
        message = (
            f"{label}: hooks skipped (not approved in allowExecutables). "
            f"Run 'apm approve {label}' to approve."
        )
    logger.warning(message, symbol="warning")


class ReplayExecAllowMap:
    """Resolve the install gate's allow-map once, only for gated dependencies.

    A lock entry whose ``exec_status`` is gated or denied had executables
    withheld at install, so audit replay re-derives the per-type decisions
    from the deny-wins trust owner and withholds them too (#3924). Entries
    recorded as deployed trust that record and replay ungated. The lockfile
    keeps no per-type evidence, so this is the only re-check replay makes.

    *policy_provider* returns the org policy object (or ``None``) the caller
    already fetched, so one audit run consults a single policy.
    """

    def __init__(self, project_root: Path, policy_provider: Callable[[], Any] | None = None):
        self._project_root = project_root
        self._policy_provider = policy_provider
        self._resolved = False
        self._map: builtins.dict[str, builtins.dict[str, bool]] | None = None

    def for_dependency(self, lock_dep: Any) -> builtins.dict[str, builtins.dict[str, bool]] | None:
        from apm_cli.security.executables import TRUST_ABSENT, TRUST_DEPLOYED

        if getattr(lock_dep, "exec_status", None) in (None, TRUST_ABSENT, TRUST_DEPLOYED):
            return None
        if not self._resolved:
            from apm_cli.security.executables import effective_exec_map_for_project

            if self._policy_provider is not None:
                policy = self._policy_provider()
            else:
                from apm_cli.policy.discovery import discover_policy_with_chain

                policy = getattr(discover_policy_with_chain(self._project_root), "policy", None)
            # Audit is read-only: never migrate legacy user consent here.
            self._map = effective_exec_map_for_project(
                self._project_root, policy=policy, migrate_user_legacy=False
            )
            self._resolved = True
        return self._map
