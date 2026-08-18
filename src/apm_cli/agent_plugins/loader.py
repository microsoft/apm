"""Canonical version-aware interpretation owner for Agent Plugins."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from .constants import (
    AGENT_PLUGINS_SCHEMA_PREFIX,
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
    UnsupportedAgentPluginVersionError,
)
from .io import read_json_document
from .ir import (
    AgentPlugin,
    AgentPluginComponents,
    AgentPluginDetection,
    AgentPluginDiagnostic,
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
from .validation import validate_mcp_config_file, validate_plugin_manifest_document

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


@dataclass(frozen=True, slots=True)
class _AdmissibleRootManifest:
    path: Path
    document: Mapping[str, Any]


def detect_agent_plugin(package_root: Path) -> AgentPluginDetection | None:
    """Classify and interpret a native Agent Plugin from its exact root manifest."""
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
    if "$schema" not in document:
        return None
    schema_id = document["$schema"]
    if not isinstance(schema_id, str):
        return _rejected_manifest_detection(
            manifest_path,
            "Invalid root plugin.json: $schema must be a string",
        )
    if not schema_id.startswith(AGENT_PLUGINS_SCHEMA_PREFIX):
        return None
    try:
        loader = _VERSION_LOADERS.get(schema_id)
        if loader is None:
            raise UnsupportedAgentPluginVersionError(
                f"Unsupported Agent Plugins manifest schema: {schema_id}"
            )
        plugin = loader(package_root, manifest_path, document)
    except AgentPluginError as exc:
        return AgentPluginDetection(
            manifest_path=manifest_path,
            schema_id=schema_id,
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
    try:
        evidence = _read_admissible_root_manifest(package_root)
    except AgentPluginManifestError as exc:
        raise AgentPluginLegacyBoundaryError(
            f"Present root plugin.json cannot enter Claude plugin normalization: {exc}"
        ) from exc
    if evidence is None:
        return None
    schema_id = evidence.document.get("$schema")
    if isinstance(schema_id, str) and schema_id.startswith(AGENT_PLUGINS_SCHEMA_PREFIX):
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

    identity = _identity_from_manifest(manifest)
    apm_configuration, authority_diagnostics = _load_apm_configuration(
        root,
        identity=identity,
        manifest=manifest,
    )
    diagnostics.extend(authority_diagnostics)

    skills, skill_diagnostics = _discover_skills(root)
    diagnostics.extend(skill_diagnostics)
    mcp_servers, mcp_diagnostics = _discover_mcp_servers(root)
    diagnostics.extend(mcp_diagnostics)

    return AgentPlugin(
        specification_version=AGENT_PLUGINS_VERSION,
        root=root,
        manifest=SourceProvenance(path=manifest_path, json_pointer=""),
        identity=identity,
        components=AgentPluginComponents(skills=skills, mcp_servers=mcp_servers),
        apm_extension=_apm_extension_from_manifest(manifest, manifest_path),
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
    *,
    identity: AgentPluginIdentity,
    manifest: dict[str, Any],
) -> tuple[ApmConfiguration | None, list[AgentPluginDiagnostic]]:
    apm_yml_path = root / "apm.yml"
    if not apm_yml_path.exists():
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
) -> tuple[tuple[AgentPluginSkill, ...], list[AgentPluginDiagnostic]]:
    skills_path = root / "skills"
    if not _has_exact_entry(root, "skills"):
        return (), []
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
    for child in sorted(skills_path.iterdir(), key=lambda item: item.name):
        if child.is_symlink() or not child.is_dir():
            continue
        skill_manifest = child / "SKILL.md"
        if not _has_exact_entry(child, "SKILL.md"):
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
        if skill_manifest.is_symlink() or not skill_manifest.is_file():
            diagnostics.append(
                _diagnostic(
                    code="skill.manifest.invalid",
                    severity=DiagnosticSeverity.ERROR,
                    message=f"Skill {child.name} has a non-regular SKILL.md and was skipped",
                    root=root,
                    path=skill_manifest,
                    component=f"skill:{child.name}",
                )
            )
            continue
        try:
            parsed = parse_skill_file(skill_manifest)
            errors = parsed.validate()
        except ValueError as exc:
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
            )
        )
    return tuple(skills), diagnostics


def _discover_mcp_servers(
    root: Path,
) -> tuple[tuple[AgentPluginMcpServer, ...], list[AgentPluginDiagnostic]]:
    mcp_path = root / "mcp.json"
    if not _has_exact_entry(root, "mcp.json"):
        return (), []
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
    servers = tuple(
        _mcp_server_from_normalized(name, raw_servers[name], mcp_path)
        for name in sorted(raw_servers)
    )
    return servers, diagnostics


def _mcp_server_from_normalized(
    name: str,
    config: dict[str, Any],
    mcp_path: Path,
) -> AgentPluginMcpServer:
    server_type = McpServerType(config["type"])
    return AgentPluginMcpServer(
        name=name,
        server_type=server_type,
        command=config.get("command"),
        args=tuple(config.get("args", ())),
        env=tuple(sorted(config.get("env", {}).items())),
        cwd=config.get("cwd"),
        url=config.get("url"),
        headers=tuple(sorted(config.get("headers", {}).items())),
        provenance=SourceProvenance(
            path=mcp_path,
            json_pointer=f"/mcpServers/{_escape_json_pointer(name)}",
        ),
    )


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


def _has_exact_entry(parent: Path, name: str) -> bool:
    try:
        return any(entry.name == name for entry in parent.iterdir())
    except OSError:
        return False


_VERSION_LOADERS: dict[str, Callable[[Path, Path, dict[str, Any]], AgentPlugin]] = {
    PLUGIN_SCHEMA_ID: _load_v1
}
