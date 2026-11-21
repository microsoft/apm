"""Admission of intact legacy plugins to Claude's native loader."""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from apm_cli.core.remediation import CLAUDE_PLUGIN_BIN_UNTRUSTED
from apm_cli.core.scope import is_user_scope
from apm_cli.deps.plugin_parser import (
    claude_native_mcp_servers,
    has_normalized_plugin_skill_sources_receipt,
    parse_plugin_manifest,
)
from apm_cli.models.apm_package import PackageInfo, PackageType
from apm_cli.models.dependency.subsets import skill_subset_filter_tokens
from apm_cli.native_plugins.record import is_safe_plugin_name
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
    ``approved_pin`` is the whole-plugin approval that admitted it; the
    deployed copy must carry exactly that pin.
    """

    dependency_key: str
    name: str
    source_root: Path
    bundle_hash: str
    parked: str | None = None
    warning: str | None = None
    approved_pin: str | None = None
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


def pin_native_claude_source(root: Path) -> str:
    """The trust owner's whole-plugin approval pin of *root*."""
    from apm_cli.security.trust_fingerprint import native_plugin_fingerprint

    try:
        return native_plugin_fingerprint(root)
    except (OSError, PathTraversalError) as exc:
        raise ClaudeNativePluginError(
            f"Cannot compute the approval pin of native Claude source {root}: unreadable data, "
            "unsafe path or symlink. Restore the intact package before retrying."
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
# model, so APM can neither vet nor pin their effect: park the plugin. A full
# allow-list of root entries would refuse the inert data directories
# (scripts/, resources/) that hooks and commands reference, which the
# whole-plugin pin already covers. monitors/ and workflows/ run commands
# (also gated as executables by security.trust.native_root_exec_types).
# bin/ is supported: it deploys inside the plugin root under the whole-plugin
# approval, the org bin_deploy policy and the --trust-bin posture.
_UNSUPPORTED_ROOTS = (
    ".apm/bin",
    ".apm/canvas",
    "settings.json",
    "output-styles",
    "rules",
    "monitors",
    "workflows",
)
_EXCLUDE_CLAUDE = "or exclude Claude from this dependency's targets"


def _escaping_component_path(root: Path, manifest: dict[str, Any]) -> str | None:
    """Name a manifest path outside the plugin directory (req-na-002), else ``None``."""
    resolved = root.resolve()
    for field in _COMPONENT_PATH_FIELDS:
        value = manifest.get(field)
        for entry in value if isinstance(value, list) else [value]:
            if isinstance(entry, str) and not (root / entry).resolve().is_relative_to(resolved):
                return f"manifest field '{field}' points outside the plugin ({entry})"
    return None


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


@dataclass(frozen=True)
class _Ineligible:
    """Why Claude cannot load a plugin intact, and the one next step."""

    why: str
    step: str
    name: str = ""


def _intact_source(
    root: Path, skill_subset: tuple[str, ...] | None
) -> _Ineligible | tuple[dict[str, Any], str, str]:
    """Return ``(manifest, name, fingerprint)``, or why the plugin cannot load intact."""
    manifest_path = root / ".claude-plugin/plugin.json"
    if not manifest_path.is_file():
        return _Ineligible(
            "it has no .claude-plugin/plugin.json, so Claude cannot load it intact",
            f"Ask the publisher to add .claude-plugin/plugin.json, {_EXCLUDE_CLAUDE}",
        )
    if skill_subset_filter_tokens(skill_subset) is not None:
        return _Ineligible(
            "Claude loads whole plugins, so its skills: selection cannot be honored",
            f"Remove the skills: selection to install the complete plugin, {_EXCLUDE_CLAUDE}",
        )
    if root.is_symlink() or has_symlink_component(root, manifest_path):
        return _Ineligible(
            "its .claude-plugin/plugin.json path contains a symlink",
            f"Ask the publisher to ship the manifest as a regular file, {_EXCLUDE_CLAUDE}",
        )
    try:
        manifest = parse_plugin_manifest(manifest_path)
    except (OSError, ValueError):
        return _Ineligible(
            "its .claude-plugin/plugin.json cannot be read as a JSON object",
            f"Ask the publisher to fix the manifest, {_EXCLUDE_CLAUDE}",
        )
    name = manifest.get("name")
    if not is_safe_plugin_name(name):
        return _Ineligible(
            f"its plugin name {name!r} is missing or unsafe (letters, digits, '-' and '_')",
            f"Ask the publisher to fix 'name' in .claude-plugin/plugin.json, {_EXCLUDE_CLAUDE}",
        )
    try:
        fingerprint = fingerprint_native_claude_source(root)
    except ClaudeNativePluginError:
        return _Ineligible(
            "it contains a symlink or an unreadable file, which an intact copy cannot carry",
            f"Ask the publisher to replace symlinks with regular files, {_EXCLUDE_CLAUDE}",
            name,
        )
    return manifest, name, fingerprint


def _unsupported_capability(root: Path, manifest: dict[str, Any]) -> _Ineligible | None:
    """Name a capability this route cannot resolve or vet, else ``None``."""
    if manifest.get("dependencies"):
        return _Ineligible(
            "it declares plugin dependencies, which APM cannot resolve yet",
            f"Install each required plugin as its own dependency, {_EXCLUDE_CLAUDE}",
        )
    unknown = set(manifest) - _MANIFEST_FIELDS - {"dependencies"}
    unsupported = [entry for entry in _UNSUPPORTED_ROOTS if (root / entry).exists()]
    if unknown or unsupported:
        return _Ineligible(
            "it ships " + ", ".join(sorted(unknown) + unsupported) + ", which APM cannot vet "
            "for Claude (supported: commands, skills, agents, hooks, bin/, MCP and LSP servers)",
            f"Ask the publisher to drop them, {_EXCLUDE_CLAUDE}",
        )
    escaping = _escaping_component_path(root, manifest)
    if escaping is not None:
        return _Ineligible(escaping, f"Ask the publisher to fix the manifest, {_EXCLUDE_CLAUDE}")
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
    bin_denied: str | None = None,
) -> ClaudeNativePlugin | None:
    """Admit a complete, approved plugin without altering native selection.

    *admit* returns the trust owner's whole-plugin verdict on this source
    (``security.trust.admit_native_plugin``), or ``None`` for local content.
    That verdict covers the plugin's MCP and LSP servers and its ``bin/``,
    including the default refusal of a transitive plugin's MCP servers with
    the gate off. *policy* is the org policy: a declared MCP server it rejects
    parks the whole plugin under ``enforcement: block`` (Claude cannot enable
    part of a plugin). *bin_denied* names the org ``bin_deploy`` rule that
    blocks this package's ``bin/``; *skip_bin* is the ``--trust-bin`` posture
    for this run. Either parks a plugin that ships ``bin/``.

    A plugin Claude cannot load intact is parked, never projected loose: the
    install continues and the warning names the package and the next step.

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

    def park(why: str, step: str, name: str, fingerprint: str = "") -> ClaudeNativePlugin:
        return ClaudeNativePlugin(
            package_name,
            name,
            root.resolve(),
            fingerprint,
            parked=f"Parked native plugin {package_name}: {why}\n    {step}",
        )

    intact = _intact_source(root, skill_subset)
    if isinstance(intact, _Ineligible):
        return park(intact.why, intact.step, intact.name or root.name)
    manifest, name, fingerprint = intact
    unsupported = _unsupported_capability(root, manifest)
    if unsupported is not None:
        return park(unsupported.why, unsupported.step, name, fingerprint)
    violation = _mcp_policy_violation(root, manifest, policy)
    if violation is not None and getattr(policy, "enforcement", None) == "block":
        return park(
            violation,
            "Ask your org policy owner to allow the server, or remove this dependency",
            name,
            fingerprint,
        )
    ships_bin = (root / "bin").exists()
    if ships_bin and bin_denied is not None:
        return park(
            f"org policy {bin_denied} blocks its bin/ executables",
            "Ask your org policy owner to allow it, or remove this dependency",
            name,
            fingerprint,
        )
    warning = None if violation is None else f"Native plugin {package_name}: {violation}"
    parked = None
    verdict = admit()
    if verdict is not None and not verdict.allowed:
        from apm_cli.security.trust import parked_plugin_message

        parked = parked_plugin_message(
            package_name, str(verdict.reason), verdict.detail, user_scope=True
        )
    elif ships_bin and skip_bin:
        return park(
            "its bin/ executables are not trusted in this run",
            CLAUDE_PLUGIN_BIN_UNTRUSTED.sentence(),
            name,
            fingerprint,
        )
    return ClaudeNativePlugin(
        package_name,
        name,
        root.resolve(),
        fingerprint,
        parked=parked,
        warning=warning,
        approved_pin=getattr(verdict, "pin", None) if parked is None else None,
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
