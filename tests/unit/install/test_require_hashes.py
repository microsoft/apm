"""Seam 3 (part A): fail-closed enforcement for ``require_hashes``.

A lockfile entry whose ``content_hash`` is missing or empty must be treated
as a FAILURE when ``security.integrity.require_hashes`` is on -- never a silent
pass. ``unhashed_dependencies`` is the pure decision function; it is wired into
the install pipeline so a hashless non-local entry stops the install.

Local deps are exempt: they are verified via ``deployed_file_hashes`` rather
than a package ``content_hash``, mirroring the existing
``registry_proxy.find_missing_hashes`` rule.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

from apm_cli.deps.lockfile import LockedDependency
from apm_cli.install.integrity import enforce_require_hashes, unhashed_dependencies


def _dep(name: str, content_hash, source=None) -> LockedDependency:
    return LockedDependency(
        repo_url=f"https://example.com/{name}.git",
        resolved_commit="a" * 40,
        content_hash=content_hash,
        source=source,
    )


class TestUnhashedDependencies(unittest.TestCase):
    def test_missing_hash_is_flagged(self):
        deps = [_dep("a", None)]
        flagged = unhashed_dependencies(deps)
        self.assertEqual([d.repo_url for d in flagged], ["https://example.com/a.git"])

    def test_empty_hash_is_flagged(self):
        deps = [_dep("a", "")]
        self.assertEqual(len(unhashed_dependencies(deps)), 1)

    def test_present_hash_passes(self):
        deps = [_dep("a", "sha256:" + "0" * 64)]
        self.assertEqual(unhashed_dependencies(deps), [])

    def test_local_dep_exempt(self):
        deps = [_dep("a", None, source="local")]
        self.assertEqual(unhashed_dependencies(deps), [])

    def test_mixed(self):
        deps = [
            _dep("good", "sha256:abc"),
            _dep("bad", None),
            _dep("local", None, source="local"),
            _dep("empty", ""),
        ]
        flagged = {d.repo_url for d in unhashed_dependencies(deps)}
        self.assertIn("https://example.com/bad.git", flagged)
        self.assertIn("https://example.com/empty.git", flagged)
        self.assertNotIn("https://example.com/good.git", flagged)
        self.assertNotIn("https://example.com/local.git", flagged)


class TestEnforceRequireHashes(unittest.TestCase):
    """The enforcement wrapper fails closed only when enabled."""

    def test_disabled_is_noop_even_with_missing_hash(self):
        # Default-off must preserve current behavior: no raise.
        enforce_require_hashes([_dep("a", None)], enabled=False)

    def test_enabled_with_all_hashed_passes(self):
        enforce_require_hashes([_dep("a", "sha256:abc")], enabled=True)

    def test_enabled_with_missing_hash_fails_closed(self):
        with self.assertRaises(RuntimeError) as ctx:
            enforce_require_hashes([_dep("a", None)], enabled=True)
        self.assertIn("require_hashes", str(ctx.exception))

    def test_enabled_with_empty_hash_fails_closed(self):
        with self.assertRaises(RuntimeError):
            enforce_require_hashes([_dep("a", "")], enabled=True)

    def test_enabled_ignores_local_only(self):
        enforce_require_hashes([_dep("a", None, source="local")], enabled=True)

    def test_enabled_redacts_credentials_in_message(self):
        # A repo_url may carry inline ``user:token@host`` credentials. The
        # fail-closed error names offenders, so it MUST redact the secret
        # before formatting -- otherwise the token leaks into terminal/CI logs.
        dep = LockedDependency(
            repo_url="https://alice:s3cr3t-token@git.example.com/x.git",
            resolved_commit="a" * 40,
            content_hash=None,
            source=None,
        )
        with self.assertRaises(RuntimeError) as ctx:
            enforce_require_hashes([dep], enabled=True)
        msg = str(ctx.exception)
        urls = [tok.strip("(),.;'\"") for tok in msg.split() if "://" in tok]
        self.assertEqual(len(urls), 1)
        parsed = urlparse(urls[0])
        self.assertEqual(parsed.hostname, "git.example.com")
        self.assertNotEqual(parsed.password, "s3cr3t-token")
        self.assertNotIn("s3cr3t-token", msg)


def _gate_ctx(apm_dir: Path, require_hashes: bool):
    """Minimal install-context stub for ``_enforce_require_hashes``."""
    integrity = SimpleNamespace(require_hashes=require_hashes)
    security = SimpleNamespace(integrity=integrity)
    policy = SimpleNamespace(security=security)
    policy_fetch = SimpleNamespace(policy=policy)
    return SimpleNamespace(
        no_policy=False,
        policy_fetch=policy_fetch,
        apm_dir=apm_dir,
        project_root=apm_dir,
    )


class TestPipelineEnforceRequireHashes(unittest.TestCase):
    """The pipeline gate must fail closed on an unreadable lockfile."""

    def test_unreadable_lockfile_fails_closed_when_enabled(self):
        from apm_cli.install.phases.policy_gate import PolicyViolationError
        from apm_cli.install.pipeline import _enforce_require_hashes

        with tempfile.TemporaryDirectory() as d:
            ctx = _gate_ctx(Path(d), require_hashes=True)
            with patch("apm_cli.deps.lockfile.LockFile.read", return_value=None):
                with self.assertRaises(PolicyViolationError) as exc:
                    _enforce_require_hashes(ctx)
        self.assertIn("require_hashes", str(exc.exception))

    def test_unreadable_lockfile_noop_when_disabled(self):
        from apm_cli.install.pipeline import _enforce_require_hashes

        with tempfile.TemporaryDirectory() as d:
            ctx = _gate_ctx(Path(d), require_hashes=False)
            with patch("apm_cli.deps.lockfile.LockFile.read", return_value=None):
                # Default-off must not raise even if the lockfile is unreadable.
                _enforce_require_hashes(ctx)


if __name__ == "__main__":
    unittest.main()
