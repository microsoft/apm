"""YAML parser and validator for apm-policy.yml files."""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Any

import yaml

from ..mcp_transport import POLICY_TRANSPORTS, canonical_transport_names
from ..models.extension_keys import is_extension_key
from ..utils.yaml_io import load_yaml_str
from .schema import (
    ApmPolicy,
    AuditPolicy,
    BinDeployPolicy,
    CompilationPolicy,
    CompilationStrategyPolicy,
    CompilationTargetPolicy,
    DependencyPolicy,
    ExecutablesPolicy,
    IntegrityPolicy,
    ManifestPolicy,
    McpPolicy,
    McpTransportPolicy,
    PolicyCache,
    RegistrySourcePolicy,
    ScannerGovernance,
    SecurityPolicy,
    UnmanagedFilesPolicy,
)

# Valid enum values for schema fields
_VALID_ENFORCEMENT = {"warn", "block", "off"}
_VALID_FETCH_FAILURE = {"warn", "block"}
_VALID_REQUIRE_RESOLUTION = {"project-wins", "policy-wins", "block"}
_VALID_SELF_DEFINED = {"deny", "warn", "allow"}
_VALID_SCRIPTS = {"allow", "deny"}
_VALID_UNMANAGED_ACTION = {"ignore", "warn", "deny"}
_VALID_AUDIT_ON_INSTALL = {"off", "warn", "block"}

# YAML 1.1 treats "off"/"on" as booleans — map them back to strings
_YAML_BOOL_COERCE = {False: "off", True: "on"}

_KNOWN_TOP_LEVEL_KEYS = {
    "name",
    "version",
    "extends",
    "enforcement",
    "fetch_failure",
    "cache",
    "dependencies",
    "mcp",
    "compilation",
    "manifest",
    "unmanaged_files",
    "security",
    "registry_source",
    "bin_deploy",
    "executables",
}


class PolicyValidationError(Exception):
    """Raised when policy YAML is malformed or violates schema constraints."""

    def __init__(self, errors: list[str], warnings: list[str] | None = None):
        self.errors = errors
        self.warnings = warnings or []
        super().__init__(f"Policy validation failed: {'; '.join(errors)}")


# Every typed policy field, by path. A value of the wrong type is a
# validation error (a malformed policy), checked before any membership test,
# hashing or iteration, so no TypeError escapes the parser (#4140).
_MAPPING_FIELDS: tuple[tuple[str, ...], ...] = (
    ("cache",),
    ("dependencies",),
    ("mcp",),
    ("mcp", "transport"),
    ("compilation",),
    ("compilation", "target"),
    ("compilation", "strategy"),
    ("manifest",),
    ("manifest", "content_types"),
    ("unmanaged_files",),
    ("registry_source",),
    ("security",),
    ("security", "audit"),
    ("security", "integrity"),
    ("bin_deploy",),
    ("executables",),
)
_ENUM_FIELDS: tuple[tuple[tuple[str, ...], frozenset[str]], ...] = (
    (("enforcement",), frozenset(_VALID_ENFORCEMENT)),
    (("fetch_failure",), frozenset(_VALID_FETCH_FAILURE)),
    (("dependencies", "require_resolution"), frozenset(_VALID_REQUIRE_RESOLUTION)),
    (("mcp", "self_defined"), frozenset(_VALID_SELF_DEFINED)),
    (("manifest", "scripts"), frozenset(_VALID_SCRIPTS)),
    (("unmanaged_files", "action"), frozenset(_VALID_UNMANAGED_ACTION)),
    (("security", "audit", "on_install"), frozenset(_VALID_AUDIT_ON_INSTALL)),
)
_BOOL_FIELDS: tuple[tuple[str, ...], ...] = (
    ("dependencies", "require_pinned_constraint"),
    ("mcp", "trust_transitive"),
    ("compilation", "source_attribution"),
    ("manifest", "require_explicit_includes"),
    ("registry_source", "allow_non_registry"),
    ("security", "audit", "fail_on_drift"),
    ("security", "integrity", "require_hashes"),
    ("bin_deploy", "deny_all"),
    ("executables", "deny_all"),
)
_STR_FIELDS: tuple[tuple[str, ...], ...] = (
    ("name",),
    ("extends",),
    ("compilation", "target", "enforce"),
    ("compilation", "strategy", "enforce"),
)
_POSITIVE_INT_FIELDS: tuple[tuple[str, ...], ...] = (
    ("cache", "ttl"),
    ("dependencies", "max_depth"),
)
_STR_LIST_FIELDS: dict[tuple[str, ...], str] = {
    ("dependencies", "allow"): "a YAML list of package patterns",
    ("dependencies", "deny"): "a YAML list of package patterns",
    ("dependencies", "require"): "a YAML list of package patterns",
    ("mcp", "allow"): "a YAML list of server patterns",
    ("mcp", "deny"): "a YAML list of server patterns",
    ("mcp", "transport", "allow"): "a YAML list of transport names",
    ("compilation", "target", "allow"): "a YAML list of target names",
    ("manifest", "required_fields"): "a YAML list of field names",
    ("manifest", "content_types", "allow"): "a YAML list of content types",
    ("unmanaged_files", "directories"): "a YAML list of directories",
    ("unmanaged_files", "exclude"): "a YAML list of path globs",
    ("registry_source", "require"): "a YAML list of package patterns",
    ("security", "audit", "external"): "a YAML list of scanner names",
    ("bin_deploy", "deny"): "a YAML list of package strings",
    ("executables", "deny"): "a YAML list of package strings",
    ("executables", "require"): "a YAML list of package strings",
    ("executables", "recommend"): "a YAML list of package strings",
    ("executables", "enforce"): "a YAML list of package strings",
}
_UNMANAGED_FILES_SHAPE_HINT = (
    "; use a block, for example:\n"
    "  unmanaged_files:\n"
    "    action: deny\n"
    "    directories:\n"
    "      - .github/instructions"
)
_MISSING = object()


def _field(data: dict, path: tuple[str, ...]) -> Any:
    """Return the value at *path*, or ``_MISSING`` when absent or under a non-mapping."""
    node: Any = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return _MISSING
        node = node[key]
    return node


def _set_field(data: dict, path: tuple[str, ...], value: Any) -> None:
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def _type_name(value: Any) -> str:
    return type(value).__name__


def _check_mappings(data: dict, errors: list[str]) -> None:
    for path in _MAPPING_FIELDS:
        value = _field(data, path)
        if value is _MISSING or value is None or isinstance(value, dict):
            continue
        name = ".".join(path)
        if path == ("unmanaged_files",):
            errors.append(
                f"unmanaged_files must be a YAML mapping (got {_type_name(value)} {value!r})"
                + _UNMANAGED_FILES_SHAPE_HINT
            )
        else:
            errors.append(f"{name} must be a YAML mapping")


def _check_enums(data: dict, errors: list[str]) -> None:
    """Membership runs only on a string, so a list or mapping never reaches ``in``."""
    for path, valid in _ENUM_FIELDS:
        value = _field(data, path)
        if value is _MISSING or value is None:
            continue
        if isinstance(value, bool):
            # YAML 1.1 reads off/on as booleans; map them back to strings.
            value = _YAML_BOOL_COERCE[value]
            _set_field(data, path, value)
        if not isinstance(value, str) or value not in valid:
            errors.append(f"{'.'.join(path)} must be one of {sorted(valid)}, got '{value}'")


def _check_scalars(data: dict, errors: list[str]) -> None:
    for path in _BOOL_FIELDS:
        value = _field(data, path)
        if value is not _MISSING and value is not None and not isinstance(value, bool):
            errors.append(f"{'.'.join(path)} must be a boolean, got '{value}'")
    for path in _STR_FIELDS:
        value = _field(data, path)
        if value is not _MISSING and value is not None and not isinstance(value, str):
            errors.append(f"{'.'.join(path)} must be a string (got {_type_name(value)})")
    version = data.get("version")
    if version is not None and (
        isinstance(version, bool) or not isinstance(version, (str, int, float))
    ):
        errors.append(f"version must be a string (got {_type_name(version)})")
    for path in _POSITIVE_INT_FIELDS:
        value = _field(data, path)
        if value is _MISSING or value is None:
            continue
        name = ".".join(path)
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"{name} must be a positive integer, got '{value}'")
        elif value <= 0:
            errors.append(f"{name} must be a positive integer, got {value}")


def _check_str_lists(data: dict, errors: list[str]) -> None:
    for path, shape in _STR_LIST_FIELDS.items():
        value = _field(data, path)
        if value is _MISSING or value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            errors.append(f"{'.'.join(path)} must be {shape} (got {_type_name(value)})")


def validate_policy(data: dict) -> tuple[list[str], list[str]]:
    """Validate a raw dict against the policy schema.

    Returns (errors, warnings) where each is a list of strings. Every field
    is type-checked first; a wrongly typed field is an error, never a crash.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(data, dict):
        errors.append("Policy must be a YAML mapping")
        return errors, warnings

    # Unknown top-level keys (warn, don't fail). Vendor-extension keys are
    # ignored, never interpreted, and do not warn (req-pl-009).
    unknown = {key for key in data if not is_extension_key(key)} - _KNOWN_TOP_LEVEL_KEYS
    for key in sorted(str(key) for key in unknown):
        warnings.append(f"Unknown top-level policy key: '{key}'")

    _check_mappings(data, errors)
    _check_enums(data, errors)
    _check_scalars(data, errors)
    _check_str_lists(data, errors)

    mcp = data.get("mcp")
    if isinstance(mcp, dict):
        _warn_unknown_mcp_transports(mcp.get("transport"), warnings)
    audit = _field(data, ("security", "audit"))
    if isinstance(audit, dict):
        _validate_scanners(audit.get("scanners"), errors, warnings)

    # executables (issue #1873, Gap A): org grant/deny trust block.
    _validate_executables(data, warnings)

    return errors, warnings


def _validate_executables(data: dict, warnings: list[str]) -> None:
    """Warn on deprecated ``bin_deploy`` and the inert ``executables.enforce``."""
    if data.get("bin_deploy") is not None:
        warnings.append(
            "'bin_deploy' is deprecated; use 'executables' (deny_all/deny) instead. "
            "bin_deploy is still honored as a bin-scoped deny alias for one minor cycle."
        )
    execs = data.get("executables")
    if isinstance(execs, dict) and execs.get("enforce"):
        warnings.append(
            "executables.enforce is accepted but INERT in v1: it degrades to "
            "'recommend' (no force-execute; a user deny still overrides). "
            "Full mandate ships in v2."
        )


def _build_policy(data: dict) -> ApmPolicy:
    """Build an ApmPolicy from a validated dict."""
    if not data:
        return ApmPolicy()

    from ..core.target_detection import (
        canonical_policy_target_name,
        canonical_policy_target_names,
    )

    cache_data = data.get("cache") or {}
    cache = PolicyCache(
        ttl=cache_data.get("ttl", PolicyCache.ttl),
    )

    _raw_deps = data.get("dependencies")
    deps_data = _raw_deps if isinstance(_raw_deps, dict) else {}
    _deps_absent = _raw_deps is None
    dependencies = DependencyPolicy(
        allow=_parse_allow(deps_data.get("allow")),
        deny=None
        if (_deps_absent or "deny" not in deps_data or deps_data["deny"] is None)
        else _parse_tuple(deps_data["deny"]),
        require=None
        if (_deps_absent or "require" not in deps_data or deps_data["require"] is None)
        else _parse_tuple(deps_data["require"]),
        require_resolution=deps_data.get("require_resolution", DependencyPolicy.require_resolution),
        max_depth=deps_data.get("max_depth", DependencyPolicy.max_depth),
        require_pinned_constraint=bool(
            deps_data.get("require_pinned_constraint", DependencyPolicy.require_pinned_constraint)
        ),
    )

    mcp_data = data.get("mcp") or {}
    transport_data = mcp_data.get("transport") or {}
    mcp = McpPolicy(
        allow=_parse_allow(mcp_data.get("allow")),
        deny=_parse_tuple(mcp_data.get("deny")),
        transport=McpTransportPolicy(
            allow=_parse_transport_allow(transport_data.get("allow")),
        ),
        self_defined=mcp_data.get("self_defined", McpPolicy.self_defined),
        trust_transitive=mcp_data.get("trust_transitive", McpPolicy.trust_transitive),
    )

    comp_data = data.get("compilation") or {}
    target_data = comp_data.get("target") or {}  # architecture-authority-exempt: policy-schema
    strategy_data = comp_data.get("strategy") or {}
    compilation = CompilationPolicy(
        target=CompilationTargetPolicy(
            allow=canonical_policy_target_names(_parse_allow(target_data.get("allow"))),
            enforce=canonical_policy_target_name(target_data.get("enforce")),
        ),
        strategy=CompilationStrategyPolicy(
            enforce=strategy_data.get("enforce"),
        ),
        source_attribution=comp_data.get(
            "source_attribution", CompilationPolicy.source_attribution
        ),
    )

    manifest_data = data.get("manifest") or {}
    manifest = ManifestPolicy(
        required_fields=_parse_tuple(manifest_data.get("required_fields")),
        scripts=manifest_data.get("scripts", ManifestPolicy.scripts),
        content_types=manifest_data.get("content_types"),
        require_explicit_includes=bool(manifest_data.get("require_explicit_includes", False)),
    )

    raw_uf = data.get("unmanaged_files")
    if raw_uf is None:
        unmanaged_files = UnmanagedFilesPolicy(action=None, directories=None)
    else:
        uf_data = raw_uf
        action = uf_data.get("action")
        directories = (
            None
            if ("directories" not in uf_data or uf_data["directories"] is None)
            else _parse_tuple(uf_data["directories"])
        )
        exclude = (
            None
            if ("exclude" not in uf_data or uf_data["exclude"] is None)
            else _parse_tuple(uf_data["exclude"])
        )
        unmanaged_files = UnmanagedFilesPolicy(
            action=action, directories=directories, exclude=exclude
        )

    reg_data = data.get("registry_source") or {}
    registry_source = RegistrySourcePolicy(
        require=_parse_tuple(reg_data.get("require")),
        allow_non_registry=bool(reg_data.get("allow_non_registry", True)),
    )

    sec_data = data.get("security") or {}
    raw_audit = sec_data.get("audit")
    audit_data = raw_audit if isinstance(raw_audit, dict) else {}
    on_install = audit_data.get("on_install")
    if isinstance(on_install, bool):
        on_install = _YAML_BOOL_COERCE.get(on_install, str(on_install))
    raw_integrity = sec_data.get("integrity")
    integrity_data = raw_integrity if isinstance(raw_integrity, dict) else {}
    security = SecurityPolicy(
        audit=AuditPolicy(
            on_install=on_install,
            external=None
            if "external" not in audit_data or audit_data["external"] is None
            else _parse_tuple(audit_data["external"]),
            scanners=_parse_scanners(audit_data.get("scanners")),
            fail_on_drift=bool(audit_data.get("fail_on_drift", False)),
        ),
        integrity=IntegrityPolicy(
            require_hashes=bool(integrity_data.get("require_hashes", False)),
        ),
    )

    bd_data = data.get("bin_deploy") or {}
    bin_deploy = BinDeployPolicy(
        deny_all=bool(bd_data.get("deny_all", False)),
        deny=_parse_tuple(bd_data.get("deny")) if bd_data.get("deny") is not None else (),
    )

    ex_data = data.get("executables") or {}
    executables = ExecutablesPolicy(
        deny_all=bool(ex_data.get("deny_all", False)),
        deny=_parse_tuple(ex_data.get("deny")) if ex_data.get("deny") is not None else (),
        require=_parse_tuple(ex_data.get("require")) if ex_data.get("require") is not None else (),
        recommend=_parse_tuple(ex_data.get("recommend"))
        if ex_data.get("recommend") is not None
        else (),
        enforce=_parse_tuple(ex_data.get("enforce")) if ex_data.get("enforce") is not None else (),
    )

    return ApmPolicy(
        name=data.get("name", "") or "",
        version=data.get("version", "") or "",
        extends=data.get("extends"),
        enforcement=data.get("enforcement", ApmPolicy.enforcement),
        fetch_failure=data.get("fetch_failure", ApmPolicy.fetch_failure),
        cache=cache,
        dependencies=dependencies,
        mcp=mcp,
        compilation=compilation,
        manifest=manifest,
        unmanaged_files=unmanaged_files,
        registry_source=registry_source,
        security=security,
        bin_deploy=bin_deploy,
        executables=executables,
    )


def _looks_like_yaml_content(source: str) -> bool:
    """Return True when a string is more likely inline YAML than a file path.

    This avoids probing the filesystem for large YAML payloads, which can raise
    platform-specific path errors such as ENAMETOOLONG on macOS.
    """
    stripped = source.lstrip()

    if "\n" in source or "\r" in source:
        return True

    if stripped.startswith(("{", "[", "---", "- ")):
        return True

    first_line = stripped.splitlines()[0] if stripped else ""
    return ": " in first_line or first_line.endswith(":")


def load_policy(source: str | Path) -> tuple[ApmPolicy, list[str]]:
    """Load and validate an apm-policy.yml from a file path or YAML string.

    Returns (policy, warnings). Raises PolicyValidationError on invalid input.
    """
    raw: str

    if isinstance(source, Path):
        raw = source.read_text(encoding="utf-8") if source.is_file() else str(source)
    elif _looks_like_yaml_content(source):
        raw = source
    else:
        path = Path(source)
        try:
            is_file = path.is_file()
        except OSError as exc:
            if exc.errno == errno.ENAMETOOLONG:
                is_file = False
            else:
                raise

        if is_file:  # noqa: SIM108
            raw = path.read_text(encoding="utf-8")
        else:
            raw = source

    try:
        data = load_yaml_str(raw)
    except yaml.YAMLError as exc:
        raise PolicyValidationError([f"YAML parse error: {exc}"]) from exc

    if data is None:
        data = {}

    if not isinstance(data, dict):
        raise PolicyValidationError(["Policy must be a YAML mapping"])

    errors, warnings = validate_policy(data)
    if errors:
        raise PolicyValidationError(errors, warnings)

    try:
        return _build_policy(data), warnings
    except (TypeError, AttributeError, ValueError) as exc:
        # A shape validate_policy did not foresee is still a malformed policy.
        raise PolicyValidationError([f"Policy has an invalid field: {exc}"], warnings) from exc


def _parse_allow(val: Any) -> tuple[str, ...] | None:
    """Parse an allow-list field.

    * Key absent (``val is None``) -> ``None`` ("no opinion").
    * Key present with a list      -> ``tuple(...)`` (may be empty).
    """
    if val is None:
        return None
    if isinstance(val, list):
        return tuple(val)
    return None


def _parse_transport_allow(val: Any) -> tuple[str, ...] | None:
    """Parse ``mcp.transport.allow`` into canonical transport names, once.

    ``http`` folds into ``streamable-http`` so every consumer (inheritance,
    checks, status) compares canonical names only.
    """
    allow = _parse_allow(val)
    if allow is None:
        return None
    return canonical_transport_names(str(item) for item in allow)


def _warn_unknown_mcp_transports(transport: Any, warnings: list[str]) -> None:
    """Warn on ``mcp.transport.allow`` entries that can never match."""
    if not isinstance(transport, dict) or not isinstance(transport.get("allow"), list):
        return
    for name in canonical_transport_names(str(item) for item in transport["allow"]):
        if name not in POLICY_TRANSPORTS:
            warnings.append(
                f"mcp.transport.allow: unknown transport '{name}' never matches; "
                f"use {', '.join(POLICY_TRANSPORTS)} (http is an alias of streamable-http)"
            )


def _parse_tuple(val: Any) -> tuple[str, ...]:
    """Parse a deny/require/directories field into a tuple."""
    if isinstance(val, list):
        return tuple(val)
    return ()


def _validate_scanners(scanners: Any, errors: list[str], warnings: list[str]) -> None:
    """Validate the optional ``security.audit.scanners`` governance mapping.

    Restrict-only: each scanner block may carry ``allow_args`` (bool). Unknown
    scanner names are a warning (forward-compat), not a hard error.
    """
    if scanners is None:
        return
    if not isinstance(scanners, dict):
        errors.append("security.audit.scanners must be a YAML mapping")
        return
    from ..security.external.registry import SUPPORTED_SCANNERS

    for name, block in scanners.items():
        if not isinstance(name, str):
            errors.append(
                f"security.audit.scanners keys must be strings (got {type(name).__name__})"
            )
            continue
        if name not in SUPPORTED_SCANNERS:
            warnings.append(
                f"security.audit.scanners: unknown scanner '{name}' "
                f"(supported: {', '.join(SUPPORTED_SCANNERS)})"
            )
        if block is None:
            continue
        if not isinstance(block, dict):
            errors.append(f"security.audit.scanners.{name} must be a YAML mapping")
            continue
        allow_args = block.get("allow_args")
        if allow_args is not None and not isinstance(allow_args, bool):
            errors.append(
                f"security.audit.scanners.{name}.allow_args must be a boolean, got '{allow_args}'"
            )


def _parse_scanners(scanners: Any) -> tuple[tuple[str, ScannerGovernance], ...] | None:
    """Build the ``AuditPolicy.scanners`` tuple-of-pairs from validated data."""
    if not isinstance(scanners, dict) or not scanners:
        return None
    pairs: list[tuple[str, ScannerGovernance]] = []
    for name, block in scanners.items():
        block_data = block if isinstance(block, dict) else {}
        allow_args = block_data.get("allow_args")
        pairs.append(
            (
                str(name),
                ScannerGovernance(allow_args=allow_args if isinstance(allow_args, bool) else None),
            )
        )
    return tuple(pairs)
