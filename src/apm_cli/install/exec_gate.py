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
) -> tuple[bool, bool, bool, bool]:
    """Return ``(hooks_approved, bin_approved, mcp_approved, canvas_approved)`` for a package.

    Local project content (``_local``) is always trusted.  Dependency
    packages are checked against the ``allowExecutables`` block.  When
    no ``allowExecutables`` block exists (``None``), all executables are
    considered approved (opt-in enforcement).

    When *ctx* is provided and a package is blocked, the declaration is
    recorded on ``ctx.blocked_executables`` for the post-loop prompt.
    """
    is_local = package_name == "_local"
    if is_local or allow_executables is None:
        return True, True, True, True

    from apm_cli.security.executables import (
        EXEC_TYPE_BIN,
        EXEC_TYPE_CANVAS,
        EXEC_TYPE_HOOKS,
        EXEC_TYPE_MCP,
        build_approval_key,
        is_package_approved,
    )

    # Build candidate keys: the dep-ref canonical key AND the name#version
    # fallback so that approvals stored under either format are honoured. The
    # version-blind name and the bare package name are also probed so that
    # version-blind grants (org recommend, ``apm approve <name>``) match
    # regardless of the installed version (#1873).
    pkg_key = resolve_package_key(package_info, package_name)
    candidate_keys = [pkg_key]

    name_blind = pkg_key.split("#", 1)[0]
    if name_blind not in candidate_keys:
        candidate_keys.append(name_blind)
    if package_name and package_name not in candidate_keys:
        candidate_keys.append(package_name)

    # Add name#version fallback when it differs from the primary key.
    _pkg = getattr(package_info, "package", None)
    if _pkg:
        _name = getattr(_pkg, "name", package_name) or package_name
        _ver = getattr(_pkg, "version", "") or ""
        alt_key = build_approval_key(_name, _ver)
        if alt_key != pkg_key:
            candidate_keys.append(alt_key)
        if _name and _name not in candidate_keys:
            candidate_keys.append(_name)

    hooks_ok = any(
        is_package_approved(allow_executables, k, EXEC_TYPE_HOOKS) for k in candidate_keys
    )
    bin_ok = any(is_package_approved(allow_executables, k, EXEC_TYPE_BIN) for k in candidate_keys)
    mcp_ok = any(is_package_approved(allow_executables, k, EXEC_TYPE_MCP) for k in candidate_keys)
    canvas_ok = any(
        is_package_approved(allow_executables, k, EXEC_TYPE_CANVAS) for k in candidate_keys
    )

    # Track blocked packages for the post-loop approval prompt, and record the
    # lockfile exec_status for the audit (Gap B) from the same scan.
    blocked = not hooks_ok or not bin_ok or not mcp_ok or not canvas_ok
    needs_status = ctx is not None and getattr(ctx, "exec_trust_ctx", None) is not None
    if ctx is not None and (blocked or needs_status):
        from apm_cli.security.executables import scan_package_executables

        _install = Path(package_info.install_path)
        _version = ""
        _pkg = getattr(package_info, "package", None)
        if _pkg:
            _version = getattr(_pkg, "version", "") or ""
        _decl = scan_package_executables(_install, package_name, _version)
        if _decl.has_executables and blocked:
            ctx.blocked_executables.append(_decl)
        if _decl.has_executables and needs_status:
            from apm_cli.security.executables import exec_status_for_declaration

            status = exec_status_for_declaration(
                ctx.exec_trust_ctx, candidate_keys, _decl.exec_types
            )
            if status is not None:
                ctx.package_exec_status[package_name] = status

    return hooks_ok, bin_ok, mcp_ok, canvas_ok


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


def log_bin_status(
    skill_result: Any,
    suffix: str,
    package_name: str,
    package_info: Any,
    log_fn,
) -> None:
    """Emit integration-tree lines for bin/ deployment or skip reasons."""
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
        _pkg_label = package_name or getattr(package_info, "name", "unknown")
        log_fn(
            f"  |-- bin/ executables skipped (not approved in allowExecutables). "
            f"Run 'apm approve {_pkg_label}' to approve."
        )
