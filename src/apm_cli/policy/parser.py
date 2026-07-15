"""YAML parser and validator for apm-policy.yml files."""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Any

import yaml

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


def validate_policy(data: dict) -> tuple[list[str], list[str]]:
    """Validate a raw dict against the policy schema.

    Returns (errors, warnings) where each is a list of strings.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(data, dict):
        errors.append("Policy must be a YAML mapping")
        return errors, warnings

    # Unknown top-level keys (warn, don't fail)
    unknown = set(data.keys()) - _KNOWN_TOP_LEVEL_KEYS
    for key in sorted(unknown):
        warnings.append(f"Unknown top-level policy key: '{key}'")

    for key in ("mcp", "manifest", "compilation", "registry_source", "bin_deploy"):
        value = data.get(key)
        if value is not None and not isinstance(value, dict):
            errors.append(f"{key} must be a YAML mapping")

    # enforcement (coerce YAML booleans: off → "off")
    enforcement = data.get("enforcement")
    if isinstance(enforcement, bool):
        enforcement = _YAML_BOOL_COERCE.get(enforcement, str(enforcement))
        data["enforcement"] = enforcement
    if enforcement is not None and enforcement not in _VALID_ENFORCEMENT:
        errors.append(
            f"enforcement must be one of {sorted(_VALID_ENFORCEMENT)}, got '{enforcement}'"
        )

    # fetch_failure (closes #829): controls fail-closed behavior on
    # policy fetch / parse failure. Default "warn" (back-compat).
    fetch_failure = data.get("fetch_failure")
    if isinstance(fetch_failure, bool):
        fetch_failure = _YAML_BOOL_COERCE.get(fetch_failure, str(fetch_failure))
        data["fetch_failure"] = fetch_failure
    if fetch_failure is not None and fetch_failure not in _VALID_FETCH_FAILURE:
        errors.append(
            f"fetch_failure must be one of {sorted(_VALID_FETCH_FAILURE)}, got '{fetch_failure}'"
        )

    # cache.ttl
    cache = data.get("cache")
    if cache is not None and not isinstance(cache, dict):
        errors.append("cache must be a YAML mapping")
    elif isinstance(cache, dict):
        ttl = cache.get("ttl")
        if ttl is not None:
            if not isinstance(ttl, int) or isinstance(ttl, bool):
                errors.append(f"cache.ttl must be a positive integer, got '{ttl}'")
            elif ttl <= 0:
                errors.append(f"cache.ttl must be a positive integer, got {ttl}")

    # dependencies
    deps = data.get("dependencies")
    if deps is not None and not isinstance(deps, dict):
        errors.append("dependencies must be a YAML mapping")
    elif isinstance(deps, dict):
        for key in ("allow", "deny", "require"):
            value = deps.get(key)
            if value is not None and (
                not isinstance(value, list) or not all(isinstance(item, str) for item in value)
            ):
                errors.append(f"dependencies.{key} must be a YAML list of package patterns")
        rr = deps.get("require_resolution")
        if rr is not None and rr not in _VALID_REQUIRE_RESOLUTION:
            errors.append(
                f"dependencies.require_resolution must be one of "
                f"{sorted(_VALID_REQUIRE_RESOLUTION)}, got '{rr}'"
            )
        md = deps.get("max_depth")
        if md is not None:
            if not isinstance(md, int) or isinstance(md, bool):
                errors.append(f"dependencies.max_depth must be a positive integer, got '{md}'")
            elif md <= 0:
                errors.append(f"dependencies.max_depth must be a positive integer, got {md}")
        rpc = deps.get("require_pinned_constraint")
        if rpc is not None and not isinstance(rpc, bool):
            errors.append(f"dependencies.require_pinned_constraint must be a boolean, got '{rpc}'")

    # mcp.self_defined
    mcp = data.get("mcp")
    if isinstance(mcp, dict):
        sd = mcp.get("self_defined")
        if sd is not None and sd not in _VALID_SELF_DEFINED:
            errors.append(
                f"mcp.self_defined must be one of {sorted(_VALID_SELF_DEFINED)}, got '{sd}'"
            )

    # manifest.scripts
    manifest = data.get("manifest")
    if isinstance(manifest, dict):
        scripts = manifest.get("scripts")
        if scripts is not None and scripts not in _VALID_SCRIPTS:
            errors.append(
                f"manifest.scripts must be one of {sorted(_VALID_SCRIPTS)}, got '{scripts}'"
            )
        rei = manifest.get("require_explicit_includes")
        if rei is not None and not isinstance(rei, bool):
            errors.append(f"manifest.require_explicit_includes must be a boolean, got '{rei}'")

    # unmanaged_files
    uf = data.get("unmanaged_files")
    if uf is not None and not isinstance(uf, dict):
        errors.append(
            "unmanaged_files must be a YAML mapping "
            f"(got {type(uf).__name__} {uf!r}); use a block, for example:\n"
            "  unmanaged_files:\n"
            "    action: deny\n"
            "    directories:\n"
            "      - .github/instructions"
        )
    elif isinstance(uf, dict):
        action = uf.get("action")
        if action is not None and action not in _VALID_UNMANAGED_ACTION:
            errors.append(
                f"unmanaged_files.action must be one of "
                f"{sorted(_VALID_UNMANAGED_ACTION)}, got '{action}'"
            )
        exclude = uf.get("exclude")
        if exclude is not None and not isinstance(exclude, list):
            errors.append(
                "unmanaged_files.exclude must be a YAML list of path globs "
                f"(got {type(exclude).__name__})"
            )

    # security.audit (install-time audit + external scanners)
    security = data.get("security")
    if security is not None and not isinstance(security, dict):
        errors.append("security must be a YAML mapping")
    elif isinstance(security, dict):
        audit = security.get("audit")
        if audit is not None and not isinstance(audit, dict):
            errors.append("security.audit must be a YAML mapping")
        elif isinstance(audit, dict):
            on_install = audit.get("on_install")
            if isinstance(on_install, bool):
                on_install = _YAML_BOOL_COERCE.get(on_install, str(on_install))
            if on_install is not None and on_install not in _VALID_AUDIT_ON_INSTALL:
                errors.append(
                    f"security.audit.on_install must be one of "
                    f"{sorted(_VALID_AUDIT_ON_INSTALL)}, got '{on_install}'"
                )
            fail_on_drift = audit.get("fail_on_drift")
            if fail_on_drift is not None and not isinstance(fail_on_drift, bool):
                errors.append(
                    f"security.audit.fail_on_drift must be a boolean, got '{fail_on_drift}'"
                )
            _validate_scanners(audit.get("scanners"), errors, warnings)
        integrity = security.get("integrity")
        if integrity is not None and not isinstance(integrity, dict):
            errors.append("security.integrity must be a YAML mapping")
        elif isinstance(integrity, dict):
            require_hashes = integrity.get("require_hashes")
            if require_hashes is not None and not isinstance(require_hashes, bool):
                errors.append(
                    f"security.integrity.require_hashes must be a boolean, got '{require_hashes}'"
                )

    # executables (issue #1873, Gap A): org grant/deny trust block.
    _validate_executables(data, errors, warnings)

    return errors, warnings


def _validate_executables(data: dict, errors: list[str], warnings: list[str]) -> None:
    """Validate the ``executables:`` block and warn on deprecated ``bin_deploy``."""
    if data.get("bin_deploy") is not None:
        warnings.append(
            "'bin_deploy' is deprecated; use 'executables' (deny_all/deny) instead. "
            "bin_deploy is still honored as a bin-scoped deny alias for one minor cycle."
        )

    execs = data.get("executables")
    if execs is None:
        return
    if not isinstance(execs, dict):
        errors.append("executables must be a YAML mapping")
        return
    deny_all = execs.get("deny_all")
    if deny_all is not None and not isinstance(deny_all, bool):
        errors.append(f"executables.deny_all must be a boolean, got '{deny_all}'")
    for key in ("deny", "require", "recommend", "enforce"):
        val = execs.get(key)
        if val is not None and not isinstance(val, list):
            errors.append(
                f"executables.{key} must be a YAML list of package strings "
                f"(got {type(val).__name__})"
            )
    if execs.get("enforce"):
        warnings.append(
            "executables.enforce is accepted but INERT in v1: it degrades to "
            "'recommend' (no force-execute; a user deny still overrides). "
            "Full mandate ships in v2."
        )


def _build_policy(data: dict) -> ApmPolicy:
    """Build an ApmPolicy from a validated dict."""
    if not data:
        return ApmPolicy()

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
            allow=_parse_allow(transport_data.get("allow")),
        ),
        self_defined=mcp_data.get("self_defined", McpPolicy.self_defined),
        trust_transitive=mcp_data.get("trust_transitive", McpPolicy.trust_transitive),
    )

    comp_data = data.get("compilation") or {}
    target_data = comp_data.get("target") or {}
    strategy_data = comp_data.get("strategy") or {}
    compilation = CompilationPolicy(
        target=CompilationTargetPolicy(
            allow=_parse_allow(target_data.get("allow")),
            enforce=target_data.get("enforce"),
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

    return _build_policy(data), warnings


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
