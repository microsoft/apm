"""Executable approval gate helpers for the install pipeline.

Extracted from ``services.py`` to stay within the LOC budget.
These helpers are used by ``integrate_package_primitives`` to enforce
the npm v12-style ``allowExecutables`` default-deny policy.
"""

from __future__ import annotations

import builtins
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

    hooks_ok = any(
        is_package_approved(allow_executables, k, EXEC_TYPE_HOOKS) for k in candidate_keys
    )
    bin_ok = any(is_package_approved(allow_executables, k, EXEC_TYPE_BIN) for k in candidate_keys)
    mcp_ok = any(is_package_approved(allow_executables, k, EXEC_TYPE_MCP) for k in candidate_keys)
    canvas_ok = any(
        is_package_approved(allow_executables, k, EXEC_TYPE_CANVAS) for k in candidate_keys
    )
    lsp_ok = any(is_package_approved(allow_executables, k, EXEC_TYPE_LSP) for k in candidate_keys)

    # Track blocked packages for the post-loop approval prompt, and record the
    # lockfile exec_status for the audit (Gap B) from the same scan.
    blocked = not hooks_ok or not bin_ok or not mcp_ok or not canvas_ok or not lsp_ok
    needs_status = ctx is not None and getattr(ctx, "exec_trust_ctx", None) is not None
    if ctx is not None and (blocked or needs_status):
        from apm_cli.security.executables import scan_package_executables

        _install = Path(package_info.install_path)
        _decl = scan_package_executables(
            _install,
            package_name,
            "",
            approval_identity=pkg_key,
        )
        if _decl.has_executables and blocked:
            ctx.blocked_executables.append(_decl)
        if _decl.has_executables and needs_status:
            from apm_cli.security.executables import exec_status_for_declaration

            status = exec_status_for_declaration(
                ctx.exec_trust_ctx, candidate_keys, _decl.exec_types
            )
            if status is not None:
                ctx.package_exec_status[package_name] = status

    return hooks_ok, bin_ok, mcp_ok, canvas_ok, lsp_ok


def resolve_package_key(package_info: Any, package_name: str) -> str:
    """Build the ``allowExecutables`` lookup key for a package.

    Tries ``dependency_ref`` first (canonical dependency string), then
    falls back to ``name#version`` from the package's own metadata.
    """
    from apm_cli.security.executables import build_approval_key

    # Prefer the dependency reference's canonical string (includes version/ref)
    dep_ref = getattr(package_info, "dependency_ref", None)
    if dep_ref is not None:
        canonical = getattr(dep_ref, "canonical_string", None)
        if callable(canonical):
            cs = canonical()
            if cs:
                return cs
        # Fall back to str(dep_ref)
        s = str(dep_ref)
        if s:
            return s

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
