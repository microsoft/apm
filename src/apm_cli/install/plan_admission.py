"""Per-package admission verdicts the deployment plan reads (moved from exec_gate.py).

The native-plugin verdict, the per-hook-file admission predicate, the org
``bin_deploy`` denial, the ``--trust-bin`` posture and the declared executable
types the gate scans come from the trust owners; ``exec_gate`` re-exports them.
"""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def _is_transitive(package_info: Any, ctx: InstallContext | None) -> bool:
    """Whether the resolved graph places this package below a direct dependency."""
    dep_ref = getattr(package_info, "dependency_ref", None)
    graph = getattr(ctx, "dependency_graph", None)
    tree = getattr(graph, "dependency_tree", None)
    if dep_ref is None or tree is None:
        return False
    depth = getattr(tree.get_node(dep_ref.get_unique_key()), "depth", 1)
    return isinstance(depth, int) and depth > 1


def plugin_admission(
    package_name: str,
    package_info: Any,
    allow_executables: builtins.dict[str, builtins.dict[str, bool]] | None,
    *,
    ctx: InstallContext | None = None,
):
    """The trust owner's verdict for a native install, else ``None`` (gate off)."""
    from apm_cli.install.exec_gate import native_install
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
    from apm_cli.install.exec_gate import resolve_package_key
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
    from apm_cli.install.exec_gate import resolve_package_key
    from apm_cli.security.trust import hook_file_filter

    pkg_key = resolve_package_key(package_info, package_name)
    keys = tuple(dict.fromkeys((pkg_key, pkg_key.split("#", 1)[0])))
    return hook_file_filter(allow_executables, keys, Path(package_info.install_path))


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


def _declared_exec_types(
    package_info: Any, package_name: str
) -> tuple[str, Path, bool, tuple[str, ...]]:
    """Scan one materialized package as the install gate does (native union included)."""
    from apm_cli.install.exec_gate import native_install, resolve_package_key
    from apm_cli.security.executables import scan_package_executables
    from apm_cli.security.trust import native_plugin_exec_types

    pkg_key = resolve_package_key(package_info, package_name)
    install = Path(package_info.install_path)
    native = native_install(package_info)
    if native:
        exec_types = native_plugin_exec_types(install)
    else:
        exec_types = scan_package_executables(
            install, package_name, "", approval_identity=pkg_key
        ).exec_types
    return pkg_key, install, native, tuple(exec_types)


def _recorded_deployed_types(dep: Any, exec_types: tuple[str, ...]) -> tuple[str, ...]:
    """The declared types the lock records as deployed: all, or a partial approval's (#3991)."""
    from apm_cli.security.executables import TRUST_DEPLOYED

    if dep.exec_status in (None, TRUST_DEPLOYED):
        return exec_types
    return tuple(exec_type for exec_type in exec_types if exec_type in dep.exec_allowed_types)


def resolve_bin_skip(
    bin_approved: bool,
    trust_bin: bool | None,
    *,
    non_interactive: bool,
) -> tuple[bool, str | None]:
    """Combine executable approval with the ``--trust-bin`` posture (trust owner)."""
    from apm_cli.security.executables import compose_trust_bin

    return compose_trust_bin(bin_approved, trust_bin, non_interactive=non_interactive)
