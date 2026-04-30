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

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch  # noqa: F401

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
    _policy_to_dict,  # noqa: F401
    _read_cache,
    _read_cache_entry,
    _serialize_policy,
    _stale_fallback_or_error,
    _write_cache,
)
from apm_cli.policy.inheritance import merge_policies, resolve_policy_chain  # noqa: F401
from apm_cli.policy.parser import load_policy
from apm_cli.policy.schema import (
    ApmPolicy,
    DependencyPolicy,
    McpPolicy,
    McpTransportPolicy,
    UnmanagedFilesPolicy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_POLICY_YAML = "name: test-policy\nversion: '1.0'\nenforcement: warn\n"


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


# ---------------------------------------------------------------------------
# _policy_to_dict round-trip
# ---------------------------------------------------------------------------


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

    def test_full_policy_round_trip(self):
        original = ApmPolicy(
            name="full-test",
            version="3.0",
            enforcement="block",
            dependencies=DependencyPolicy(
                allow=("org/*", "approved/lib"),
                deny=("banned/evil",),
                require=("required/std",),
                require_resolution="policy-wins",
                max_depth=5,
            ),
            mcp=McpPolicy(
                allow=("mcp-good",),
                deny=("mcp-bad",),
                transport=McpTransportPolicy(allow=("stdio", "sse")),
                self_defined="deny",
                trust_transitive=False,
            ),
        )
        restored = self._round_trip(original)

        self.assertEqual(restored.name, original.name)
        self.assertEqual(restored.enforcement, original.enforcement)
        self.assertEqual(restored.dependencies.deny, original.dependencies.deny)
        self.assertEqual(restored.dependencies.allow, original.dependencies.allow)
        self.assertEqual(restored.dependencies.require, original.dependencies.require)
        self.assertEqual(
            restored.dependencies.require_resolution,
            original.dependencies.require_resolution,
        )
        self.assertEqual(restored.dependencies.max_depth, original.dependencies.max_depth)
        self.assertEqual(restored.mcp.deny, original.mcp.deny)
        self.assertEqual(restored.mcp.allow, original.mcp.allow)
        self.assertEqual(restored.mcp.transport.allow, original.mcp.transport.allow)
        self.assertEqual(restored.mcp.self_defined, original.mcp.self_defined)
        self.assertEqual(restored.mcp.trust_transitive, original.mcp.trust_transitive)

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

    def test_fingerprint_deterministic(self):
        """Same policy always produces same fingerprint."""
        policy = ApmPolicy(name="deterministic", enforcement="block")
        s1 = _serialize_policy(policy)
        s2 = _serialize_policy(policy)
        self.assertEqual(s1, s2)
        self.assertEqual(_policy_fingerprint(s1), _policy_fingerprint(s2))


if __name__ == "__main__":
    unittest.main()
