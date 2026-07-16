"""Bounded property tests for Govern policy serialization and cache behavior."""

from __future__ import annotations

import copy
import dataclasses
import tempfile
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from hypothesis import given, seed, settings
from hypothesis import strategies as st

from apm_cli.policy import discovery
from apm_cli.policy.inheritance import resolve_policy_chain
from apm_cli.policy.parser import load_policy
from apm_cli.policy.schema import ApmPolicy

PROPERTY_SEED = 0xA9C2026
PROPERTY_PROFILE = settings(
    max_examples=24,
    deadline=750,
    database=None,
    print_blob=True,
)
SINGLE_EXAMPLE_PROFILE = settings(
    max_examples=1,
    deadline=750,
    database=None,
    print_blob=True,
)
POLICY_URL = "https://policy.example.com/apm-policy.yml"
PACKAGE_VALUES = (
    "acme/alpha",
    "acme/beta",
    "contoso/security-*",
    "github/*",
    "microsoft/*",
)
TARGET_VALUES = ("claude", "copilot", "vscode")
CONTENT_TYPE_VALUES = ("agents", "instructions", "prompts", "skills")
PATH_VALUES = (".agents", ".github/instructions", ".github/prompts", ".github/skills")
INVALID_CASES = (
    ("enforcement", ("enforcement",), "invalid"),
    ("fetch-failure", ("fetch_failure",), "off"),
    ("cache-ttl", ("cache", "ttl"), 0),
    ("dependency-resolution", ("dependencies", "require_resolution"), "relax"),
    ("dependency-depth", ("dependencies", "max_depth"), -1),
    ("manifest-scripts", ("manifest", "scripts"), "warn"),
    ("unmanaged-action", ("unmanaged_files", "action"), "allow"),
    ("audit-on-install", ("security", "audit", "on_install"), "allow"),
    ("integrity-hashes", ("security", "integrity", "require_hashes"), "yes"),
    ("executables-deny-all", ("executables", "deny_all"), "yes"),
)


def _non_empty_unique(
    values: tuple[str, ...], *, max_size: int = 3
) -> st.SearchStrategy[list[str]]:
    """Return bounded non-empty unique lists from a stable finite vocabulary."""
    candidates = tuple(
        candidate
        for size in range(1, min(len(values), max_size) + 1)
        for candidate in combinations(values, size)
    )
    return st.sampled_from(candidates).map(list)


@st.composite
def enforceable_policy_data(draw: st.DrawFn) -> dict[str, Any]:
    """Generate valid policies with every enforceable field set non-default."""
    package_list = _non_empty_unique(PACKAGE_VALUES)
    target_list = _non_empty_unique(TARGET_VALUES)
    scanner_list = _non_empty_unique(("sarif", "skillspector"), max_size=2)
    return {
        "name": "govern-property-policy",
        "version": "1.0.0",
        "enforcement": draw(st.sampled_from(("block", "off"))),
        "fetch_failure": "block",
        "cache": {"ttl": draw(st.integers(min_value=1, max_value=3599))},
        "dependencies": {
            "allow": draw(package_list),
            "deny": draw(package_list),
            "require": draw(package_list),
            "require_resolution": draw(st.sampled_from(("block", "policy-wins"))),
            "max_depth": draw(st.integers(min_value=1, max_value=49)),
            "require_pinned_constraint": True,
        },
        "mcp": {
            "allow": draw(package_list),
            "deny": draw(package_list),
            "transport": {
                "allow": draw(_non_empty_unique(("http", "sse", "stdio", "streamable-http")))
            },
            "self_defined": draw(st.sampled_from(("allow", "deny"))),
            "trust_transitive": True,
        },
        "compilation": {
            "target": {
                "allow": draw(target_list),
                "enforce": draw(st.sampled_from(TARGET_VALUES)),
            },
            "strategy": {
                "enforce": draw(st.sampled_from(("distributed", "single-file"))),
            },
            "source_attribution": True,
        },
        "manifest": {
            "required_fields": draw(
                _non_empty_unique(("dependencies", "description", "name", "version"))
            ),
            "scripts": "deny",
            "content_types": {
                "allow": draw(_non_empty_unique(CONTENT_TYPE_VALUES)),
            },
            "require_explicit_includes": True,
        },
        "unmanaged_files": {
            "action": draw(st.sampled_from(("deny", "warn"))),
            "directories": draw(_non_empty_unique(PATH_VALUES)),
            "exclude": draw(_non_empty_unique(("build/**", "dist/**", "tmp/**"))),
        },
        "registry_source": {
            "require": draw(_non_empty_unique(("internal", "official", "trusted"))),
            "allow_non_registry": False,
        },
        "security": {
            "audit": {
                "on_install": draw(st.sampled_from(("block", "off", "warn"))),
                "external": draw(scanner_list),
                "scanners": {
                    "sarif": {"allow_args": draw(st.booleans())},
                    "skillspector": {"allow_args": draw(st.booleans())},
                },
                "fail_on_drift": True,
            },
            "integrity": {"require_hashes": True},
        },
        "bin_deploy": {
            "deny_all": True,
            "deny": draw(package_list),
        },
        "executables": {
            "deny_all": True,
            "deny": draw(package_list),
            "require": draw(package_list),
            "recommend": draw(package_list),
            "enforce": draw(package_list),
        },
        "future_govern_key": "preserve-authored-warning",
    }


def _policy_yaml(data: dict[str, Any]) -> str:
    """Render generated policy input without relying on cache serialization."""
    return yaml.safe_dump(data, default_flow_style=False, sort_keys=False)


def _enforceable_fields(policy: ApmPolicy) -> dict[str, Any]:
    """Project every current enforceable/cache field into a named observation."""
    return {
        "enforcement": policy.enforcement,
        "fetch_failure": policy.fetch_failure,
        "cache.ttl": policy.cache.ttl,
        "dependencies.allow": policy.dependencies.allow,
        "dependencies.deny": policy.dependencies.deny,
        "dependencies.require": policy.dependencies.require,
        "dependencies.require_resolution": policy.dependencies.require_resolution,
        "dependencies.max_depth": policy.dependencies.max_depth,
        "dependencies.require_pinned_constraint": policy.dependencies.require_pinned_constraint,
        "mcp.allow": policy.mcp.allow,
        "mcp.deny": policy.mcp.deny,
        "mcp.transport.allow": policy.mcp.transport.allow,
        "mcp.self_defined": policy.mcp.self_defined,
        "mcp.trust_transitive": policy.mcp.trust_transitive,
        "compilation.target.allow": policy.compilation.target.allow,
        "compilation.target.enforce": policy.compilation.target.enforce,
        "compilation.strategy.enforce": policy.compilation.strategy.enforce,
        "compilation.source_attribution": policy.compilation.source_attribution,
        "manifest.required_fields": policy.manifest.required_fields,
        "manifest.scripts": policy.manifest.scripts,
        "manifest.content_types": policy.manifest.content_types,
        "manifest.require_explicit_includes": policy.manifest.require_explicit_includes,
        "unmanaged_files.action": policy.unmanaged_files.action,
        "unmanaged_files.directories": policy.unmanaged_files.directories,
        "unmanaged_files.exclude": policy.unmanaged_files.exclude,
        "registry_source.require": policy.registry_source.require,
        "registry_source.allow_non_registry": policy.registry_source.allow_non_registry,
        "security.audit.on_install": policy.security.audit.on_install,
        "security.audit.external": policy.security.audit.external,
        "security.audit.scanners": policy.security.audit.scanners,
        "security.audit.fail_on_drift": policy.security.audit.fail_on_drift,
        "security.integrity.require_hashes": policy.security.integrity.require_hashes,
        "bin_deploy.deny_all": policy.bin_deploy.deny_all,
        "bin_deploy.deny": policy.bin_deploy.deny,
        "executables.deny_all": policy.executables.deny_all,
        "executables.deny": policy.executables.deny,
        "executables.require": policy.executables.require,
        "executables.recommend": policy.executables.recommend,
        "executables.enforce": policy.executables.enforce,
    }


def _schema_enforceable_field_paths(policy: ApmPolicy) -> set[str]:
    """Return dataclass leaf paths, excluding non-enforceable policy metadata."""
    paths: set[str] = set()

    def visit(value: Any, prefix: str = "") -> None:
        for field in dataclasses.fields(value):
            path = f"{prefix}.{field.name}" if prefix else field.name
            child = getattr(value, field.name)
            if dataclasses.is_dataclass(child):
                visit(child, path)
            else:
                paths.add(path)

    visit(policy)
    return paths - {"name", "version", "extends"}


def _assert_cache_round_trip(policy: ApmPolicy) -> str:
    """Assert field preservation and canonical idempotence through cache YAML."""
    serialized = discovery._serialize_policy(policy)
    reparsed, _ = load_policy(serialized)
    assert _enforceable_fields(reparsed) == _enforceable_fields(policy), (
        "cache round-trip changed enforceable fields"
    )
    assert discovery._serialize_policy(reparsed) == serialized
    return serialized


def _result_observation(result: discovery.PolicyFetchResult) -> tuple[Any, ...]:
    """Return user-observable policy result data, excluding cache provenance."""
    assert result.policy is not None
    return (
        _enforceable_fields(result.policy),
        result.source,
        result.error,
        result.fetch_error,
        result.outcome,
        result.warnings,
        result.raw_bytes_hash,
    )


def _cache_bytes(project_root: Path) -> dict[str, bytes]:
    """Read one policy cache entry as a filename-to-bytes map."""
    cache_dir = project_root / "apm_modules" / discovery.POLICY_CACHE_DIR
    return {path.name: path.read_bytes() for path in sorted(cache_dir.iterdir())}


def _set_nested(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """Replace one nested policy value in-place."""
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def _assert_policy_ttl_refreshes(data: dict[str, Any]) -> None:
    """Assert a warm read refreshes immediately after the policy TTL."""
    content = _policy_yaml(data)
    response = SimpleNamespace(status_code=200, text=content, headers={})
    ttl = data["cache"]["ttl"]
    cached_at = 10_000.0

    with tempfile.TemporaryDirectory() as temp_dir:
        project_root = Path(temp_dir)
        with patch("apm_cli.policy.discovery.requests.get", return_value=response) as request:
            with patch("apm_cli.policy.discovery.time.time", return_value=cached_at):
                cold = discovery._fetch_from_url(POLICY_URL, project_root)
            with patch(
                "apm_cli.policy.discovery.time.time",
                return_value=cached_at + ttl + 1,
            ):
                refreshed = discovery._fetch_from_url(POLICY_URL, project_root)

        assert request.call_count == 2, "policy cache TTL did not trigger refresh"
        assert not cold.cached
        assert not refreshed.cached
        assert _result_observation(refreshed) == _result_observation(cold)


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(data=enforceable_policy_data())
def test_effective_policy_cache_serialization_is_lossless_and_canonical(
    data: dict[str, Any],
) -> None:
    """Parse -> effective policy -> cache YAML -> parse preserves all 39 fields."""
    parsed, _ = load_policy(_policy_yaml(data))
    effective = resolve_policy_chain([parsed])

    assert set(_enforceable_fields(effective)) == _schema_enforceable_field_paths(effective)
    assert len(_enforceable_fields(effective)) == 39
    _assert_cache_round_trip(effective)


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(data=enforceable_policy_data())
def test_cold_and_warm_policy_results_are_observationally_equivalent(
    data: dict[str, Any],
) -> None:
    """A canonical warm-cache read preserves cold outcome and authored warnings."""
    content = _policy_yaml(data)
    response = SimpleNamespace(status_code=200, text=content, headers={})

    with tempfile.TemporaryDirectory() as temp_dir:
        project_root = Path(temp_dir)
        with patch("apm_cli.policy.discovery.requests.get", return_value=response) as request:
            cold = discovery._fetch_from_url(POLICY_URL, project_root)
            warm = discovery._fetch_from_url(POLICY_URL, project_root)

        assert request.call_count == 1
        assert not cold.cached
        assert warm.cached
        assert _result_observation(warm) == _result_observation(cold)


@seed(PROPERTY_SEED)
@PROPERTY_PROFILE
@given(data=enforceable_policy_data())
def test_policy_cache_ttl_controls_when_warm_reads_refresh(
    data: dict[str, Any],
) -> None:
    """A warm read refreshes immediately after the effective policy TTL."""
    _assert_policy_ttl_refreshes(data)


@seed(PROPERTY_SEED)
@SINGLE_EXAMPLE_PROFILE
@given(data=enforceable_policy_data())
def test_cache_ttl_property_breaks_if_reader_uses_fixed_default(
    data: dict[str, Any],
) -> None:
    """Negative twin: a fixed one-hour TTL must break the refresh property."""
    original = discovery._read_cache_entry

    def force_default_ttl(
        repo_ref: str,
        project_root: Path,
        ttl: int | None = None,
        *,
        expected_hash: str | None = None,
    ) -> Any:
        del ttl
        return original(
            repo_ref,
            project_root,
            ttl=discovery.DEFAULT_CACHE_TTL,
            expected_hash=expected_hash,
        )

    with patch("apm_cli.policy.discovery._read_cache_entry", side_effect=force_default_ttl):
        with pytest.raises(AssertionError, match="policy cache TTL did not trigger refresh"):
            _assert_policy_ttl_refreshes(data)


@pytest.mark.parametrize(
    ("_case_name", "path", "invalid_value"),
    INVALID_CASES,
    ids=[case[0] for case in INVALID_CASES],
)
@seed(PROPERTY_SEED)
@SINGLE_EXAMPLE_PROFILE
@given(valid_data=enforceable_policy_data())
def test_invalid_refresh_fails_closed_and_preserves_last_good_cache(
    _case_name: str,
    path: tuple[str, ...],
    invalid_value: Any,
    valid_data: dict[str, Any],
) -> None:
    """Malformed refreshes never replace valid cached policy or metadata bytes."""
    invalid_data = copy.deepcopy(valid_data)
    _set_nested(invalid_data, path, invalid_value)
    responses = [
        SimpleNamespace(status_code=200, text=_policy_yaml(valid_data), headers={}),
        SimpleNamespace(status_code=200, text=_policy_yaml(invalid_data), headers={}),
    ]

    with tempfile.TemporaryDirectory() as temp_dir:
        project_root = Path(temp_dir)
        with patch("apm_cli.policy.discovery.requests.get", side_effect=responses):
            valid = discovery._fetch_from_url(POLICY_URL, project_root)
            before = _cache_bytes(project_root)
            invalid = discovery._fetch_from_url(POLICY_URL, project_root, no_cache=True)
            after = _cache_bytes(project_root)

    assert valid.policy is not None
    assert invalid.policy is None
    assert invalid.outcome == "malformed"
    assert before == after


@seed(PROPERTY_SEED)
@SINGLE_EXAMPLE_PROFILE
@given(data=enforceable_policy_data())
def test_round_trip_property_breaks_when_serializer_omits_enforceable_field(
    data: dict[str, Any],
) -> None:
    """Negative twin: deleting one serialized field must break the property."""
    policy, _ = load_policy(_policy_yaml(data))
    original = discovery._policy_to_dict

    def omit_integrity_field(value: ApmPolicy) -> dict[str, Any]:
        serialized = original(value)
        del serialized["security"]["integrity"]["require_hashes"]
        return serialized

    with patch("apm_cli.policy.discovery._policy_to_dict", side_effect=omit_integrity_field):
        with pytest.raises(AssertionError, match="cache round-trip changed enforceable fields"):
            _assert_cache_round_trip(policy)
