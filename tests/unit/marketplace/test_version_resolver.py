"""Unit tests for marketplace version constraint resolution."""

import unittest
from unittest.mock import patch

from apm_cli.marketplace.errors import NoMatchingVersionError
from apm_cli.marketplace.ref_resolver import RemoteRef
from apm_cli.marketplace.version_resolver import (
    is_version_constraint,
    resolve_version_constraint,
)


def _make_tag_ref(tag_name: str, sha: str = "a" * 40) -> RemoteRef:
    return RemoteRef(name=f"refs/tags/{tag_name}", sha=sha)


def _make_refs(*tag_versions: str, plugin_name: str = "secrets-vault") -> list[RemoteRef]:
    """Build a list of RemoteRef for the given versions using {name}--v{ver} format."""
    refs = []
    for ver in tag_versions:
        sha = ver.replace(".", "")[:8].ljust(40, "0")
        refs.append(_make_tag_ref(f"{plugin_name}--v{ver}", sha=sha))
    return refs


class TestIsVersionConstraint(unittest.TestCase):
    def test_tilde(self):
        assert is_version_constraint("~2.1.0")

    def test_caret(self):
        assert is_version_constraint("^2.0.0")

    def test_gte(self):
        assert is_version_constraint(">=1.4.0")

    def test_exact_with_equals(self):
        assert is_version_constraint("=2.1.0")

    def test_bare_version(self):
        assert is_version_constraint("2.1.0")

    def test_bare_version_with_prerelease(self):
        assert is_version_constraint("2.1.0-beta.1")

    def test_raw_tag_is_not_constraint(self):
        assert not is_version_constraint("v2.1.0")

    def test_branch_is_not_constraint(self):
        assert not is_version_constraint("main")

    def test_sha_is_not_constraint(self):
        assert not is_version_constraint("abc123def456")


@patch("apm_cli.marketplace.version_resolver.RefResolver")
class TestResolveVersionConstraint(unittest.TestCase):
    def test_tilde_range_picks_highest_patch(self, MockResolver):
        refs = _make_refs("2.1.0", "2.1.1", "2.1.5", "2.2.0")
        MockResolver.return_value.list_remote_refs.return_value = refs

        tag, _sha = resolve_version_constraint("secrets-vault", "acme/plugins", "~2.1.0")
        assert tag == "secrets-vault--v2.1.5"

    def test_caret_range_picks_highest_minor(self, MockResolver):
        refs = _make_refs("2.0.0", "2.1.0", "2.5.3", "3.0.0")
        MockResolver.return_value.list_remote_refs.return_value = refs

        tag, _sha = resolve_version_constraint("secrets-vault", "acme/plugins", "^2.0.0")
        assert tag == "secrets-vault--v2.5.3"

    def test_gte_range(self, MockResolver):
        refs = _make_refs("1.0.0", "2.0.0", "3.0.0")
        MockResolver.return_value.list_remote_refs.return_value = refs

        tag, _sha = resolve_version_constraint("secrets-vault", "acme/plugins", ">=2.0.0")
        assert tag == "secrets-vault--v3.0.0"

    def test_exact_match(self, MockResolver):
        refs = _make_refs("2.0.0", "2.1.0", "2.1.1")
        MockResolver.return_value.list_remote_refs.return_value = refs

        tag, _sha = resolve_version_constraint("secrets-vault", "acme/plugins", "2.1.0")
        assert tag == "secrets-vault--v2.1.0"

    def test_no_matching_tag_raises(self, MockResolver):
        refs = _make_refs("1.0.0", "1.1.0")
        MockResolver.return_value.list_remote_refs.return_value = refs

        with self.assertRaises(NoMatchingVersionError):
            resolve_version_constraint("secrets-vault", "acme/plugins", "~2.0.0")

    def test_prerelease_excluded(self, MockResolver):
        refs = _make_refs("2.1.0", "2.2.0-beta.1")
        MockResolver.return_value.list_remote_refs.return_value = refs

        tag, _sha = resolve_version_constraint("secrets-vault", "acme/plugins", "^2.0.0")
        assert tag == "secrets-vault--v2.1.0"

    def test_ignores_other_plugin_tags(self, MockResolver):
        refs = [
            *_make_refs("2.0.0", "2.1.0", plugin_name="secrets-vault"),
            *_make_refs("2.5.0", plugin_name="other-plugin"),
        ]
        MockResolver.return_value.list_remote_refs.return_value = refs

        tag, _sha = resolve_version_constraint("secrets-vault", "acme/plugins", "^2.0.0")
        assert tag == "secrets-vault--v2.1.0"

    def test_empty_refs_raises(self, MockResolver):
        MockResolver.return_value.list_remote_refs.return_value = []

        with self.assertRaises(NoMatchingVersionError):
            resolve_version_constraint("secrets-vault", "acme/plugins", "~1.0.0")

    def test_passes_host_token_and_auth_scheme(self, MockResolver):
        refs = _make_refs("1.0.0", plugin_name="my-plugin")
        MockResolver.return_value.list_remote_refs.return_value = refs

        resolve_version_constraint(
            "my-plugin",
            "owner/repo",
            "^1.0.0",
            host="dev.azure.com",
            token="dummy-bearer",
            auth_scheme="bearer",
        )
        MockResolver.assert_called_once_with(
            host="dev.azure.com",
            token="dummy-bearer",
            auth_scheme="bearer",
        )

    def test_passes_auth_owner_for_ado_retry(self, MockResolver):
        refs = _make_refs("1.0.0", plugin_name="my-plugin")
        MockResolver.return_value.list_remote_refs.return_value = refs
        auth_resolver = object()

        resolve_version_constraint(
            "my-plugin",
            "owner/repo",
            "^1.0.0",
            host="dev.azure.com",
            token="stale-pat",
            auth_scheme="basic",
            auth_resolver=auth_resolver,
        )

        MockResolver.assert_called_once_with(
            host="dev.azure.com",
            token="stale-pat",
            auth_scheme="basic",
            auth_resolver=auth_resolver,
            auth_target="dev.azure.com",
        )

    def test_resolver_closed_after_use(self, MockResolver):
        refs = _make_refs("1.0.0", plugin_name="my-plugin")
        MockResolver.return_value.list_remote_refs.return_value = refs

        resolve_version_constraint("my-plugin", "owner/repo", "^1.0.0")
        MockResolver.return_value.close.assert_called_once()

    def test_resolver_closed_on_error(self, MockResolver):
        MockResolver.return_value.list_remote_refs.side_effect = RuntimeError("network")

        with self.assertRaises(RuntimeError):
            resolve_version_constraint("my-plugin", "owner/repo", "^1.0.0")
        MockResolver.return_value.close.assert_called_once()

    def test_bare_version_resolves_against_tags(self, MockResolver):
        refs = _make_refs("2.1.0", "2.1.1", "2.2.0")
        MockResolver.return_value.list_remote_refs.return_value = refs

        tag, _sha = resolve_version_constraint("secrets-vault", "acme/plugins", "2.1.0")
        assert tag == "secrets-vault--v2.1.0"
