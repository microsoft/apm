"""Tests for the redesigned policy cache layer.

Covers:
- Cache stores merged effective policy (not raw leaf YAML)
- Chain-version / schema-version mismatch invalidates cache
- MAX_STALE_TTL boundary: cache_stale flag at 7d - epsilon, cache_miss past 7d
- Backdated metadata triggers correct outcome
- Garbage-response path returns the right outcome
- _is_policy_empty detection
- _policy_to_dict round-trip fidelity
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
import unittest
from dataclasses import fields, is_dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from apm_cli.install.errors import PolicyViolationError
from apm_cli.models.apm_package import DependencyReference
from apm_cli.policy.discovery import (
    CACHE_SCHEMA_VERSION,
    DEFAULT_CACHE_TTL,
    MAX_STALE_TTL,
    PolicyFetchResult,  # noqa: F401
    _cache_key,
    _detect_garbage,
    _fetch_from_repo,
    _fetch_from_url,  # noqa: F401
    _get_cache_dir,
    _is_policy_empty,
    _policy_fingerprint,
    _policy_to_dict,
    _read_cache,
    _read_cache_entry,
    _serialize_policy,
    _stale_fallback_or_error,
    _write_cache,
    discover_policy_with_chain,
)
from apm_cli.policy.inheritance import merge_policies, resolve_policy_chain  # noqa: F401
from apm_cli.policy.outcome_routing import route_discovery_outcome
from apm_cli.policy.parser import load_policy
from apm_cli.policy.policy_checks import run_dependency_policy_checks
from apm_cli.policy.schema import (
    ApmPolicy,
    DependencyPolicy,
    IntegrityPolicy,
    McpPolicy,
    SecurityPolicy,
    UnmanagedFilesPolicy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_POLICY_YAML = "name: test-policy\nversion: '1.0'\nenforcement: warn\n"
POLICY_FIXTURES = Path(__file__).parents[2] / "fixtures" / "policy_url_chain"


def _make_policy(**kwargs) -> ApmPolicy:
    return ApmPolicy(**kwargs)


def _setup_cache(
    repo_ref: str,
    root: Path,
    policy: ApmPolicy,
    *,
    chain_refs: list | None = None,
    cached_at: float | None = None,
    schema_version: str = CACHE_SCHEMA_VERSION,
) -> None:
    """Write a cache entry, optionally overriding metadata fields."""
    _write_cache(repo_ref, policy, root, chain_refs=chain_refs)

    if cached_at is not None or schema_version != CACHE_SCHEMA_VERSION:
        cache_dir = _get_cache_dir(root)
        key = _cache_key(repo_ref)
        meta_file = cache_dir / f"{key}.meta.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        if cached_at is not None:
            meta["cached_at"] = cached_at
        if schema_version != CACHE_SCHEMA_VERSION:
            meta["schema_version"] = schema_version
        meta_file.write_text(json.dumps(meta), encoding="utf-8")


# ---------------------------------------------------------------------------
# Cache stores merged effective policy
# ---------------------------------------------------------------------------


class TestCacheMergedPolicy(unittest.TestCase):
    """Cache stores ApmPolicy objects (merged), not raw YAML strings."""

    def test_write_read_round_trip(self):
        """Written policy can be read back with identical semantics."""
        policy = ApmPolicy(
            name="merged-org",
            version="2.0",
            enforcement="block",
            dependencies=DependencyPolicy(
                deny=("evil/pkg", "banned/lib"),
                allow=("good/*",),
                require=("required/core",),
                require_resolution="policy-wins",
                max_depth=10,
            ),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_ref = "contoso/.github"
            _write_cache(repo_ref, policy, root, chain_refs=["hub@abc", "org@def"])

            entry = _read_cache_entry(repo_ref, root)
            self.assertIsNotNone(entry)
            self.assertFalse(entry.stale)

            p = entry.policy
            self.assertEqual(p.name, "merged-org")
            self.assertEqual(p.enforcement, "block")
            self.assertEqual(p.dependencies.deny, ("evil/pkg", "banned/lib"))
            self.assertEqual(p.dependencies.allow, ("good/*",))
            self.assertEqual(p.dependencies.require, ("required/core",))
            self.assertEqual(p.dependencies.require_resolution, "policy-wins")
            self.assertEqual(p.dependencies.max_depth, 10)
            self.assertEqual(entry.chain_refs, ["hub@abc", "org@def"])

    def test_merged_chain_stored(self):
        """resolve_policy_chain result caches correctly."""
        parent = ApmPolicy(
            name="enterprise-hub",
            enforcement="block",
            dependencies=DependencyPolicy(deny=("banned/x",)),
        )
        child = ApmPolicy(
            name="org-policy",
            enforcement="warn",
            dependencies=DependencyPolicy(deny=("local-bad/y",)),
        )
        merged = resolve_policy_chain([parent, child])

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            chain_refs = ["hub@sha1", "org@sha2"]
            _write_cache("org/.github", merged, root, chain_refs=chain_refs)

            entry = _read_cache_entry("org/.github", root)
            self.assertIsNotNone(entry)
            # Merged: enforcement escalates to 'block'; deny is union
            self.assertEqual(entry.policy.enforcement, "block")
            self.assertIn("banned/x", entry.policy.dependencies.deny)
            self.assertIn("local-bad/y", entry.policy.dependencies.deny)
            self.assertEqual(entry.chain_refs, chain_refs)


class TestURLChainCache:
    """URL inheritance persists only complete merged policies."""

    @staticmethod
    def _response(content: str, status_code: int = 200) -> MagicMock:
        response = MagicMock()
        response.status_code = status_code
        response.text = content
        response.headers = {}
        return response

    def test_complete_url_chain_persists_merged_leaf_and_reuses_cache(self, tmp_path: Path) -> None:
        leaf_url = "https://policy.example.com/leaf.yml"
        parent_url = "https://policy.example.com/parent.yml"
        leaf_yaml = (POLICY_FIXTURES / "leaf.yml").read_text(encoding="utf-8")
        parent_yaml = (POLICY_FIXTURES / "parent.yml").read_text(encoding="utf-8")
        responses = {
            leaf_url: self._response(leaf_yaml),
            parent_url: self._response(parent_yaml),
        }

        with patch(
            "apm_cli.policy.discovery.requests.get",
            side_effect=lambda url, **_kwargs: responses[url],
        ) as transport:
            cold = discover_policy_with_chain(tmp_path, policy_override=leaf_url)
            assert cold.policy is not None
            assert cold.policy.enforcement == "block"
            assert cold.policy.dependencies.deny == (
                "parent/blocked-one",
                "parent/blocked-two",
                "leaf/blocked",
            )

            entry = _read_cache_entry(leaf_url, tmp_path)
            assert entry is not None
            assert entry.policy == cold.policy
            assert (
                entry.raw_bytes_hash
                == "sha256:" + hashlib.sha256(leaf_yaml.encode("utf-8")).hexdigest()
            )

            transport.side_effect = AssertionError("warm URL lookup reached the network")
            warm = discover_policy_with_chain(tmp_path, policy_override=leaf_url)

        assert warm.cached is True
        assert warm.policy == cold.policy
        assert transport.call_count == 2

    def test_failed_url_parent_leaves_no_weak_leaf_cache(self, tmp_path: Path) -> None:
        leaf_url = "https://policy.example.com/leaf.yml"
        parent_url = "https://policy.example.com/parent.yml"
        leaf_yaml = (POLICY_FIXTURES / "leaf.yml").read_text(encoding="utf-8")
        responses = {
            leaf_url: self._response(leaf_yaml),
            parent_url: self._response("", status_code=503),
        }

        with patch(
            "apm_cli.policy.discovery.requests.get",
            side_effect=lambda url, **_kwargs: responses[url],
        ) as transport:
            first = discover_policy_with_chain(tmp_path, policy_override=leaf_url)
            assert first.outcome == "incomplete_chain"
            assert first.policy is None
            assert _read_cache_entry(leaf_url, tmp_path) is None

            second = discover_policy_with_chain(tmp_path, policy_override=leaf_url)

        assert second.outcome == "incomplete_chain"
        assert second.policy is None
        assert _read_cache_entry(leaf_url, tmp_path) is None
        assert transport.call_count == 4


# ---------------------------------------------------------------------------
# Schema / chain version mismatch invalidation
# ---------------------------------------------------------------------------


class TestCacheInvalidation(unittest.TestCase):
    """Cache entries are invalidated on schema or chain mismatch."""

    def test_schema_version_mismatch_invalidates(self):
        """Old cache with wrong schema_version returns None."""
        policy = ApmPolicy(name="old-format")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _setup_cache("test/.github", root, policy, schema_version="1")
            entry = _read_cache_entry("test/.github", root)
            self.assertIsNone(entry, "Stale schema_version should invalidate cache")

    def test_current_schema_version_accepted(self):
        """Cache with correct schema_version is accepted."""
        policy = ApmPolicy(name="current-format")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _setup_cache("test/.github", root, policy)
            entry = _read_cache_entry("test/.github", root)
            self.assertIsNotNone(entry)
            self.assertEqual(entry.policy.name, "current-format")

    def test_fingerprint_recorded(self):
        """Cache metadata includes a non-empty fingerprint."""
        policy = ApmPolicy(name="fp-test", enforcement="block")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_cache("fp/.github", policy, root)

            cache_dir = _get_cache_dir(root)
            key = _cache_key("fp/.github")
            meta = json.loads((cache_dir / f"{key}.meta.json").read_text(encoding="utf-8"))
            self.assertIn("fingerprint", meta)
            self.assertTrue(len(meta["fingerprint"]) > 0)

            # Fingerprint matches recomputed value
            serialized = _serialize_policy(policy)
            self.assertEqual(meta["fingerprint"], _policy_fingerprint(serialized))


# ---------------------------------------------------------------------------
# MAX_STALE_TTL boundary tests
# ---------------------------------------------------------------------------


class TestMaxStaleTTL(unittest.TestCase):
    """Boundary tests for the 7-day MAX_STALE_TTL."""

    def _backdate_cache(self, root: Path, repo_ref: str, age_seconds: float):
        """Set cache metadata cached_at to ``now - age_seconds``."""
        cache_dir = _get_cache_dir(root)
        key = _cache_key(repo_ref)
        meta_file = cache_dir / f"{key}.meta.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["cached_at"] = time.time() - age_seconds
        meta_file.write_text(json.dumps(meta), encoding="utf-8")

    def test_within_ttl_is_fresh(self):
        """Cache within TTL: stale=False."""
        policy = ApmPolicy(name="fresh")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_cache("ttl-test/.github", policy, root)

            entry = _read_cache_entry("ttl-test/.github", root)
            self.assertIsNotNone(entry)
            self.assertFalse(entry.stale)

    def test_past_ttl_within_max_stale_is_stale(self):
        """Cache past TTL but within MAX_STALE_TTL: stale=True, still returned."""
        policy = ApmPolicy(name="stale-ok")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_cache("stale-test/.github", policy, root)
            # Backdate to TTL + 1 hour (well within 7 days)
            self._backdate_cache(root, "stale-test/.github", DEFAULT_CACHE_TTL + 3600)

            entry = _read_cache_entry("stale-test/.github", root)
            self.assertIsNotNone(entry, "Stale cache within MAX_STALE_TTL should be returned")
            self.assertTrue(entry.stale)
            self.assertEqual(entry.policy.name, "stale-ok")

    def test_7d_minus_epsilon_returns_stale(self):
        """At 7 days minus 1 second: cache is stale but usable."""
        policy = ApmPolicy(name="boundary-ok")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_cache("boundary/.github", policy, root)
            self._backdate_cache(root, "boundary/.github", MAX_STALE_TTL - 1)

            entry = _read_cache_entry("boundary/.github", root)
            self.assertIsNotNone(entry, "Cache at 7d-1s should still be usable")
            self.assertTrue(entry.stale)

    def test_past_7d_returns_none(self):
        """At 7 days + 1 second: cache is unusable."""
        policy = ApmPolicy(name="boundary-expired")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_cache("expired/.github", policy, root)
            self._backdate_cache(root, "expired/.github", MAX_STALE_TTL + 1)

            entry = _read_cache_entry("expired/.github", root)
            self.assertIsNone(entry, "Cache past MAX_STALE_TTL should be None")

    def test_stale_cache_sets_cache_stale_flag_on_fetch_fail(self):
        """Fetch failure + stale cache -> PolicyFetchResult.cache_stale=True."""
        policy = ApmPolicy(name="stale-fallback", enforcement="block")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_cache("fallback/.github", policy, root)
            self._backdate_cache(root, "fallback/.github", DEFAULT_CACHE_TTL + 100)

            entry = _read_cache_entry("fallback/.github", root)
            self.assertIsNotNone(entry)

            # Simulate fetch failure with stale fallback
            result = _stale_fallback_or_error(
                entry, "Connection timeout", "org:fallback/.github", "cache_miss_fetch_fail"
            )
            self.assertTrue(result.found)
            self.assertTrue(result.cached)
            self.assertTrue(result.cache_stale)
            self.assertEqual(result.outcome, "cached_stale")
            self.assertEqual(result.fetch_error, "Connection timeout")
            self.assertEqual(result.policy.name, "stale-fallback")


# ---------------------------------------------------------------------------
# Backdated metadata -> correct outcome
# ---------------------------------------------------------------------------


class TestBackdatedMetaOutcomes(unittest.TestCase):
    """Backdated cache metadata triggers correct outcome classification."""

    def test_fresh_cache_outcome_found(self):
        policy = ApmPolicy(
            name="org-policy", enforcement="block", dependencies=DependencyPolicy(deny=("bad/pkg",))
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_cache("org/.github", policy, root)

            result = _read_cache("org/.github", root)
            self.assertIsNotNone(result)
            self.assertEqual(result.outcome, "found")
            self.assertFalse(result.cache_stale)

    def test_empty_policy_outcome(self):
        """Default/empty policy -> outcome='empty'."""
        policy = ApmPolicy()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_cache("empty/.github", policy, root)

            result = _read_cache("empty/.github", root)
            self.assertIsNotNone(result)
            self.assertEqual(result.outcome, "empty")

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

    @patch("apm_cli.policy.discovery._fetch_github_contents")
    def test_garbage_from_repo_no_cache(self, mock_fetch):
        """_fetch_from_repo with garbage response and no cache -> garbage_response."""
        # Return HTML pretending to be the file content
        mock_fetch.return_value = ("<html>Login Required</html>", None)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _fetch_from_repo("contoso/.github", Path(tmpdir), no_cache=True)
            self.assertEqual(result.outcome, "garbage_response")
            self.assertFalse(result.found)

    @patch("apm_cli.policy.discovery._fetch_github_contents")
    def test_garbage_from_repo_with_stale_cache(self, mock_fetch):
        """_fetch_from_repo with garbage + stale cache -> cached_stale."""
        mock_fetch.return_value = ("<html>Portal</html>", None)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Pre-populate cache, then backdate past TTL
            policy = ApmPolicy(name="cached-org", enforcement="block")
            _setup_cache(
                "contoso/.github",
                root,
                policy,
                cached_at=time.time() - DEFAULT_CACHE_TTL - 100,
            )

            result = _fetch_from_repo("contoso/.github", root, no_cache=False)
            self.assertEqual(result.outcome, "cached_stale")
            self.assertTrue(result.cache_stale)
            self.assertEqual(result.policy.name, "cached-org")


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


FIELD_COMPLETE_POLICY_YAML = """
name: field-complete
version: "9.9"
enforcement: block
fetch_failure: block
cache:
  ttl: 17
dependencies:
  allow: [acme/*]
  deny: [evil/*]
  require: [required/core]
  require_resolution: block
  max_depth: 4
  require_pinned_constraint: true
mcp:
  allow: [approved-mcp]
  deny: [blocked-mcp]
  transport:
    allow: [stdio]
  self_defined: deny
  trust_transitive: true
compilation:
  target:
    allow: [vscode]
    enforce: vscode
  strategy:
    enforce: distributed
  source_attribution: true
manifest:
  required_fields: [version, description]
  scripts: deny
  content_types:
    allow: [instructions, skill]
  require_explicit_includes: true
unmanaged_files:
  action: deny
  directories: [.github]
  exclude: [.github/generated/**]
registry_source:
  require: [corp-main]
  allow_non_registry: false
security:
  audit:
    on_install: block
    external: [skillspector]
    scanners:
      skillspector:
        allow_args: false
    fail_on_drift: true
  integrity:
    require_hashes: true
bin_deploy:
  deny_all: true
  deny: [legacy/tool]
executables:
  deny_all: true
  deny: [blocked/tool]
  require: [required/tool]
  recommend: [approved/tool]
  enforce: [mandated/tool]
"""


def _assert_policy_fields_equal(expected: object, actual: object, path: str = "policy") -> None:
    assert type(actual) is type(expected), f"{path} type changed across cache round trip"
    for field_info in fields(expected):
        field_path = f"{path}.{field_info.name}"
        expected_value = getattr(expected, field_info.name)
        actual_value = getattr(actual, field_info.name)
        if is_dataclass(expected_value):
            _assert_policy_fields_equal(expected_value, actual_value, field_path)
        else:
            assert actual_value == expected_value, f"{field_path} changed across cache round trip"


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
    declared = _dataclass_leaf_paths(ApmPolicy())
    serialized = _mapping_leaf_paths(_policy_to_dict(ApmPolicy()))
    assert serialized == declared, (
        f"cached policy shape differs from ApmPolicy: "
        f"missing={sorted(declared - serialized)}, "
        f"extra={sorted(serialized - declared)}"
    )


class TestPolicyRoundTrip(unittest.TestCase):
    """_policy_to_dict -> YAML -> load_policy preserves semantics."""

    def _round_trip(self, original: ApmPolicy) -> ApmPolicy:
        """Serialize policy to YAML, write to a temp file, read back."""
        serialized = _serialize_policy(original)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False, encoding="utf-8"
        ) as f:
            f.write(serialized)
            tmp_path = Path(f.name)
        try:
            restored, _ = load_policy(tmp_path)
            return restored
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_all_effective_policy_fields_survive_cache_round_trip(self) -> None:
        original, _warnings = load_policy(FIELD_COMPLETE_POLICY_YAML)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_cache("contoso/.github", original, root)
            entry = _read_cache_entry("contoso/.github", root)
        self.assertIsNotNone(entry)
        _assert_policy_fields_equal(original, entry.policy)

    def test_none_allow_preserved(self):
        """allow=None (no opinion) survives round-trip."""
        original = ApmPolicy(dependencies=DependencyPolicy(allow=None))
        restored = self._round_trip(original)
        self.assertIsNone(restored.dependencies.allow)

    def test_empty_allow_preserved(self):
        """allow=() (explicitly empty) survives round-trip."""
        original = ApmPolicy(dependencies=DependencyPolicy(allow=()))
        restored = self._round_trip(original)
        self.assertEqual(restored.dependencies.allow, ())

    def test_unmanaged_none_preserved(self) -> None:
        original = ApmPolicy(
            unmanaged_files=UnmanagedFilesPolicy(action=None, directories=None, exclude=None)
        )
        restored = self._round_trip(original)
        self.assertIsNone(restored.unmanaged_files.directories)
        self.assertIsNone(restored.unmanaged_files.exclude)

    def test_unmanaged_explicit_empty_preserved(self) -> None:
        original = ApmPolicy(
            unmanaged_files=UnmanagedFilesPolicy(action=None, directories=(), exclude=())
        )
        restored = self._round_trip(original)
        self.assertEqual(restored.unmanaged_files.directories, ())
        self.assertEqual(restored.unmanaged_files.exclude, ())

    def test_fingerprint_deterministic(self):
        """Same policy always produces same fingerprint."""
        policy = ApmPolicy(name="deterministic", enforcement="block")
        s1 = _serialize_policy(policy)
        s2 = _serialize_policy(policy)
        self.assertEqual(s1, s2)
        self.assertEqual(_policy_fingerprint(s1), _policy_fingerprint(s2))


class TestColdWarmEnforcementParity:
    def test_cache_only_repo_discovery_never_calls_transport(self, tmp_path: Path) -> None:
        """Offline bundle policy lookup serves stale cache or an absent result."""
        repo_ref = "contoso/.github"
        policy = ApmPolicy(
            enforcement="block",
            security=SecurityPolicy(
                integrity=IntegrityPolicy(require_hashes=True),
            ),
        )
        _write_cache(repo_ref, policy, tmp_path)
        cache_dir = _get_cache_dir(tmp_path)
        meta_path = cache_dir / f"{_cache_key(repo_ref)}.meta.json"
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        metadata["cached_at"] = time.time() - DEFAULT_CACHE_TTL - 1
        meta_path.write_text(json.dumps(metadata), encoding="utf-8")

        with patch(
            "apm_cli.policy.discovery._fetch_github_contents",
            side_effect=AssertionError("cache-only policy lookup reached transport"),
        ) as transport:
            stale = _fetch_from_repo(repo_ref, tmp_path, cache_only=True)
            missing = _fetch_from_repo("contoso/missing", tmp_path, cache_only=True)
            mismatch = _fetch_from_repo(
                repo_ref,
                tmp_path,
                cache_only=True,
                expected_hash="sha256:" + "0" * 64,
            )

        assert transport.call_count == 0
        assert stale.outcome == "cached_stale"
        assert stale.cached is True
        assert stale.policy == policy
        assert missing.outcome == "absent"
        assert missing.policy is None
        assert mismatch.outcome == "hash_mismatch"
        assert mismatch.policy is None

        (tmp_path / "apm.yml").write_text(
            "name: invalid-pin\npolicy:\n  hash: not-a-digest\n",
            encoding="utf-8",
        )
        malformed = discover_policy_with_chain(tmp_path, cache_only=True)
        assert malformed.outcome == "hash_mismatch"
        assert malformed.policy is None

        unrelated_root = tmp_path / "unrelated-cache"
        unrelated_root.mkdir()
        _write_cache("other/.github", policy, unrelated_root)
        unrelated = discover_policy_with_chain(
            unrelated_root,
            policy_override=repo_ref,
            expected_hash="sha256:" + "0" * 64,
            cache_only=True,
        )
        assert unrelated.outcome == "hash_mismatch"
        assert unrelated.policy is None

        local_root = tmp_path / "local-override"
        local_root.mkdir()
        local_policy = local_root / "apm-policy.yml"
        local_policy.write_text(
            "name: local\n"
            "enforcement: block\n"
            "security:\n"
            "  integrity:\n"
            "    require_hashes: true\n",
            encoding="utf-8",
        )
        local = discover_policy_with_chain(
            local_root,
            policy_override=str(local_policy),
            cache_only=True,
        )
        assert local.outcome == "found"
        assert local.cached is False

    def test_merged_strict_policy_denials_survive_warm_cache(self, tmp_path: Path) -> None:
        payloads = {
            "contoso/.github": """
name: leaf
extends: hub/.github
enforcement: block
dependencies:
  deny: [leaf/deny]
""",
            "hub/.github": """
name: strict-parent
enforcement: block
dependencies:
  require_pinned_constraint: true
manifest:
  require_explicit_includes: true
registry_source:
  allow_non_registry: false
security:
  integrity:
    require_hashes: true
""",
        }

        def fetch(repo_ref: str, policy_path: str) -> tuple[str | None, str | None]:
            assert policy_path == "apm-policy.yml"
            return payloads[repo_ref], None

        with patch(
            "apm_cli.policy.discovery._fetch_github_contents", side_effect=fetch
        ) as transport:
            cold = discover_policy_with_chain(tmp_path, policy_override="contoso/.github")
            assert cold.policy is not None
            assert cold.cached is False
            assert transport.call_count == 2
            transport.side_effect = AssertionError("network fetch attempted on warm cache hit")
            warm = discover_policy_with_chain(tmp_path, policy_override="contoso/.github")

        assert warm.policy is not None
        assert warm.cached is True
        dependency = DependencyReference.parse("acme/lib#main")

        def failed_checks(policy: ApmPolicy) -> set[str]:
            result = run_dependency_policy_checks(
                [dependency],
                policy=policy,
                fail_fast=False,
                manifest_includes="auto",
                registries={},
            )
            return {check.name for check in result.checks if not check.passed}

        cold_failures = failed_checks(cold.policy)
        warm_failures = failed_checks(warm.policy)
        assert warm_failures == cold_failures, (
            "policy enforcement changed after cache warm-up: "
            f"cold={sorted(cold_failures)}, warm={sorted(warm_failures)}"
        )
        assert warm_failures == {
            "dependency-pinned-constraint",
            "explicit-includes",
            "registry-source",
        }
        assert warm.policy == cold.policy


@pytest.mark.parametrize(
    "parent_ref",
    [
        "platform/.github",
        "ghes.contoso.com/platform/.github",
    ],
)
def test_ghes_chain_preserves_backend_for_shorthand_and_explicit_parent(
    tmp_path: Path,
    parent_ref: str,
) -> None:
    leaf_yaml = f"""
name: child
extends: {parent_ref}
dependencies:
  deny: [child/deny]
"""
    parent_yaml = """
name: strict-parent
enforcement: block
dependencies:
  require_pinned_constraint: true
"""
    expected_paths = {
        "/api/v3/repos/team/.github/contents/apm-policy.yml": leaf_yaml,
        "/api/v3/repos/platform/.github/contents/apm-policy.yml": parent_yaml,
    }
    observed_urls: list[str] = []

    def get(url: str, **_kwargs) -> MagicMock:
        parsed = urlparse(url)
        assert parsed.hostname == "ghes.contoso.com"
        assert parsed.path in expected_paths
        observed_urls.append(url)
        response = MagicMock()
        response.status_code = 200
        response.headers = {}
        response.json.return_value = {"content": expected_paths[parsed.path]}
        return response

    with (
        patch("apm_cli.policy.discovery._get_token_for_host", return_value=None),
        patch("apm_cli.policy.discovery.requests.get", side_effect=get) as transport,
    ):
        cold = discover_policy_with_chain(
            tmp_path,
            policy_override="ghes.contoso.com/team/.github",
        )
        assert cold.policy is not None
        assert cold.policy.enforcement == "block"
        assert cold.policy.dependencies.require_pinned_constraint is True
        assert cold.cached is False
        assert transport.call_count == 2

        transport.side_effect = AssertionError("network fetch attempted on warm cache hit")
        warm = discover_policy_with_chain(
            tmp_path,
            policy_override="ghes.contoso.com/team/.github",
        )

    assert {(urlparse(url).hostname, urlparse(url).path) for url in observed_urls} == {
        ("ghes.contoso.com", path) for path in expected_paths
    }
    assert warm.cached is True
    assert warm.policy == cold.policy
    cache_entry = _read_cache_entry("ghes.contoso.com/team/.github", tmp_path)
    assert cache_entry is not None
    assert cache_entry.chain_refs == [
        "ghes.contoso.com/platform/.github",
        "ghes.contoso.com/team/.github",
    ]


def test_ghes_org_sentinel_uses_project_remote_host_and_warm_cache(
    tmp_path: Path,
) -> None:
    leaf_ref = "ghes.contoso.com/team/policy"
    leaf_yaml = """
name: team-policy
extends: org
dependencies:
  deny: [team/blocked]
"""
    parent_yaml = """
name: platform-policy
enforcement: block
dependencies:
  require_pinned_constraint: true
"""
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "remote",
            "add",
            "origin",
            "https://ghes.contoso.com/platform/application.git",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    observed_urls: list[str] = []
    expected_paths = [
        "/api/v3/repos/team/policy/contents/apm-policy.yml",
        "/api/v3/repos/platform/.github-private/contents/apm-policy.yml",
        "/api/v3/repos/platform/.github/contents/apm-policy.yml",
    ]

    def get(url: str, **_kwargs) -> MagicMock:
        parsed = urlparse(url)
        assert parsed.hostname == "ghes.contoso.com"
        assert parsed.path == expected_paths[len(observed_urls)]
        observed_urls.append(url)
        response = MagicMock()
        response.headers = {}
        if parsed.path == expected_paths[1]:
            response.status_code = 404
        else:
            response.status_code = 200
            response.json.return_value = {
                "content": leaf_yaml if parsed.path == expected_paths[0] else parent_yaml
            }
        return response

    with (
        patch("apm_cli.policy.discovery._get_token_for_host", return_value=None),
        patch("apm_cli.policy.discovery.requests.get", side_effect=get) as transport,
    ):
        cold = discover_policy_with_chain(tmp_path, policy_override=leaf_ref)
        assert cold.policy is not None
        assert cold.policy.enforcement == "block"
        assert cold.policy.dependencies.deny == ("team/blocked",)
        assert cold.policy.dependencies.require_pinned_constraint is True
        assert transport.call_count == 3

        transport.side_effect = AssertionError("network fetch attempted on warm cache hit")
        warm = discover_policy_with_chain(tmp_path, policy_override=leaf_ref)

    assert [(urlparse(url).hostname, urlparse(url).path) for url in observed_urls] == [
        ("ghes.contoso.com", path) for path in expected_paths
    ]
    assert warm.cached is True
    assert warm.policy == cold.policy
    cache_entry = _read_cache_entry(leaf_ref, tmp_path)
    assert cache_entry is not None
    assert cache_entry.chain_refs == [
        "ghes.contoso.com/platform/.github",
        leaf_ref,
    ]


@pytest.mark.parametrize(
    ("parent_ref", "expected_parent"),
    [
        (
            "dev.azure.com/contoso/governance/policy",
            ("contoso", "governance", "policy", "dev.azure.com"),
        ),
        (
            "governance/policy",
            ("contoso", "governance", "policy", "dev.azure.com"),
        ),
    ],
)
def test_ado_chain_preserves_backend_for_explicit_and_same_org_parent(
    tmp_path: Path,
    parent_ref: str,
    expected_parent: tuple[str, str, str, str],
) -> None:
    leaf_yaml = f"name: child\nextends: {parent_ref}\n"
    parent_yaml = (
        "name: strict-parent\n"
        "enforcement: block\n"
        "dependencies:\n"
        "  require_pinned_constraint: true\n"
    )
    observed: list[tuple[str, str, str, str]] = []

    def fetch_ado(
        org: str,
        project: str,
        repo: str,
        policy_path: str,
        *,
        host: str,
    ) -> tuple[str | None, str | None]:
        assert policy_path == "apm-policy.yml"
        key = (org, project, repo, host)
        observed.append(key)
        if key == ("contoso", "_apm", "_apm", "dev.azure.com"):
            return leaf_yaml, None
        assert key == expected_parent
        return parent_yaml, None

    with (
        patch(
            "apm_cli.policy.discovery._extract_org_from_git_remote",
            return_value=("contoso", "dev.azure.com"),
        ),
        patch("apm_cli.policy.discovery._fetch_ado_contents", side_effect=fetch_ado) as ado,
        patch(
            "apm_cli.policy.discovery._fetch_github_contents",
            side_effect=AssertionError("ADO policy routed through GitHub Contents API"),
        ) as github,
    ):
        cold = discover_policy_with_chain(tmp_path)
        assert cold.source == "org:dev.azure.com/contoso/_apm/_apm"
        assert cold.policy is not None
        assert cold.policy.enforcement == "block"
        assert cold.policy.dependencies.require_pinned_constraint is True
        assert ado.call_count == 2
        github.assert_not_called()

        ado.side_effect = AssertionError("network fetch attempted on warm cache hit")
        warm = discover_policy_with_chain(tmp_path)

    assert observed == [
        ("contoso", "_apm", "_apm", "dev.azure.com"),
        expected_parent,
    ]
    assert warm.cached is True
    assert warm.policy == cold.policy
    cache_entry = _read_cache_entry("dev.azure.com/contoso/_apm/_apm", tmp_path)
    assert cache_entry is not None
    assert cache_entry.chain_refs == [
        "dev.azure.com/contoso/governance/policy",
        "dev.azure.com/contoso/_apm/_apm",
    ]


@pytest.mark.parametrize("fetch_failure", ["block", "warn"])
def test_stale_parent_propagates_chain_outcome_without_fresh_leaf_cache(
    tmp_path: Path,
    fetch_failure: str,
) -> None:
    leaf_ref = "child/.github"
    parent_ref = "parent/.github"
    leaf_yaml = f"name: child\nextends: {parent_ref}\n"
    parent = ApmPolicy(
        name="stale-parent",
        enforcement="block",
        fetch_failure=fetch_failure,
        dependencies=DependencyPolicy(require_pinned_constraint=True),
    )
    stale_age = DEFAULT_CACHE_TTL + 100
    _setup_cache(
        parent_ref,
        tmp_path,
        parent,
        cached_at=time.time() - stale_age,
    )

    def fetch(repo_ref: str, policy_path: str) -> tuple[str | None, str | None]:
        assert policy_path == "apm-policy.yml"
        if repo_ref == leaf_ref:
            return leaf_yaml, None
        assert repo_ref == parent_ref
        return None, "503: parent refresh unavailable"

    with patch("apm_cli.policy.discovery._fetch_github_contents", side_effect=fetch):
        result = discover_policy_with_chain(tmp_path, policy_override=leaf_ref)

    assert result.outcome == "cached_stale"
    assert result.policy is not None
    assert result.policy.enforcement == "block"
    assert result.policy.dependencies.require_pinned_constraint is True
    assert result.cached is True
    assert result.cache_stale is True
    assert result.cache_age_seconds is not None
    assert result.cache_age_seconds >= stale_age
    assert result.fetch_error == "503: parent refresh unavailable"
    assert _read_cache_entry(leaf_ref, tmp_path) is None

    logger = MagicMock()
    if fetch_failure == "block":
        with pytest.raises(
            PolicyViolationError, match="cached policy declares fetch_failure=block"
        ):
            route_discovery_outcome(
                result,
                logger=logger,
                fetch_failure_default="warn",
            )
    else:
        routed = route_discovery_outcome(
            result,
            logger=logger,
            fetch_failure_default="warn",
        )
        assert routed == result.policy
        logger.policy_resolved.assert_called_once()
        logger.policy_discovery_miss.assert_called_once_with(
            outcome="cached_stale",
            source=result.source,
            error="503: parent refresh unavailable",
        )


def test_nearest_stale_ancestor_metadata_wins_without_fresh_leaf_cache(
    tmp_path: Path,
) -> None:
    leaf_ref = "child/.github"
    nearest_ref = "parent/.github"
    farthest_ref = "root/.github"
    leaf_yaml = f"""
name: child
extends: {nearest_ref}
dependencies:
  deny: [child/blocked]
"""
    nearest = ApmPolicy(
        name="parent",
        extends=farthest_ref,
        dependencies=DependencyPolicy(deny=("parent/blocked",)),
    )
    farthest = ApmPolicy(
        name="root",
        enforcement="block",
        dependencies=DependencyPolicy(deny=("root/blocked",)),
    )
    fixed_time = 2_000_000_000.0
    nearest_age = DEFAULT_CACHE_TTL + 111
    farthest_age = DEFAULT_CACHE_TTL + 999

    def fetch(repo_ref: str, policy_path: str) -> tuple[str | None, str | None]:
        assert policy_path == "apm-policy.yml"
        if repo_ref == leaf_ref:
            return leaf_yaml, None
        if repo_ref == nearest_ref:
            return None, "503: nearest parent refresh unavailable"
        assert repo_ref == farthest_ref
        return None, "503: farthest parent refresh unavailable"

    with patch("apm_cli.policy.discovery.time.time", return_value=fixed_time):
        _setup_cache(
            nearest_ref,
            tmp_path,
            nearest,
            cached_at=fixed_time - nearest_age,
        )
        _setup_cache(
            farthest_ref,
            tmp_path,
            farthest,
            cached_at=fixed_time - farthest_age,
        )
        with patch("apm_cli.policy.discovery._fetch_github_contents", side_effect=fetch):
            result = discover_policy_with_chain(tmp_path, policy_override=leaf_ref)

    assert result.outcome == "cached_stale"
    assert result.policy is not None
    assert result.policy.enforcement == "block"
    assert result.policy.dependencies.deny == (
        "root/blocked",
        "parent/blocked",
        "child/blocked",
    )
    assert result.fetch_error == "503: nearest parent refresh unavailable"
    assert result.cache_age_seconds == nearest_age
    assert result.cached is True
    assert result.cache_stale is True
    assert _read_cache_entry(leaf_ref, tmp_path) is None


def test_farther_failure_overrides_nearest_stale_ancestor(
    tmp_path: Path,
) -> None:
    leaf_ref = "child/.github"
    nearest_ref = "parent/.github"
    farthest_ref = "root/.github"
    leaf_yaml = f"name: child\nextends: {nearest_ref}\n"
    nearest = ApmPolicy(name="parent", extends=farthest_ref, enforcement="block")
    fixed_time = 2_000_000_000.0

    def fetch(repo_ref: str, policy_path: str) -> tuple[str | None, str | None]:
        assert policy_path == "apm-policy.yml"
        if repo_ref == leaf_ref:
            return leaf_yaml, None
        if repo_ref == nearest_ref:
            return None, "503: nearest parent refresh unavailable"
        assert repo_ref == farthest_ref
        return None, "503: root unavailable"

    with patch("apm_cli.policy.discovery.time.time", return_value=fixed_time):
        _setup_cache(
            nearest_ref,
            tmp_path,
            nearest,
            cached_at=fixed_time - DEFAULT_CACHE_TTL - 111,
        )
        with patch("apm_cli.policy.discovery._fetch_github_contents", side_effect=fetch):
            result = discover_policy_with_chain(tmp_path, policy_override=leaf_ref)

    assert result.outcome == "incomplete_chain"
    assert result.policy is None
    assert result.fetch_error is None
    assert result.cached is False
    assert result.cache_stale is False
    assert _read_cache_entry(leaf_ref, tmp_path) is None


def test_incomplete_chain_does_not_leave_weak_leaf_cache(tmp_path: Path) -> None:
    leaf_yaml = """
name: child
extends: parent/.github
dependencies:
  deny: [child/deny]
"""

    def fetch(repo_ref: str, policy_path: str) -> tuple[str | None, str | None]:
        assert policy_path == "apm-policy.yml"
        if repo_ref == "child/.github":
            return leaf_yaml, None
        assert repo_ref == "parent/.github"
        return None, "503: parent unavailable"

    with patch("apm_cli.policy.discovery._fetch_github_contents", side_effect=fetch) as transport:
        first = discover_policy_with_chain(tmp_path, policy_override="child/.github")
        assert first.outcome == "incomplete_chain"
        assert first.policy is None
        assert _read_cache_entry("child/.github", tmp_path) is None
        second = discover_policy_with_chain(tmp_path, policy_override="child/.github")
    assert second.outcome == "incomplete_chain"
    assert second.policy is None
    assert transport.call_count == 4


def test_extends_only_leaf_resolves_strict_parent(tmp_path: Path) -> None:
    payloads = {
        "child/.github": "name: child\nextends: parent/.github\n",
        "parent/.github": ("name: parent\ndependencies:\n  require_pinned_constraint: true\n"),
    }

    def fetch(repo_ref: str, policy_path: str) -> tuple[str | None, str | None]:
        assert policy_path == "apm-policy.yml"
        return payloads[repo_ref], None

    with patch("apm_cli.policy.discovery._fetch_github_contents", side_effect=fetch) as transport:
        result = discover_policy_with_chain(tmp_path, policy_override="child/.github")
    assert result.outcome == "found"
    assert result.policy is not None
    assert result.policy.dependencies.require_pinned_constraint is True
    assert transport.call_count == 2


if __name__ == "__main__":
    unittest.main()
