"""YAML parser and validator for apm-policy.yml files."""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

import yaml

from .schema import (
    ApmPolicy,
    CompilationPolicy,
    CompilationStrategyPolicy,
    CompilationTargetPolicy,
    DependencyPolicy,
    ManifestPolicy,
    McpPolicy,
    McpTransportPolicy,
    PolicyCache,
    UnmanagedFilesPolicy,
)

# Valid enum values for schema fields
_VALID_ENFORCEMENT = {"warn", "block", "off"}
_VALID_FETCH_FAILURE = {"warn", "block"}
_VALID_REQUIRE_RESOLUTION = {"project-wins", "policy-wins", "block"}
_VALID_SELF_DEFINED = {"deny", "warn", "allow"}
_VALID_SCRIPTS = {"allow", "deny"}
_VALID_UNMANAGED_ACTION = {"ignore", "warn", "deny"}

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
}


class PolicyValidationError(Exception):
    """Raised when policy YAML is malformed or violates schema constraints."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__(f"Policy validation failed: {'; '.join(errors)}")


def validate_policy(data: dict) -> Tuple[List[str], List[str]]:
    """Validate a raw dict against the policy schema.

    Returns (errors, warnings) where each is a list of strings.
    """
    errors: List[str] = []
    warnings: List[str] = []

    if not isinstance(data, dict):
        errors.append("Policy must be a YAML mapping")
        return errors, warnings

    # Unknown top-level keys (warn, don't fail)
    unknown = set(data.keys()) - _KNOWN_TOP_LEVEL_KEYS
    for key in sorted(unknown):
        warnings.append(f"Unknown top-level policy key: '{key}'")

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
            f"fetch_failure must be one of {sorted(_VALID_FETCH_FAILURE)}, "
            f"got '{fetch_failure}'"
        )

    # cache.ttl
    cache = data.get("cache")
    if isinstance(cache, dict):
        ttl = cache.get("ttl")
        if ttl is not None:
            if not isinstance(ttl, int) or isinstance(ttl, bool):
                errors.append(f"cache.ttl must be a positive integer, got '{ttl}'")
            elif ttl <= 0:
                errors.append(f"cache.ttl must be a positive integer, got {ttl}")

    # dependencies
    deps = data.get("dependencies")
    if isinstance(deps, dict):
        rr = deps.get("require_resolution")
        if rr is not None and rr not in _VALID_REQUIRE_RESOLUTION:
            errors.append(
                f"dependencies.require_resolution must be one of "
                f"{sorted(_VALID_REQUIRE_RESOLUTION)}, got '{rr}'"
            )
        md = deps.get("max_depth")
        if md is not None:
            if not isinstance(md, int) or isinstance(md, bool):
                errors.append(
                    f"dependencies.max_depth must be a positive integer, got '{md}'"
                )
            elif md <= 0:
                errors.append(
                    f"dependencies.max_depth must be a positive integer, got {md}"
                )

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
            errors.append(
                f"manifest.require_explicit_includes must be a boolean, got '{rei}'"
            )

    # unmanaged_files.action
    uf = data.get("unmanaged_files")
    if isinstance(uf, dict):
        action = uf.get("action")
        if action is not None and action not in _VALID_UNMANAGED_ACTION:
            errors.append(
                f"unmanaged_files.action must be one of "
                f"{sorted(_VALID_UNMANAGED_ACTION)}, got '{action}'"
            )

    return errors, warnings


def _build_policy(data: dict) -> ApmPolicy:
    """Build an ApmPolicy from a validated dict."""
    if not data:
        return ApmPolicy()

    cache_data = data.get("cache") or {}
    cache = PolicyCache(
        ttl=cache_data.get("ttl", PolicyCache.ttl),
    )

    deps_data = data.get("dependencies") or {}
    dependencies = DependencyPolicy(
        allow=_parse_allow(deps_data.get("allow")),
        deny=_parse_tuple(deps_data.get("deny")),
        require=_parse_tuple(deps_data.get("require")),
        require_resolution=deps_data.get(
            "require_resolution", DependencyPolicy.require_resolution
        ),
        max_depth=deps_data.get("max_depth", DependencyPolicy.max_depth),
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
        trust_transitive=mcp_data.get(
            "trust_transitive", McpPolicy.trust_transitive
        ),
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
        require_explicit_includes=bool(
            manifest_data.get("require_explicit_includes", False)
        ),
    )

    uf_data = data.get("unmanaged_files") or {}
    unmanaged_files = UnmanagedFilesPolicy(
        action=uf_data.get("action", UnmanagedFilesPolicy.action),
        directories=_parse_tuple(uf_data.get("directories")),
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


def load_policy(source: Union[str, Path]) -> Tuple[ApmPolicy, List[str]]:
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

        if is_file:
            raw = path.read_text(encoding="utf-8")
        else:
            raw = source

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PolicyValidationError([f"YAML parse error: {exc}"]) from exc

    if data is None:
        data = {}

    if not isinstance(data, dict):
        raise PolicyValidationError(["Policy must be a YAML mapping"])

    errors, warnings = validate_policy(data)
    if errors:
        raise PolicyValidationError(errors)

    return _build_policy(data), warnings


def _parse_allow(val: Any) -> Optional[Tuple[str, ...]]:
    """Parse an allow-list field.

    * Key absent (``val is None``) -> ``None`` ("no opinion").
    * Key present with a list      -> ``tuple(...)`` (may be empty).
    """
    if val is None:
        return None
    if isinstance(val, list):
        return tuple(val)
    return None


def _parse_tuple(val: Any) -> Tuple[str, ...]:
    """Parse a deny/require/directories field into a tuple."""
    if isinstance(val, list):
        return tuple(val)
    return ()
