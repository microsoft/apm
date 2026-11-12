"""Canonical version-aware interpretation owner for Agent Plugins."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

import yaml

from ..utils.path_security import PathTraversalError, ensure_path_within, validate_path_segments
from .assets import AssetInventory, AssetInventoryError, normalized_path_key
from .constants import (
    AGENT_PLUGINS_VERSION,
    COM_MICROSOFT_APM_NAMESPACE,
    PLUGIN_SCHEMA_ID,
)
from .errors import (
    AgentPluginError,
    AgentPluginLegacyBoundaryError,
    AgentPluginManifestAuthorityError,
    AgentPluginManifestError,
    NotAgentPluginError,
)
from .io import read_json_document
from .ir import (
    AgentPlugin,
    AgentPluginComponents,
    AgentPluginDetection,
    AgentPluginDiagnostic,
    AgentPluginExecutable,
    AgentPluginIdentity,
    AgentPluginMcpServer,
    AgentPluginSkill,
    ApmConfiguration,
    ApmExtensionData,
    DiagnosticSeverity,
    FrozenJsonArray,
    FrozenJsonObject,
    FrozenJsonValue,
    McpServerType,
    SourceProvenance,
)
from .validation import (
    validate_mcp_config_file,
    validate_plugin_manifest_document,
)

_PORTABLE_IDENTITY_FIELDS = frozenset(
    {
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
    }
)
_APM_CONFIGURATION_FIELDS = frozenset(
    {
        "allowExecutables",
        "build",
        "dependencies",
        "devDependencies",
        "includes",
        "manifestVersion",
        "policy",
        "registries",
        "schemaVersion",
        "scripts",
        "target",
        "targets",
        "type",
    }
)
_REJECTED_MANIFEST_SCHEMA_ID = "<rejected-root-plugin-json>"
_IGNORED_PORTABLE_COMPONENT_PATHS = (
    "agents",
    "commands",
    "hooks",
    "instructions",
    "extensions",
    "lsp.json",
)


@dataclass(frozen=True, slots=True)
class _AdmissibleRootManifest:
    path: Path
    document: Mapping[str, Any]


class _CandidateDisposition(Enum):
    ABSENT = "absent"
    SAFE = "safe"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class _CandidateResolution:
    path: Path
    disposition: _CandidateDisposition
    rejection: str | None = None


def detect_agent_plugin(package_root: Path) -> AgentPluginDetection | None:
    """Classify and interpret a native Agent Plugin from its exact root manifest."""
    from ..install.primitive_classification import PluginSchemaRoute, classify_plugin_manifest

    manifest_path = package_root / "plugin.json"
    try:
        evidence = _read_admissible_root_manifest(package_root)
    except AgentPluginManifestError as exc:
        return _rejected_manifest_detection(
            manifest_path,
            str(exc),
        )
    if evidence is None:
        return None
    document = dict(evidence.document)
    try:
        classification = classify_plugin_manifest(document)
        if classification.route is PluginSchemaRoute.LEGACY:
            return None
        schema_id = document["$schema"]
        loader = _VERSION_LOADERS[schema_id]
        plugin = loader(package_root, manifest_path, document)
    except AgentPluginError as exc:
        return AgentPluginDetection(
            manifest_path=manifest_path,
            schema_id=str(document.get("$schema", _REJECTED_MANIFEST_SCHEMA_ID)),
            error=exc,
        )
    return AgentPluginDetection(
        manifest_path=manifest_path,
        schema_id=schema_id,
        plugin=plugin,
    )


def _rejected_manifest_detection(
    manifest_path: Path,
    message: str,
) -> AgentPluginDetection:
    return AgentPluginDetection(
        manifest_path=manifest_path,
        schema_id=_REJECTED_MANIFEST_SCHEMA_ID,
        error=AgentPluginManifestError(message),
    )


def load_agent_plugin(package_root: Path) -> AgentPlugin:
    """Load one native Agent Plugin or raise a typed fail-closed error."""
    detection = detect_agent_plugin(package_root)
    if detection is None:
        raise NotAgentPluginError(
            f"{package_root} does not contain a root plugin.json selecting Agent Plugins"
        )
    if detection.error is not None:
        raise detection.error
    if detection.plugin is None:
        raise AgentPluginManifestError("Agent Plugin detection produced no contract IR")
    return detection.plugin


def reject_agent_plugin_legacy_normalization(package_root: Path) -> None:
    """Prevent native Agent Plugin input from entering Claude normalization."""
    admit_legacy_plugin_manifest(package_root)


def admit_legacy_plugin_manifest(package_root: Path) -> dict[str, Any] | None:
    """Return one admissible schema-less legacy manifest or reject fallback."""
    from ..install.primitive_classification import PluginSchemaRoute, classify_plugin_manifest

    try:
        evidence = _read_admissible_root_manifest(package_root)
    except AgentPluginManifestError as exc:
        raise AgentPluginLegacyBoundaryError(
            f"Present root plugin.json cannot enter Claude plugin normalization: {exc}"
        ) from exc
    if evidence is None:
        return None
    try:
        classification = classify_plugin_manifest(evidence.document)
    except AgentPluginError as exc:
        raise AgentPluginLegacyBoundaryError(
            f"Schema-bearing plugin.json cannot enter Claude plugin normalization: {exc}"
        ) from exc
    if classification.route is PluginSchemaRoute.AGENT_PLUGIN:
        raise AgentPluginLegacyBoundaryError(
            "Agent Plugin input must be interpreted by load_agent_plugin(), "
            "not Claude plugin normalization"
        )
    return dict(evidence.document)


def _read_admissible_root_manifest(package_root: Path) -> _AdmissibleRootManifest | None:
    manifest_path = package_root / "plugin.json"
    try:
        manifest_present = any(entry.name == "plugin.json" for entry in package_root.iterdir())
    except OSError as exc:
        raise AgentPluginManifestError(
            f"Root plugin.json presence could not be determined: {exc}"
        ) from exc
    if not manifest_present:
        return None
    if normalized_path_key("plugin.json") in _case_ambiguous_names(package_root):
        raise AgentPluginManifestError("Root plugin.json is case-ambiguous")
    try:
        document = read_json_document(manifest_path, reject_duplicate_schema=True)
    except (OSError, ValueError) as exc:
        raise AgentPluginManifestError(f"Invalid root plugin.json: {exc}") from exc
    if not isinstance(document, dict):
        raise AgentPluginManifestError("Invalid root plugin.json: manifest must be a JSON object")
    schema_id = document.get("$schema")
    if "$schema" in document and not isinstance(schema_id, str):
        raise AgentPluginManifestError("Invalid root plugin.json: $schema must be a string")
    return _AdmissibleRootManifest(
        path=manifest_path,
        document=MappingProxyType(document),
    )


def _load_v1(
    package_root: Path,
    manifest_path: Path,
    document: dict[str, Any],
) -> AgentPlugin:
    root = package_root.resolve()
    validation = validate_plugin_manifest_document(document)
    if not validation.is_valid or validation.normalized is None:
        raise AgentPluginManifestError("; ".join(validation.errors))
    manifest = validation.normalized
    diagnostics = [
        _diagnostic(
            code="manifest.field.ignored",
            severity=DiagnosticSeverity.WARNING,
            message=warning,
            root=root,
            path=manifest_path,
            component="manifest",
        )
        for warning in sorted(validation.warnings)
    ]

    asset_inventory = AssetInventory(root)
    try:
        root_entries = asset_inventory.list_component_candidates(root)
    except (AssetInventoryError, OSError) as exc:
        diagnostics.append(
            _diagnostic(
                code="assets.package.invalid",
                severity=DiagnosticSeverity.ERROR,
                message=f"Agent Plugin component discovery was disabled: {exc}",
                root=root,
                path=root,
                component="components",
            )
        )
        root_entries = ()
        inventory_available = False
    else:
        inventory_available = True

    identity = _identity_from_manifest(manifest)
    apm_configuration = None
    if inventory_available:
        apm_configuration, authority_diagnostics = _load_apm_configuration(
            root,
            root_entries,
            identity=identity,
            manifest=manifest,
        )
        diagnostics.extend(authority_diagnostics)

    skills, skill_diagnostics = _discover_skills(root, root_entries, asset_inventory)
    diagnostics.extend(skill_diagnostics)
    mcp_servers, mcp_diagnostics = _discover_mcp_servers(root, root_entries, asset_inventory)
    diagnostics.extend(mcp_diagnostics)
    apm_extension = _apm_extension_from_manifest(manifest, manifest_path)
    diagnostics.extend(_ignored_portable_component_diagnostics(root, root_entries))

    return AgentPlugin(
        specification_version=AGENT_PLUGINS_VERSION,
        root=root,
        manifest=SourceProvenance(path=manifest_path, json_pointer=""),
        identity=identity,
        components=AgentPluginComponents(skills=skills, mcp_servers=mcp_servers),
        apm_extension=apm_extension,
        apm_configuration=apm_configuration,
        diagnostics=tuple(
            sorted(
                diagnostics,
                key=lambda item: (item.path, item.component or "", item.code, item.message),
            )
        ),
    )


def _identity_from_manifest(manifest: dict[str, Any]) -> AgentPluginIdentity:
    author = manifest.get("author")
    author_items = (
        tuple(sorted((str(key), str(value)) for key, value in author.items()))
        if isinstance(author, dict)
        else ()
    )
    keywords = manifest.get("keywords")
    return AgentPluginIdentity(
        name=manifest["name"],
        version=manifest.get("version"),
        description=manifest.get("description"),
        author=author_items,
        homepage=manifest.get("homepage"),
        repository=manifest.get("repository"),
        license=manifest.get("license"),
        keywords=tuple(keywords) if isinstance(keywords, list) else (),
    )


def _apm_extension_from_manifest(
    manifest: dict[str, Any],
    manifest_path: Path,
) -> ApmExtensionData | None:
    extensions = manifest.get("extensions")
    if not isinstance(extensions, dict):
        return None
    payload = extensions.get(COM_MICROSOFT_APM_NAMESPACE)
    if not isinstance(payload, dict):
        return None
    schema_version = payload.get("schemaVersion")
    if not isinstance(schema_version, str):
        return None
    return ApmExtensionData(
        schema_version=schema_version,
        values=_freeze_object(payload),
        provenance=SourceProvenance(
            path=manifest_path,
            json_pointer="/extensions/com.microsoft.apm",
        ),
    )


def _load_apm_configuration(
    root: Path,
    root_entries: tuple[Path, ...],
    *,
    identity: AgentPluginIdentity,
    manifest: dict[str, Any],
) -> tuple[ApmConfiguration | None, list[AgentPluginDiagnostic]]:
    apm_yml_path = root / "apm.yml"
    if not _has_exact_entry(root_entries, "apm.yml"):
        return None, []
    if not apm_yml_path.is_file() or apm_yml_path.is_symlink():
        raise AgentPluginManifestAuthorityError("Agent Plugin apm.yml must be a regular file")

    from ..utils.yaml_io import load_yaml

    try:
        document = load_yaml(apm_yml_path)
    except (OSError, yaml.YAMLError) as exc:
        raise AgentPluginManifestAuthorityError(f"Invalid Agent Plugin apm.yml: {exc}") from exc
    if not isinstance(document, dict):
        raise AgentPluginManifestAuthorityError("Agent Plugin apm.yml must contain a YAML object")
    if not all(isinstance(key, str) for key in document):
        raise AgentPluginManifestAuthorityError("Agent Plugin apm.yml keys must be strings")

    conflicts = _identity_conflicts(document, identity=identity, manifest=manifest)
    if conflicts:
        raise AgentPluginManifestAuthorityError(
            "Agent Plugin portable identity is owned by plugin.json; conflicting apm.yml fields: "
            + ", ".join(conflicts)
        )

    unsupported = sorted(set(document) - _PORTABLE_IDENTITY_FIELDS - _APM_CONFIGURATION_FIELDS)
    if unsupported:
        raise AgentPluginManifestAuthorityError(
            "Agent Plugin apm.yml may contain only APM dependency, policy, and build "
            "configuration; unsupported fields: " + ", ".join(str(field) for field in unsupported)
        )

    config = {
        str(key): value for key, value in document.items() if key in _APM_CONFIGURATION_FIELDS
    }
    duplicated = sorted(str(key) for key in document if key in _PORTABLE_IDENTITY_FIELDS)
    diagnostics = []
    if duplicated:
        diagnostics.append(
            _diagnostic(
                code="manifest.apm_identity.ignored",
                severity=DiagnosticSeverity.WARNING,
                message=(
                    "Portable identity from apm.yml was ignored; plugin.json is authoritative: "
                    + ", ".join(duplicated)
                ),
                root=root,
                path=apm_yml_path,
                component="apm",
            )
        )
    if not config:
        return None, diagnostics
    return (
        ApmConfiguration(values=_freeze_object(config), provenance=apm_yml_path),
        diagnostics,
    )


def _identity_conflicts(
    apm_document: dict[str, Any],
    *,
    identity: AgentPluginIdentity,
    manifest: dict[str, Any],
) -> list[str]:
    conflicts: list[str] = []
    for field in sorted(_PORTABLE_IDENTITY_FIELDS):
        if field not in apm_document or field not in manifest:
            continue
        apm_value = apm_document[field]
        plugin_value = manifest[field]
        if field == "author" and isinstance(apm_value, str):
            apm_value = {"name": apm_value}
        if field == "keywords" and isinstance(apm_value, tuple):
            apm_value = list(apm_value)
        if apm_value != plugin_value:
            conflicts.append(field)
    if apm_document.get("name") not in (None, identity.name):
        conflicts.append("name")
    return sorted(set(conflicts))


def _discover_skills(
    root: Path,
    root_entries: tuple[Path, ...],
    asset_inventory: AssetInventory,
) -> tuple[tuple[AgentPluginSkill, ...], list[AgentPluginDiagnostic]]:
    skills_path = root / "skills"
    if not _has_exact_entry(root_entries, "skills"):
        return (), []
    if normalized_path_key("skills") in _case_ambiguous_entries(root_entries):
        return (), [
            _diagnostic(
                code="skills.location.ambiguous",
                severity=DiagnosticSeverity.ERROR,
                message="Agent Plugins root skills directory is case-ambiguous",
                root=root,
                path=skills_path,
                component="skills",
            )
        ]
    if skills_path.is_symlink() or not skills_path.is_dir():
        return (), [
            _diagnostic(
                code="skills.location.invalid",
                severity=DiagnosticSeverity.ERROR,
                message="Agent Plugins skills must be a regular root directory",
                root=root,
                path=skills_path,
                component="skills",
            )
        ]

    from ..primitives.parser import parse_skill_file

    skills: list[AgentPluginSkill] = []
    diagnostics: list[AgentPluginDiagnostic] = []
    try:
        skill_entries = asset_inventory.list_component_candidates(skills_path)
    except (AssetInventoryError, OSError) as exc:
        return (), [
            _diagnostic(
                code="skills.assets.invalid",
                severity=DiagnosticSeverity.ERROR,
                message=f"Agent Plugins skills were disabled: {exc}",
                root=root,
                path=skills_path,
                component="skills",
            )
        ]
    ambiguous_names = _case_ambiguous_entries(skill_entries)
    for child in skill_entries:
        if normalized_path_key(child.name) in ambiguous_names:
            diagnostics.append(
                _diagnostic(
                    code="skill.path.ambiguous",
                    severity=DiagnosticSeverity.ERROR,
                    message=f"Skill directory {child.name} is case-ambiguous and was skipped",
                    root=root,
                    path=child,
                    component=f"skill:{child.name}",
                )
            )
            continue
        if child.is_symlink() or not child.is_dir():
            diagnostics.append(
                _diagnostic(
                    code="skill.location.invalid",
                    severity=DiagnosticSeverity.ERROR,
                    message=f"Skill entry {child.name} is not a regular directory and was skipped",
                    root=root,
                    path=child,
                    component=f"skill:{child.name}",
                )
            )
            continue
        try:
            assets = asset_inventory.collect_component(child)
        except (AssetInventoryError, OSError) as exc:
            diagnostics.append(
                _diagnostic(
                    code="skill.assets.invalid",
                    severity=DiagnosticSeverity.ERROR,
                    message=f"Skill {child.name} was skipped: {exc}",
                    root=root,
                    path=child,
                    component=f"skill:{child.name}",
                )
            )
            continue
        skill_manifest = child / "SKILL.md"
        manifest_relative = f"skills/{child.name}/SKILL.md"
        manifest_asset = next((asset for asset in assets if asset.path == manifest_relative), None)
        if manifest_asset is None:
            diagnostics.append(
                _diagnostic(
                    code="skill.manifest.missing",
                    severity=DiagnosticSeverity.ERROR,
                    message=f"Skill directory {child.name} has no exact regular SKILL.md and was skipped",
                    root=root,
                    path=child,
                    component=f"skill:{child.name}",
                )
            )
            continue
        try:
            parsed = parse_skill_file(skill_manifest)
            errors = parsed.validate()
            with asset_inventory.open_verified_asset(manifest_asset):
                pass
        except (AssetInventoryError, ValueError) as exc:
            errors = [str(exc)]
            parsed = None
        if errors or parsed is None:
            diagnostics.append(
                _diagnostic(
                    code="skill.invalid",
                    severity=DiagnosticSeverity.ERROR,
                    message=f"Skill {child.name} was skipped: {'; '.join(errors)}",
                    root=root,
                    path=skill_manifest,
                    component=f"skill:{child.name}",
                )
            )
            continue
        skills.append(
            AgentPluginSkill(
                directory_name=child.name,
                name=parsed.name,
                description=parsed.description,
                root=child,
                manifest=SourceProvenance(path=skill_manifest, json_pointer=""),
                assets=assets,
            )
        )
    return tuple(skills), diagnostics


def _discover_mcp_servers(
    root: Path,
    root_entries: tuple[Path, ...],
    asset_inventory: AssetInventory,
) -> tuple[tuple[AgentPluginMcpServer, ...], list[AgentPluginDiagnostic]]:
    mcp_path = root / "mcp.json"
    if not _has_exact_entry(root_entries, "mcp.json"):
        return (), []
    if normalized_path_key("mcp.json") in _case_ambiguous_entries(root_entries):
        return (), [
            _diagnostic(
                code="mcp.location.ambiguous",
                severity=DiagnosticSeverity.ERROR,
                message="Agent Plugins root mcp.json is case-ambiguous",
                root=root,
                path=mcp_path,
                component="mcp",
            )
        ]
    if mcp_path.is_symlink() or not mcp_path.is_file():
        return (), [
            _diagnostic(
                code="mcp.location.invalid",
                severity=DiagnosticSeverity.ERROR,
                message="Agent Plugins MCP configuration must be root mcp.json as a regular file",
                root=root,
                path=mcp_path,
                component="mcp",
            )
        ]

    try:
        validation = validate_mcp_config_file(
            mcp_path,
            expected_plugin_schema_id=PLUGIN_SCHEMA_ID,
            isolate_invalid_servers=True,
            plugin_root=root,
        )
    except (OSError, ValueError) as exc:
        return (), [
            _diagnostic(
                code="mcp.document.invalid",
                severity=DiagnosticSeverity.ERROR,
                message=f"Agent Plugins mcp.json was disabled: {exc}",
                root=root,
                path=mcp_path,
                component="mcp",
            )
        ]
    if validation.errors or validation.normalized is None:
        return (), [
            _diagnostic(
                code="mcp.document.invalid",
                severity=DiagnosticSeverity.ERROR,
                message=f"Agent Plugins mcp.json was disabled: {error}",
                root=root,
                path=mcp_path,
                component="mcp",
            )
            for error in sorted(validation.errors)
        ]

    diagnostics = [
        _diagnostic(
            code="mcp.server.invalid",
            severity=DiagnosticSeverity.ERROR,
            message=warning,
            root=root,
            path=mcp_path,
            component="mcp",
        )
        for warning in sorted(validation.warnings)
    ]
    raw_servers = validation.normalized["mcpServers"]
    servers: list[AgentPluginMcpServer] = []
    for name in sorted(raw_servers):
        server, executable_diagnostics = _mcp_server_from_normalized(
            name,
            raw_servers[name],
            mcp_path,
            root,
            asset_inventory,
        )
        diagnostics.extend(executable_diagnostics)
        if server is not None:
            servers.append(server)
    return tuple(servers), diagnostics


def _mcp_server_from_normalized(
    name: str,
    config: dict[str, Any],
    mcp_path: Path,
    root: Path,
    asset_inventory: AssetInventory,
) -> tuple[AgentPluginMcpServer | None, list[AgentPluginDiagnostic]]:
    server_type = McpServerType(config["type"])
    provenance = SourceProvenance(
        path=mcp_path,
        json_pointer=f"/mcpServers/{_escape_json_pointer(name)}",
    )
    executables, diagnostics = _declaration_executables(
        root=root,
        asset_inventory=asset_inventory,
        declarations=(
            (
                config.get("command"),
                f"{provenance.json_pointer}/command",
                True,
            ),
            *(
                (
                    argument,
                    f"{provenance.json_pointer}/args/{index}",
                    False,
                )
                for index, argument in enumerate(config.get("args", ()))
            ),
        ),
        source_path=mcp_path,
        component=f"mcp:{name}",
        diagnostic_code="mcp.server.executable.invalid",
    )
    if any(diagnostic.severity is DiagnosticSeverity.ERROR for diagnostic in diagnostics):
        return None, diagnostics
    return (
        AgentPluginMcpServer(
            name=name,
            server_type=server_type,
            command=config.get("command"),
            args=tuple(config.get("args", ())),
            env=tuple(sorted(config.get("env", {}).items())),
            cwd=config.get("cwd"),
            url=config.get("url"),
            headers=tuple(sorted(config.get("headers", {}).items())),
            provenance=provenance,
            executables=executables,
        ),
        diagnostics,
    )


def _declaration_executables(
    *,
    root: Path,
    asset_inventory: AssetInventory,
    declarations: tuple[tuple[object, str, bool], ...],
    source_path: Path,
    component: str,
    diagnostic_code: str,
    relative_base: Path | None = None,
) -> tuple[tuple[AgentPluginExecutable, ...], list[AgentPluginDiagnostic]]:
    executables: list[AgentPluginExecutable] = []
    diagnostics: list[AgentPluginDiagnostic] = []
    for declaration, json_pointer, include_external in declarations:
        if not isinstance(declaration, str):
            continue
        relative = _plugin_relative_declaration_path(declaration)
        if relative is None:
            if include_external:
                executables.append(
                    AgentPluginExecutable(
                        declaration=declaration,
                        plugin_relative_path=None,
                        asset=None,
                        provenance=SourceProvenance(
                            path=source_path,
                            json_pointer=json_pointer,
                        ),
                    )
                )
            continue
        try:
            validate_path_segments(
                relative,
                context="Agent Plugin executable reference",
                reject_empty=True,
                allow_current_dir=True,
            )
        except PathTraversalError as exc:
            diagnostics.append(
                _diagnostic(
                    code=diagnostic_code,
                    severity=DiagnosticSeverity.ERROR,
                    message=f"Executable reference {declaration!r} was rejected: {exc}",
                    root=root,
                    path=source_path,
                    component=component,
                )
            )
            continue
        relative_path = Path(*PurePosixPath(relative).parts)
        if declaration.replace("\\", "/").startswith("./") and relative_base is not None:
            primary = _resolve_executable_candidate(relative_base / relative_path, root)
            resolution = (
                _resolve_executable_candidate(root / relative_path, root)
                if primary.disposition is _CandidateDisposition.ABSENT
                else primary
            )
        else:
            resolution = _resolve_executable_candidate(root / relative_path, root)
        if resolution.disposition is _CandidateDisposition.REJECTED:
            diagnostics.append(
                _diagnostic(
                    code=diagnostic_code,
                    severity=DiagnosticSeverity.ERROR,
                    message=(
                        f"Executable reference {declaration!r} was rejected: {resolution.rejection}"
                    ),
                    root=root,
                    path=source_path,
                    component=component,
                )
            )
            continue
        candidate = resolution.path
        relative = candidate.relative_to(root).as_posix()
        if resolution.disposition is _CandidateDisposition.ABSENT:
            executables.append(
                AgentPluginExecutable(
                    declaration=declaration,
                    plugin_relative_path=relative,
                    asset=None,
                    provenance=SourceProvenance(
                        path=source_path,
                        json_pointer=json_pointer,
                    ),
                )
            )
            diagnostics.append(
                _diagnostic(
                    code=diagnostic_code.replace(".invalid", ".missing"),
                    severity=DiagnosticSeverity.WARNING,
                    message=f"Executable reference {declaration!r} has no package asset",
                    root=root,
                    path=source_path,
                    component=component,
                )
            )
            continue
        try:
            asset = asset_inventory.collect_file(candidate)
        except (AssetInventoryError, OSError) as exc:
            diagnostics.append(
                _diagnostic(
                    code=diagnostic_code,
                    severity=DiagnosticSeverity.ERROR,
                    message=f"Executable reference {declaration!r} was rejected: {exc}",
                    root=root,
                    path=source_path,
                    component=component,
                )
            )
            continue
        executables.append(
            AgentPluginExecutable(
                declaration=declaration,
                plugin_relative_path=relative,
                asset=asset,
                provenance=SourceProvenance(
                    path=source_path,
                    json_pointer=json_pointer,
                ),
            )
        )
    return tuple(executables), diagnostics


def _resolve_executable_candidate(path: Path, root: Path) -> _CandidateResolution:
    """Classify one literal candidate without conflating absence and rejection."""
    try:
        ensure_path_within(path, root)
    except (OSError, PathTraversalError, RuntimeError) as exc:
        return _CandidateResolution(
            path=path,
            disposition=_CandidateDisposition.REJECTED,
            rejection=str(exc),
        )
    try:
        path.lstat()
    except FileNotFoundError:
        return _CandidateResolution(path=path, disposition=_CandidateDisposition.ABSENT)
    except OSError as exc:
        return _CandidateResolution(
            path=path,
            disposition=_CandidateDisposition.REJECTED,
            rejection=f"asset metadata is unreadable: {exc}",
        )
    return _CandidateResolution(path=path, disposition=_CandidateDisposition.SAFE)


def _plugin_relative_declaration_path(declaration: str) -> str | None:
    portable_declaration = declaration.replace("\\", "/")
    if portable_declaration.startswith("./"):
        relative = portable_declaration[2:]
    elif portable_declaration.startswith("${PLUGIN_ROOT}/"):
        relative = portable_declaration.removeprefix("${PLUGIN_ROOT}/")
    elif portable_declaration.startswith("../"):
        relative = portable_declaration
    else:
        return None
    portable = PurePosixPath(relative)
    if portable.is_absolute() or any(part in ("", ".", "..") for part in portable.parts):
        return relative
    return portable.as_posix()


def _ignored_portable_component_diagnostics(
    root: Path,
    root_entries: tuple[Path, ...],
) -> list[AgentPluginDiagnostic]:
    diagnostics: list[AgentPluginDiagnostic] = []
    for name in _IGNORED_PORTABLE_COMPONENT_PATHS:
        if not _has_exact_entry(root_entries, name):
            continue
        diagnostics.append(
            _diagnostic(
                code="portable.component.ignored",
                severity=DiagnosticSeverity.WARNING,
                message=(
                    f"Root {name} is not an Agent Plugins v1 portable component and was ignored"
                ),
                root=root,
                path=root / name,
                component="portable",
            )
        )
    return diagnostics


def _case_ambiguous_names(directory: Path) -> set[str]:
    try:
        return _case_ambiguous_entries(tuple(directory.iterdir()))
    except OSError:
        return set()


def _case_ambiguous_entries(entries: tuple[Path, ...]) -> set[str]:
    grouped: dict[str, list[str]] = {}
    for entry in entries:
        grouped.setdefault(normalized_path_key(entry.name), []).append(entry.name)
    return {key for key, names in grouped.items() if len(set(names)) > 1}


def _freeze_json(value: object) -> FrozenJsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return FrozenJsonArray(tuple(_freeze_json(item) for item in value))
    if isinstance(value, dict):
        return _freeze_object(value)
    raise AgentPluginManifestAuthorityError(
        f"APM configuration contains unsupported value type: {type(value).__name__}"
    )


def _freeze_object(value: dict[str, Any]) -> FrozenJsonObject:
    if not all(isinstance(key, str) for key in value):
        raise AgentPluginManifestAuthorityError("APM configuration object keys must be strings")
    return FrozenJsonObject(tuple(sorted((key, _freeze_json(item)) for key, item in value.items())))


def _diagnostic(
    *,
    code: str,
    severity: DiagnosticSeverity,
    message: str,
    root: Path,
    path: Path,
    component: str | None,
) -> AgentPluginDiagnostic:
    try:
        relative_path = path.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        relative_path = path.name
    return AgentPluginDiagnostic(
        code=code,
        severity=severity,
        message=message,
        path=relative_path,
        component=component,
    )


def _escape_json_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _has_exact_entry(entries: tuple[Path, ...], name: str) -> bool:
    return any(entry.name == name for entry in entries)


_VERSION_LOADERS: dict[str, Callable[[Path, Path, dict[str, Any]], AgentPlugin]] = {
    PLUGIN_SCHEMA_ID: _load_v1
}
