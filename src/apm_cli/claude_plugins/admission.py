"""Admission of intact legacy plugins to Claude's native loader."""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from apm_cli.core.scope import is_user_scope
from apm_cli.deps.plugin_parser import (
    claude_native_mcp_servers,
    has_normalized_plugin_skill_sources_receipt,
    parse_plugin_manifest,
)
from apm_cli.models.apm_package import PackageInfo, PackageType
from apm_cli.models.dependency.subsets import skill_subset_filter_tokens
from apm_cli.utils.content_hash import compute_native_plugin_hash
from apm_cli.utils.path_security import PathTraversalError, has_symlink_component


class ClaudeNativePluginError(RuntimeError):
    """Native registration cannot safely honor the requested package."""


@dataclass(frozen=True, slots=True)
class ClaudeNativePlugin:
    """An admitted dependency, not a claim of native activation or execution.

    A *parked* plugin stays out of loose Claude projection but is not declared
    to Claude either; ``parked`` carries the one diagnostic shown to the user.
    ``warning`` reports an org MCP policy violation under ``enforcement: warn``.
    """

    dependency_key: str
    name: str
    source_root: Path
    bundle_hash: str
    parked: str | None = None
    warning: str | None = None
    target_name: ClassVar[str] = "claude"


def fingerprint_native_claude_source(root: Path) -> str:
    """Translate canonical native fingerprint failures at the Claude boundary."""
    try:
        return compute_native_plugin_hash(root)
    except (OSError, PathTraversalError) as exc:
        raise ClaudeNativePluginError(
            f"Cannot fingerprint native Claude source {root}: unreadable data, unsafe path or "
            "symlink. Restore the intact package before retrying."
        ) from exc


_MANIFEST_FIELDS = frozenset(
    {
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "commands",
        "agents",
        "skills",
        "hooks",
        "mcpServers",
        "lspServers",
    }
)
_COMPONENT_PATH_FIELDS = ("commands", "agents", "skills", "hooks", "mcpServers", "lspServers")
# Plugin-root entries Claude loads as a capability that this route does not
# model, so APM can neither vet nor pin their effect: refuse the plugin. A
# full allow-list of root entries would refuse the inert data directories
# (scripts/, resources/) that hooks and commands reference, which the
# whole-plugin pin already covers. monitors/ and workflows/ run commands
# (also gated as executables by security.trust.native_root_exec_types).
_UNSUPPORTED_ROOTS = (
    "bin",
    ".apm/bin",
    ".apm/canvas",
    "settings.json",
    "output-styles",
    "rules",
    "monitors",
    "workflows",
)


def _refuse_escaping_component_paths(root: Path, manifest: dict[str, Any]) -> None:
    """A manifest path outside the plugin directory leaves no plugin root (req-na-002)."""
    resolved = root.resolve()
    for field in _COMPONENT_PATH_FIELDS:
        value = manifest.get(field)
        for entry in value if isinstance(value, list) else [value]:
            if isinstance(entry, str) and not (root / entry).resolve().is_relative_to(resolved):
                raise ClaudeNativePluginError(
                    f"Native Claude plugin manifest field '{field}' points outside the plugin "
                    f"({entry}). Fix the manifest or exclude Claude from this dependency's targets."
                )


def _mcp_policy_violation(root: Path, manifest: dict[str, Any], policy: Any) -> str | None:
    """Name the first MCP server org MCP policy rejects, else ``None``.

    Claude loads the plugin's servers itself, so the checks the install gate
    runs on loose servers (``policy_checks.mcp_policy_checks``) run here on
    every server Claude's loader would start (``claude_native_mcp_servers``).
    An entry APM cannot read never passes a policy that restricts anything.
    LSP servers have no org policy schema beyond ``executables``; the
    whole-plugin trust verdict is their policy.
    """
    if getattr(policy, "mcp", None) is None or getattr(policy, "enforcement", "warn") == "off":
        return None
    servers, unmodeled = claude_native_mcp_servers(root, manifest)
    if not servers and not unmodeled:
        return None
    from apm_cli.models.dependency.mcp import MCPDependency
    from apm_cli.policy.policy_checks import mcp_policy_checks, mcp_policy_restricts

    if unmodeled and mcp_policy_restricts(policy):
        return f"MCP {unmodeled[0]} cannot be checked against org MCP policy (APM cannot read it)"
    subjects = [
        MCPDependency(
            name=server["name"],
            transport=server["transport"],
            registry=False,
            url=server["url"],
            command=server["command"],
        )
        for server in servers
    ]
    for check in mcp_policy_checks(subjects, policy):
        if not check.passed:
            subject = check.violation_subjects()[0]
            return f"MCP server '{subject.ref}' violates org policy {check.name} ({subject.reason})"
    return None


def plan_native_claude_plugin(
    package_info: PackageInfo,
    *,
    package_name: str,
    targets: list[Any],
    scope: Any,
    skill_subset: tuple[str, ...] | None,
    admit: Callable[[], Any],
    skip_bin: bool,
    policy: Any = None,
) -> ClaudeNativePlugin | None:
    """Admit a complete, approved plugin without altering native selection.

    *admit* returns the trust owner's whole-plugin verdict on this source
    (``security.trust.admit_native_plugin``), or ``None`` for local content.
    That verdict covers the plugin's MCP and LSP servers too, including the
    default refusal of a transitive plugin's MCP servers with the gate off. *policy* is the
    org policy: a declared MCP server it rejects parks the whole plugin under
    ``enforcement: block`` (Claude cannot enable part of a plugin).

    Normalized virtual plugins in older locks can have SKILL_BUNDLE type.
    Only the parser-owned normalization receipt admits that compatibility
    case; an incidental Claude manifest never overrides APM package intent.
    """
    if not is_user_scope(scope) or not any(t.name == "claude" for t in targets):
        return None
    root = Path(package_info.install_path)
    package_type = package_info.package_type
    reference = package_info.dependency_ref
    normalized_virtual = (
        package_type is PackageType.SKILL_BUNDLE
        and reference is not None
        and reference.is_virtual
        and has_normalized_plugin_skill_sources_receipt(root)
    )
    if package_type is not PackageType.MARKETPLACE_PLUGIN and not normalized_virtual:
        return None
    manifest_path = root / ".claude-plugin/plugin.json"
    if not manifest_path.is_file():
        raise ClaudeNativePluginError(
            "Native Claude registration requires an intact .claude-plugin/plugin.json."
        )
    if skill_subset_filter_tokens(skill_subset) is not None:
        raise ClaudeNativePluginError(
            "Native Claude plugins cannot honor a skill subset. Install the complete plugin "
            "or exclude Claude from this dependency's targets."
        )
    if root.is_symlink() or has_symlink_component(root, manifest_path):
        raise ClaudeNativePluginError("Native Claude manifest path must not contain a symlink.")
    try:
        manifest = parse_plugin_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        raise ClaudeNativePluginError("Cannot read the native Claude plugin manifest.") from exc
    name = manifest.get("name")
    if not isinstance(name, str) or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name) is None:
        raise ClaudeNativePluginError("Native Claude plugin requires a safe, explicit plugin name.")
    fingerprint = fingerprint_native_claude_source(root)
    if manifest.get("dependencies"):
        return ClaudeNativePlugin(
            package_name,
            name,
            root.resolve(),
            fingerprint,
            parked=(
                f"Parked native plugin {package_name}: it declares plugin dependencies, "
                "which APM cannot resolve yet\n    Install each required plugin as its "
                "own dependency, or exclude Claude from this dependency's targets"
            ),
        )
    unknown = set(manifest) - _MANIFEST_FIELDS - {"dependencies"}
    unsupported = [entry for entry in _UNSUPPORTED_ROOTS if (root / entry).exists()]
    if unknown or unsupported:
        raise ClaudeNativePluginError(
            "Native Claude plugin has unqualified capabilities: "
            + ", ".join(sorted(unknown) + unsupported)
            + ". This route supports intact commands, skills, agents, approved hooks and "
            "MCP and LSP servers only."
        )
    _refuse_escaping_component_paths(root, manifest)
    violation = _mcp_policy_violation(root, manifest, policy)
    if violation is not None and getattr(policy, "enforcement", None) == "block":
        return ClaudeNativePlugin(
            package_name,
            name,
            root.resolve(),
            fingerprint,
            parked=(
                f"Parked native plugin {package_name}: {violation}\n    Ask your org policy "
                "owner to allow the server, or remove this dependency"
            ),
        )
    warning = None if violation is None else f"Native plugin {package_name}: {violation}"
    parked = None
    verdict = admit()
    if verdict is not None and not verdict.allowed:
        from apm_cli.security.trust import parked_plugin_message

        parked = parked_plugin_message(package_name, str(verdict.reason), verdict.detail)
    return ClaudeNativePlugin(
        package_name, name, root.resolve(), fingerprint, parked=parked, warning=warning
    )


def native_service_runtimes(
    package_key: str, locked: Any, run_keys: Collection[str] = ()
) -> frozenset[str]:
    """Runtimes that load a dependency's MCP and LSP servers from its native plugin.

    Claude loads an admitted plugin's servers itself, so APM must not also
    write them into Claude's config; a parked plugin is not deployed, so its
    servers must not reach Claude loose either. *run_keys* are the plugins
    this install routed natively (admitted or parked); the lock's
    ``native_plugin`` record carries the admitted ones to every later reader.
    """
    if package_key in run_keys or getattr(locked, "native_plugin", None) is not None:
        return frozenset({ClaudeNativePlugin.target_name})
    return frozenset()


def queue_native_claude(ctx: Any, package_name: str, plugin: Any, diagnostics: Any) -> None:
    """Hand an admitted plugin to native declaration; report a parked one.

    A parked plugin is neither projected loose nor declared to Claude
    (OpenAPM req-na-005): it is not deployed and the user sees why. Any
    pending-approval entry for it is marked native, so approving pins the
    whole plugin rather than per-file items its native route never reads.
    """
    if plugin is None or ctx is None:
        return
    native_keys = getattr(ctx, "claude_native_keys", None)
    if native_keys is not None:
        native_keys.add(package_name)
    if plugin.warning and diagnostics is not None:
        diagnostics.warn(plugin.warning, package=package_name)
    if plugin.parked:
        from dataclasses import replace

        blocked = getattr(ctx, "blocked_executables", None)
        if blocked:
            blocked[:] = [
                replace(decl, native_plugin=True)
                if decl.install_path and Path(decl.install_path).resolve() == plugin.source_root
                else decl
                for decl in blocked
            ]
        if diagnostics is not None:
            diagnostics.warn(plugin.parked, package=package_name)
        return
    ctx.claude_native_plugins[package_name] = plugin
