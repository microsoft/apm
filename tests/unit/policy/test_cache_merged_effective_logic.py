"""Tests for the redesigned policy cache layer (pure logic cases)."""

from __future__ import annotations

import unittest
from dataclasses import fields, is_dataclass

import pytest

from apm_cli.policy.discovery import (
    DEFAULT_CACHE_TTL,
    PolicyFetchResult,  # noqa: F401
    _detect_garbage,
    _fetch_from_url,  # noqa: F401
    _is_policy_empty,
    _policy_to_dict,
    _stale_fallback_or_error,
)
from apm_cli.policy.inheritance import merge_policies, resolve_policy_chain  # noqa: F401
from apm_cli.policy.parser import load_policy
from apm_cli.policy.schema import (
    ApmPolicy,
    BinDeployPolicy,
    DependencyPolicy,
    McpPolicy,
    UnmanagedFilesPolicy,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cache stores merged effective policy
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Schema / chain version mismatch invalidation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MAX_STALE_TTL boundary tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Backdated metadata -> correct outcome
# ---------------------------------------------------------------------------


class TestBackdatedMetaOutcomes(unittest.TestCase):
    """Backdated cache metadata triggers correct outcome classification."""

    def test_no_cache_fallback_outcome(self):
        """No cache + fetch error -> cache_miss_fetch_fail."""
        result = _stale_fallback_or_error(
            None, "Network down", "org:test/.github", "cache_miss_fetch_fail"
        )
        self.assertFalse(result.found)
        self.assertEqual(result.outcome, "cache_miss_fetch_fail")
        self.assertIsNotNone(result.error)


# ---------------------------------------------------------------------------
# Garbage-response detection
# ---------------------------------------------------------------------------


class TestGarbageResponse(unittest.TestCase):
    """Garbage-response detection: 200 OK with non-YAML body."""

    def test_html_garbage_no_cache(self):
        """HTML body without stale cache -> garbage_response outcome."""
        html_body = "<html><body>Sign in to continue</body></html>"
        result = _detect_garbage(html_body, "example.com/org/.github", "org:org/.github", None)
        self.assertIsNotNone(result)
        self.assertEqual(result.outcome, "garbage_response")
        # HTML parses as a YAML string (not a mapping), so error says "not a YAML mapping"
        self.assertIn("not a YAML mapping", result.error)

    def test_yaml_list_garbage_no_cache(self):
        """YAML list (not mapping) without cache -> garbage_response."""
        yaml_list = "- item1\n- item2\n"
        result = _detect_garbage(yaml_list, "test-ref", "org:test-ref", None)
        self.assertIsNotNone(result)
        self.assertEqual(result.outcome, "garbage_response")
        self.assertIn("not a YAML mapping", result.error)

    def test_html_garbage_with_stale_cache(self):
        """HTML body with stale cache -> cached_stale outcome (fallback)."""
        from apm_cli.policy.discovery import _CacheEntry

        stale_entry = _CacheEntry(
            policy=ApmPolicy(name="stale-policy"),
            source="org:org/.github",
            age_seconds=DEFAULT_CACHE_TTL + 100,
            stale=True,
            chain_refs=["org/.github"],
            fingerprint="abc",
        )
        html_body = "<html><body>captive portal</body></html>"
        result = _detect_garbage(html_body, "org/.github", "org:org/.github", stale_entry)
        self.assertIsNotNone(result)
        self.assertEqual(result.outcome, "cached_stale")
        self.assertTrue(result.cache_stale)
        self.assertEqual(result.policy.name, "stale-policy")

    def test_valid_yaml_not_garbage(self):
        """Valid YAML mapping -> _detect_garbage returns None (not garbage)."""
        valid = "name: test\nenforcement: warn\n"
        result = _detect_garbage(valid, "test-ref", "org:test-ref", None)
        self.assertIsNone(result)

    def test_empty_yaml_not_garbage(self):
        """Empty YAML (None after parse) -> not garbage (becomes empty policy)."""
        result = _detect_garbage("", "test-ref", "org:test-ref", None)
        self.assertIsNone(result)

    def test_none_content_not_garbage(self):
        """None content -> not garbage (caller handles as absent)."""
        result = _detect_garbage(None, "test-ref", "org:test-ref", None)
        self.assertIsNone(result)

    def test_truly_invalid_yaml_no_cache(self):
        """Content that fails YAML parse entirely -> garbage_response."""
        # Tabs in wrong places cause YAML parse errors
        bad_yaml = ":\n\t\t: :\n{{{invalid"
        result = _detect_garbage(bad_yaml, "bad-ref", "org:bad-ref", None)
        self.assertIsNotNone(result)
        self.assertEqual(result.outcome, "garbage_response")
        self.assertIn("not valid YAML", result.error)
        self.assertIn("captive portal", result.error)


# ---------------------------------------------------------------------------
# _is_policy_empty
# ---------------------------------------------------------------------------


class TestIsPolicyEmpty(unittest.TestCase):
    """_is_policy_empty correctly identifies empty/non-empty policies."""

    def test_default_policy_is_empty(self):
        self.assertTrue(_is_policy_empty(ApmPolicy()))

    def test_named_default_is_empty(self):
        """A policy with only name/version but no rules is still empty."""
        self.assertTrue(_is_policy_empty(ApmPolicy(name="my-org", version="1.0")))

    def test_deny_list_not_empty(self):
        p = ApmPolicy(dependencies=DependencyPolicy(deny=("evil/pkg",)))
        self.assertFalse(_is_policy_empty(p))

    def test_allow_list_not_empty(self):
        p = ApmPolicy(dependencies=DependencyPolicy(allow=("good/*",)))
        self.assertFalse(_is_policy_empty(p))

    def test_require_list_not_empty(self):
        p = ApmPolicy(dependencies=DependencyPolicy(require=("needed/lib",)))
        self.assertFalse(_is_policy_empty(p))

    def test_mcp_deny_not_empty(self):
        p = ApmPolicy(mcp=McpPolicy(deny=("bad-mcp",)))
        self.assertFalse(_is_policy_empty(p))

    def test_unmanaged_files_warn_not_empty(self):
        p = ApmPolicy(unmanaged_files=UnmanagedFilesPolicy(action="warn"))
        self.assertFalse(_is_policy_empty(p))

    def test_enforcement_block_still_empty_if_no_rules(self):
        """enforcement='block' alone doesn't make a policy non-empty."""
        p = ApmPolicy(enforcement="block")
        self.assertTrue(_is_policy_empty(p))


ACTIONABLE_POLICY_CASES = {
    ("fetch_failure",): "fetch_failure: block\n",
    ("dependencies", "allow"): "dependencies:\n  allow: [acme/*]\n",
    ("dependencies", "deny"): "dependencies:\n  deny: [evil/*]\n",
    ("dependencies", "require"): "dependencies:\n  require: [corp/base]\n",
    (
        "dependencies",
        "require_resolution",
    ): "dependencies:\n  require_resolution: block\n",
    ("dependencies", "max_depth"): "dependencies:\n  max_depth: 1\n",
    (
        "dependencies",
        "require_pinned_constraint",
    ): "dependencies:\n  require_pinned_constraint: true\n",
    ("mcp", "allow"): "mcp:\n  allow: [corp-mcp]\n",
    ("mcp", "deny"): "mcp:\n  deny: [bad-mcp]\n",
    ("mcp", "transport", "allow"): "mcp:\n  transport:\n    allow: [stdio]\n",
    ("mcp", "self_defined"): "mcp:\n  self_defined: deny\n",
    ("mcp", "trust_transitive"): "mcp:\n  trust_transitive: true\n",
    (
        "compilation",
        "target",
        "allow",
    ): "compilation:\n  target:\n    allow: [vscode]\n",
    (
        "compilation",
        "target",
        "enforce",
    ): "compilation:\n  target:\n    enforce: vscode\n",
    (
        "compilation",
        "strategy",
        "enforce",
    ): "compilation:\n  strategy:\n    enforce: distributed\n",
    (
        "compilation",
        "source_attribution",
    ): "compilation:\n  source_attribution: true\n",
    ("manifest", "required_fields"): "manifest:\n  required_fields: [version]\n",
    ("manifest", "scripts"): "manifest:\n  scripts: deny\n",
    (
        "manifest",
        "content_types",
    ): "manifest:\n  content_types:\n    allow: [skill]\n",
    (
        "manifest",
        "require_explicit_includes",
    ): "manifest:\n  require_explicit_includes: true\n",
    ("unmanaged_files", "action"): "unmanaged_files:\n  action: deny\n",
    (
        "unmanaged_files",
        "directories",
    ): "unmanaged_files:\n  directories: [.github]\n",
    (
        "unmanaged_files",
        "exclude",
    ): "unmanaged_files:\n  exclude: [.github/generated/**]\n",
    ("registry_source", "require"): "registry_source:\n  require: [corp]\n",
    (
        "registry_source",
        "allow_non_registry",
    ): "registry_source:\n  allow_non_registry: false\n",
    (
        "security",
        "audit",
        "on_install",
    ): "security:\n  audit:\n    on_install: block\n",
    (
        "security",
        "audit",
        "external",
    ): "security:\n  audit:\n    external: [skillspector]\n",
    (
        "security",
        "audit",
        "scanners",
    ): "security:\n  audit:\n    scanners:\n      skillspector:\n        allow_args: false\n",
    (
        "security",
        "audit",
        "fail_on_drift",
    ): "security:\n  audit:\n    fail_on_drift: true\n",
    (
        "security",
        "integrity",
        "require_hashes",
    ): "security:\n  integrity:\n    require_hashes: true\n",
    ("bin_deploy", "deny_all"): "bin_deploy:\n  deny_all: true\n",
    ("bin_deploy", "deny"): "bin_deploy:\n  deny: [legacy/tool]\n",
    ("executables", "deny_all"): "executables:\n  deny_all: true\n",
    ("executables", "deny"): "executables:\n  deny: [blocked/tool]\n",
    ("executables", "require"): "executables:\n  require: [required/tool]\n",
    ("executables", "recommend"): "executables:\n  recommend: [approved/tool]\n",
    ("executables", "enforce"): "executables:\n  enforce: [mandated/tool]\n",
}

NON_ACTIONABLE_POLICY_LEAVES = {
    ("name",),
    ("version",),
    ("extends",),
    ("enforcement",),
    ("cache", "ttl"),
}


def test_policy_empty_cases_cover_every_actionable_dataclass_leaf() -> None:
    declared = _dataclass_leaf_paths(ApmPolicy())
    assert set(ACTIONABLE_POLICY_CASES) == declared - NON_ACTIONABLE_POLICY_LEAVES


@pytest.mark.parametrize(
    ("field_path", "policy_yaml"),
    [
        pytest.param(field_path, policy_yaml, id=".".join(field_path))
        for field_path, policy_yaml in ACTIONABLE_POLICY_CASES.items()
    ],
)
def test_every_actionable_policy_leaf_is_not_empty(
    field_path: tuple[str, ...], policy_yaml: str
) -> None:
    policy, _warnings = load_policy("name: actionable-leaf\n" + policy_yaml)
    assert not _is_policy_empty(policy)


# ---------------------------------------------------------------------------
# _policy_to_dict round-trip
# ---------------------------------------------------------------------------


def _dataclass_leaf_paths(value: object, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()
    for field_info in fields(value):
        field_path = (*prefix, field_info.name)
        child = getattr(value, field_info.name)
        if is_dataclass(child):
            paths.update(_dataclass_leaf_paths(child, field_path))
        else:
            paths.add(field_path)
    return paths


def _mapping_leaf_paths(value: object, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    if not isinstance(value, dict) or not value:
        return {prefix}
    paths: set[tuple[str, ...]] = set()
    for key, child in value.items():
        child_path = (*prefix, key)
        if isinstance(child, dict) and child:
            paths.update(_mapping_leaf_paths(child, child_path))
        else:
            paths.add(child_path)
    return paths


def test_policy_serializer_covers_every_dataclass_leaf() -> None:
    policy = ApmPolicy(
        bin_deploy=BinDeployPolicy(
            deny_all=True,
            deny=("legacy/tool",),
        )
    )
    declared = _dataclass_leaf_paths(policy)
    serialized = _mapping_leaf_paths(_policy_to_dict(policy))
    assert serialized == declared, (
        f"cached policy shape differs from ApmPolicy: "
        f"missing={sorted(declared - serialized)}, "
        f"extra={sorted(serialized - declared)}"
    )


@pytest.mark.parametrize(
    ("bin_deploy", "expected"),
    [
        (BinDeployPolicy(deny_all=True), {"deny_all": True, "deny": []}),
        (BinDeployPolicy(deny=("legacy/tool",)), {"deny_all": False, "deny": ["legacy/tool"]}),
    ],
)
def test_policy_serializer_preserves_sparse_bin_deploy_shapes(
    bin_deploy: BinDeployPolicy, expected: dict[str, object]
) -> None:
    assert _policy_to_dict(ApmPolicy(bin_deploy=bin_deploy))["bin_deploy"] == expected


if __name__ == "__main__":
    unittest.main()
