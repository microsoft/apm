"""Agent Plugin bundle exporter."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from ..agent_plugins import (
    MCP_SCHEMA_ID,
    PLUGIN_SCHEMA_ID,
    AgentPluginAsset,
    AgentPluginExecutable,
    DiagnosticSeverity,
    args_contain_literal_secret,
    contains_literal_secret_fields,
    load_agent_plugin,
    url_contains_literal_secret,
)
from ..deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed
from ..deps.plugin_parser import synthesize_plugin_json_from_apm_yml
from ..models.apm_package import APMPackage
from ..utils.archive import (
    projected_archive_path,
    validate_archive_format,
)
from ..utils.console import _rich_warning
from ..utils.path_security import ensure_path_within, safe_rmtree
from .export_common import (
    _cache_would_contribute_hooks_or_mcp,
    _cache_would_contribute_primitives,
    _collect_apm_components,
    _collect_deployed_components,
    _collect_explicit_local_components,
    _collect_root_plugin_components,
    _dep_install_path,
    _get_dev_dependency_urls,
    _merge_file_map,
    _sanitize_bundle_name,
    _scan_bundle_sources,
    _warn_no_local_primitives,
    _warn_skipped_root_components,
    _write_bundle_sources,
)
from .formats import BundleFormat, agent_plugin_warning
from .lockfile_enrichment import enrich_lockfile_for_pack
from .packer import PackResult
from .reproducible_archive import write_reproducible_archive


def _portable_components(
    components: list[tuple[Path, str]],
) -> tuple[list[tuple[Path, str]], set[str]]:
    """Split collected components into portable skills and dropped surfaces.

    Portable Agent Plugins v1 carries only root ``skills/``; every other
    collected top-level directory is a non-portable primitive that the
    portable core cannot represent. Return the retained skill components and
    the set of rejected top-level directory names for fail-closed admission.
    """
    retained: list[tuple[Path, str]] = []
    dropped: set[str] = set()
    for source, rel in components:
        if rel.startswith("skills/"):
            retained.append((source, rel))
        else:
            dropped.add(Path(rel).parts[0])
    return retained, dropped


def _require_portable_agent_plugin(dropped_surfaces: set[str]) -> None:
    """Reject an Agent Plugin projection that would discard source primitives."""
    if not dropped_surfaces:
        return

    surfaces = ", ".join(sorted(dropped_surfaces))
    message = (
        "Cannot pack Agent Plugin: non-portable primitives would be discarded "
        f"({surfaces}). Agent Plugins v1 portable components are limited to root "
        "plugin.json, skills/, and root mcp.json."
    )
    legacy_surfaces = sorted(dropped_surfaces - {"lsp"})
    if legacy_surfaces:
        message += (
            " Use 'apm pack --format claude-plugin' to preserve "
            f"{', '.join(legacy_surfaces)} in the legacy Claude client format."
        )
    if "lsp" in dropped_surfaces:
        message += (
            " LSP configuration is carried by neither 'agent-plugin' nor "
            "'claude-plugin'; configure LSP in the target client instead."
        )
    raise ValueError(message)


def _apm_hooks_present(apm_dir: Path) -> bool:
    """Report whether ``.apm/hooks/`` carries a hook document.

    Hooks are not representable by the portable Agent Plugins v1 core and are
    collected on a separate channel from :func:`_portable_components`, so this
    probe lets the caller fold ``hooks`` into fail-closed portable admission
    without opening or parsing the files.
    """
    hooks_dir = apm_dir / "hooks"
    if not hooks_dir.is_dir():
        return False
    return any(
        entry.is_file() and entry.suffix == ".json" and not entry.is_symlink()
        for entry in hooks_dir.iterdir()
    )


def _root_hooks_present(project_root: Path) -> bool:
    """Report whether root-layout hooks exist without parsing them.

    Packages that follow the plugin directory convention at the repo root (no
    ``.apm/``) carry hooks as a root ``hooks.json`` file or ``hooks/`` directory.
    :func:`_collect_root_plugin_components` deliberately skips hooks, so this
    bounded existence probe -- mirroring ``_collect_hooks_from_root`` discovery
    -- lets the caller fold ``hooks`` into fail-closed portable admission
    without opening or parsing the files.
    """
    hooks_file = project_root / "hooks.json"
    if hooks_file.is_file() and not hooks_file.is_symlink():
        return True
    hooks_dir = project_root / "hooks"
    if not hooks_dir.is_dir():
        return False
    return any(
        entry.is_file() and entry.suffix == ".json" and not entry.is_symlink()
        for entry in hooks_dir.iterdir()
    )


def _copy_optional_root_file(project_root: Path, bundle_dir: Path, name: str) -> str | None:
    source = project_root / name
    if not source.is_file() or source.is_symlink():
        return None
    dest = bundle_dir / name
    ensure_path_within(dest, bundle_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest, follow_symlinks=False)
    return name


def _agent_mcp_document(package: APMPackage, lockfile: LockFile) -> dict:
    """Project resolved production MCP configs into the Agent Plugins schema."""
    dev_names = {dependency.name for dependency in package.get_dev_mcp_dependencies()}
    prod_names = {dependency.name for dependency in package.get_mcp_dependencies()}
    servers: dict[str, dict] = {}
    resolved_names = set(lockfile.mcp_servers)
    for name, raw_config in lockfile.mcp_configs.items():
        if name not in resolved_names:
            continue
        if name in dev_names and name not in prod_names:
            continue
        if not isinstance(raw_config, dict):
            raise ValueError(f"Cannot pack MCP server {name!r}: resolved config is not an object")

        transport = str(raw_config.get("transport") or raw_config.get("type") or "").lower()
        if transport == "http":
            transport = "streamable-http"
        if transport == "stdio":
            allowed = ("command", "args", "env", "cwd")
        elif transport in {"streamable-http", "sse"}:
            allowed = ("url", "headers")
        else:
            raise ValueError(
                f"Cannot pack MCP server {name!r}: transport {transport or '<missing>'!r} "
                "is not representable by Agent Plugins v1. Use a portable stdio, "
                "streamable-http, or sse config, or run 'apm pack --claude-plugin'."
            )

        projected = {"type": transport}
        projected.update(
            {key: raw_config[key] for key in allowed if raw_config.get(key) is not None}
        )
        if (
            contains_literal_secret_fields(projected.get("env"))
            or contains_literal_secret_fields(projected.get("headers"))
            or url_contains_literal_secret(projected.get("url"))
            or args_contain_literal_secret(projected.get("args"))
        ):
            raise ValueError(
                f"Cannot pack MCP server {name!r}: secret-shaped environment, header, URL, "
                "or argument values must use ${VAR} references. Replace the literal secret or run "
                "'apm pack --claude-plugin'."
            )
        servers[str(name)] = projected

    return {"$schema": MCP_SCHEMA_ID, "mcpServers": servers}


def _expected_skill_assets(output_files: set[str]) -> dict[str, set[str]]:
    """Return exact portable skill assets grouped by immediate directory."""
    expected: dict[str, set[str]] = {}
    for output_file in output_files:
        parts = Path(output_file).parts
        if len(parts) >= 3 and parts[0] == "skills":
            expected.setdefault(parts[1], set()).add(output_file)
    return expected


def _asset_paths(assets: tuple[AgentPluginAsset, ...]) -> set[str]:
    """Return canonical paths from an immutable loader-owned inventory."""
    return {asset.path for asset in assets}


def _validate_executable_facts(
    executables: tuple[AgentPluginExecutable, ...],
    *,
    declaration_path: Path,
    expected_output_files: set[str],
    component: str,
) -> None:
    """Require referenced executable facts to remain tied to generated assets."""
    for executable in executables:
        if executable.provenance.path != declaration_path:
            raise ValueError(
                f"Generated Agent Plugin {component} executable provenance changed during "
                f"canonical reload: {executable.provenance.path}"
            )
        if executable.plugin_relative_path is None:
            if executable.asset is not None:
                raise ValueError(
                    f"Generated Agent Plugin {component} external executable unexpectedly "
                    "resolved to a package asset"
                )
            continue
        if (
            executable.asset is None
            or executable.asset.path != executable.plugin_relative_path
            or executable.asset.path not in expected_output_files
        ):
            raise ValueError(
                f"Generated Agent Plugin {component} executable asset changed during "
                f"canonical reload: {executable.plugin_relative_path}"
            )


def _validate_agent_plugin_round_trip(
    staged_bundle: Path,
    *,
    expected_name: str,
    expected_version: str,
    expected_output_files: set[str],
    expected_mcp_names: set[str],
) -> None:
    """Reload staged output through the canonical Agent Plugin interpreter."""
    plugin = load_agent_plugin(staged_bundle)
    errors = [
        diagnostic.message
        for diagnostic in plugin.diagnostics
        if diagnostic.severity is DiagnosticSeverity.ERROR
    ]
    if errors:
        raise ValueError("Generated Agent Plugin failed canonical reload: " + "; ".join(errors))
    if plugin.identity.name != expected_name or plugin.identity.version != expected_version:
        raise ValueError(
            "Generated Agent Plugin identity changed during canonical reload: "
            f"expected {expected_name}@{expected_version}, got "
            f"{plugin.identity.name}@{plugin.identity.version or '<missing>'}"
        )
    expected_skills = _expected_skill_assets(expected_output_files)
    loaded_skills = {
        skill.directory_name: _asset_paths(skill.assets) for skill in plugin.components.skills
    }
    if loaded_skills != expected_skills:
        raise ValueError(
            "Generated Agent Plugin skills changed during canonical reload: "
            f"expected {sorted(expected_skills)}, got {sorted(loaded_skills)}"
        )
    loaded_mcp = {server.name for server in plugin.components.mcp_servers}
    if loaded_mcp != expected_mcp_names:
        raise ValueError(
            "Generated Agent Plugin MCP servers changed during canonical reload: "
            f"expected {sorted(expected_mcp_names)}, got {sorted(loaded_mcp)}"
        )
    for server in plugin.components.mcp_servers:
        _validate_executable_facts(
            server.executables,
            declaration_path=plugin.root / "mcp.json",
            expected_output_files=expected_output_files,
            component=f"MCP server {server.name!r}",
        )


def export_agent_plugin_bundle(
    project_root: Path,
    output_dir: Path,
    target: str | None = None,
    archive: bool = False,
    archive_format: str = "zip",
    dry_run: bool = False,
    force: bool = False,
    logger=None,
) -> PackResult:
    """Export the project as an Agent Plugin bundle."""
    migrate_lockfile_if_needed(project_root)
    lockfile_path = get_lockfile_path(project_root)
    lockfile = LockFile.read(lockfile_path)
    if lockfile is None:
        raise FileNotFoundError(
            "apm.lock.yaml not found -- run 'apm install' first to resolve dependencies."
        )

    apm_yml_path = project_root / "apm.yml"
    package = APMPackage.from_apm_yml(apm_yml_path)
    pkg_name = package.name
    pkg_version = package.version or "0.0.0"

    for dep_ref in package.get_apm_dependencies():
        if dep_ref.is_local:
            raise ValueError(
                "Cannot pack -- apm.yml contains local path dependency: "
                f"{dep_ref.local_path}\n"
                "Local dependencies are for development only. Replace them with "
                "remote references (e.g., 'owner/repo') before packing."
            )

    plugin_json = {"$schema": PLUGIN_SCHEMA_ID}
    plugin_json.update(synthesize_plugin_json_from_apm_yml(apm_yml_path))

    warnings: list[str] = []
    transition_warning = agent_plugin_warning()
    if transition_warning:
        warnings.append(transition_warning)

    dev_dep_urls = _get_dev_dependency_urls(apm_yml_path)
    file_map: dict[str, tuple[Path, str]] = {}
    collisions: list[str] = []
    dropped_surfaces: set[str] = set()
    mcp_document = _agent_mcp_document(package, lockfile)
    apm_modules_dir = project_root / "apm_modules"

    if lockfile:
        for dep in lockfile.get_all_dependencies():
            if (
                getattr(dep, "is_dev", False)
                or (
                    dep.repo_url,
                    getattr(dep, "virtual_path", "") or "",
                )
                in dev_dep_urls
            ):
                continue

            dep_name = dep.repo_url
            install_path = _dep_install_path(dep, apm_modules_dir)
            if dep.deployed_files:
                dep_components, dep_dropped = _portable_components(
                    _collect_deployed_components(project_root, dep)
                )
                dropped_surfaces |= dep_dropped
                if dep.skill_subset and not dep_components:
                    declared_skills = ", ".join(dep.skill_subset)
                    raise ValueError(
                        f"Cannot pack dependency {dep.repo_url}: the skills recorded "
                        f"in apm.lock.yaml (skill_subset: {declared_skills}) were not "
                        "found among its installed files. Run 'apm install' to "
                        "re-deploy the expected skills, then pack again."
                    )
            elif _cache_would_contribute_primitives(install_path, dep):
                raise ValueError(
                    f"Cannot pack dependency {dep.repo_url}: the lockfile records no "
                    "deployed files for it, but installed content that cannot be "
                    "verified exists in the apm_modules cache (a stale or partial "
                    "install). Run 'apm install' to record provenance in apm.lock.yaml, "
                    "then pack again."
                )
            else:
                dep_components = []

            if _cache_would_contribute_hooks_or_mcp(install_path):
                _warn = (
                    f"dependency {dep.repo_url} contributed hooks/MCP config that is "
                    "not attested in apm.lock.yaml; it will NOT be packed. "
                    "Attested primitives (skills/agents/etc.) are unaffected."
                )
                if logger:
                    logger.warning(_warn)
                else:
                    _rich_warning(_warn, symbol="warning")

            _merge_file_map(file_map, dep_components, dep_name, force, collisions)

    own_apm_dir = project_root / ".apm"
    if isinstance(package.includes, list):
        own_components, _root_hooks, root_hooks_present = _collect_explicit_local_components(
            project_root, package.includes
        )
        own_components, own_dropped = _portable_components(own_components)
        dropped_surfaces |= own_dropped
        if root_hooks_present:
            dropped_surfaces.add("hooks")
    else:
        own_components, own_dropped = _portable_components(_collect_apm_components(own_apm_dir))
        dropped_surfaces |= own_dropped
        if _apm_hooks_present(own_apm_dir):
            dropped_surfaces.add("hooks")
        if own_apm_dir.is_dir():
            _warn_skipped_root_components(project_root, logger)
        else:
            root_components, root_dropped = _portable_components(
                _collect_root_plugin_components(project_root)
            )
            own_components.extend(root_components)
            dropped_surfaces |= root_dropped
            if _root_hooks_present(project_root):
                dropped_surfaces.add("hooks")

    if (
        not isinstance(package.includes, list)
        and own_apm_dir.is_dir()
        and not own_components
        and not _apm_hooks_present(own_apm_dir)
    ):
        _warn_no_local_primitives(logger)

    _merge_file_map(file_map, own_components, pkg_name, force, collisions)

    if lockfile.lsp_servers or lockfile.lsp_configs:
        dropped_surfaces.add("lsp")

    _require_portable_agent_plugin(dropped_surfaces)

    for msg in collisions:
        if logger:
            logger.warning(msg)
        else:
            _rich_warning(msg)

    output_files = sorted(file_map.keys())
    output_files.append("mcp.json")
    output_files.append("plugin.json")
    output_files.extend(
        name
        for name in ("README.md", "LICENSE", "CHANGELOG.md", "CHANGELOG")
        if (project_root / name).is_file() and not (project_root / name).is_symlink()
    )
    output_files.append("apm.lock.yaml")
    output_files.sort()

    safe_name = _sanitize_bundle_name(pkg_name)
    safe_version = _sanitize_bundle_name(pkg_version)
    bundle_dir = output_dir / f"{safe_name}-{safe_version}"
    ensure_path_within(bundle_dir, output_dir)

    if dry_run:
        bundle_path = (
            projected_archive_path(output_dir, bundle_dir.name, archive_format)
            if archive
            else bundle_dir
        )
        return PackResult(bundle_path=bundle_path, files=output_files, warnings=warnings)

    if archive:
        validate_archive_format(archive_format)
    output_dir.mkdir(parents=True, exist_ok=True)
    _scan_bundle_sources(file_map, logger)
    with tempfile.TemporaryDirectory(prefix=".apm-agent-plugin-", dir=output_dir) as temp_dir:
        staging_root = Path(temp_dir)
        staged_bundle = staging_root / bundle_dir.name
        ensure_path_within(staged_bundle, staging_root)
        _write_bundle_sources(file_map, staged_bundle, staging_root)

        mcp_path = staged_bundle / "mcp.json"
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text(
            json.dumps(mcp_document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        (staged_bundle / "plugin.json").write_text(
            json.dumps(plugin_json, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        for doc_name in ("README.md", "LICENSE", "CHANGELOG.md", "CHANGELOG"):
            _copy_optional_root_file(project_root, staged_bundle, doc_name)

        bundle_files: dict[str, str] = {}
        for fp in staged_bundle.rglob("*"):
            if not fp.is_file() or fp.is_symlink():
                continue
            rel = fp.relative_to(staged_bundle).as_posix()
            if rel == "apm.lock.yaml":
                continue
            bundle_files[rel] = hashlib.sha256(fp.read_bytes()).hexdigest()

        enriched_yaml = enrich_lockfile_for_pack(
            lockfile,
            BundleFormat.AGENT_PLUGIN.lock_value,
            target or "all",
            bundle_files=bundle_files,
            packed_at=lockfile.generated_at,
        )
        (staged_bundle / "apm.lock.yaml").write_text(enriched_yaml, encoding="utf-8")

        _validate_agent_plugin_round_trip(
            staged_bundle,
            expected_name=pkg_name,
            expected_version=pkg_version,
            expected_output_files=set(output_files),
            expected_mcp_names=set(mcp_document["mcpServers"]),
        )

        if archive:
            archive_path = projected_archive_path(output_dir, bundle_dir.name, archive_format)
            staged_archive = staging_root / archive_path.name
            write_reproducible_archive(staged_bundle, staged_archive, archive_format)
            os.replace(staged_archive, archive_path)
            committed_path = archive_path
        else:
            if bundle_dir.exists():
                safe_rmtree(bundle_dir, output_dir)
            os.replace(staged_bundle, bundle_dir)
            committed_path = bundle_dir

    return PackResult(bundle_path=committed_path, files=output_files, warnings=warnings)
