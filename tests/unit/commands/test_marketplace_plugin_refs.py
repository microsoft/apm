"""Unit tests for ref, source and tag helpers behind ``apm marketplace package``."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.commands.marketplace.plugin import _resolve_ref
from apm_cli.core.command_logger import CommandLogger
from apm_cli.marketplace.ref_resolver import RemoteRef

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-marketplace-entry-ref-verification"),
]

_FAKE_SHA = "a" * 40
_FAKE_SHA_B = "b" * 40


class TestResolveRef:
    """Unit tests for the ``_resolve_ref()`` helper."""

    def _make_logger(self) -> CommandLogger:
        return CommandLogger("test", verbose=False)

    def test_version_set_returns_none(self):
        """When version is provided, ref resolution is skipped."""
        result = _resolve_ref(
            self._make_logger(),
            "acme/tools",
            ref=None,
            version=">=1.0.0",
            no_verify=False,
        )
        assert result is None

    def test_no_ref_no_verify_exits(self):
        """No --ref, --no-verify → error (cannot resolve without network)."""
        with pytest.raises(SystemExit) as exc_info:
            _resolve_ref(
                self._make_logger(),
                "acme/tools",
                ref=None,
                version=None,
                no_verify=True,
            )
        assert exc_info.value.code == 2

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.resolve_ref_sha",
        return_value=_FAKE_SHA,
    )
    def test_no_ref_resolves_head(self, mock_resolve):
        """No --ref, no --version → auto-resolve HEAD."""
        result = _resolve_ref(
            self._make_logger(),
            "acme/tools",
            ref=None,
            version=None,
            no_verify=False,
        )
        assert result == _FAKE_SHA
        mock_resolve.assert_called_once_with("acme/tools", "HEAD")

    def test_explicit_head_no_verify_exits(self):
        """--ref HEAD + --no-verify → error."""
        with pytest.raises(SystemExit) as exc_info:
            _resolve_ref(
                self._make_logger(),
                "acme/tools",
                ref="HEAD",
                version=None,
                no_verify=True,
            )
        assert exc_info.value.code == 2

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.resolve_ref_sha",
        return_value=_FAKE_SHA,
    )
    def test_explicit_head_resolves(self, mock_resolve):
        """--ref HEAD → warn + resolve."""
        result = _resolve_ref(
            self._make_logger(),
            "acme/tools",
            ref="HEAD",
            version=None,
            no_verify=False,
        )
        assert result == _FAKE_SHA

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.resolve_ref_sha",
        return_value=_FAKE_SHA,
    )
    def test_explicit_head_case_insensitive(self, mock_resolve):
        """--ref head (lowercase) → treated as HEAD."""
        result = _resolve_ref(
            self._make_logger(),
            "acme/tools",
            ref="head",
            version=None,
            no_verify=False,
        )
        assert result == _FAKE_SHA

    def test_sha_returns_as_is(self):
        """A 40-char hex SHA is returned unchanged."""
        result = _resolve_ref(
            self._make_logger(),
            "acme/tools",
            ref=_FAKE_SHA,
            version=None,
            no_verify=False,
        )
        assert result == _FAKE_SHA

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
        return_value=[
            RemoteRef(name="refs/heads/main", sha=_FAKE_SHA_B),
            RemoteRef(name="refs/tags/v1.0.0", sha=_FAKE_SHA),
        ],
    )
    def test_branch_name_resolves_to_sha(self, mock_list):
        """--ref main (matching refs/heads/main) → warn + resolve."""
        result = _resolve_ref(
            self._make_logger(),
            "acme/tools",
            ref="main",
            version=None,
            no_verify=False,
        )
        assert result == _FAKE_SHA_B

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
        return_value=[
            RemoteRef(name="refs/heads/main", sha=_FAKE_SHA),
            RemoteRef(name="refs/tags/v1.0.0", sha=_FAKE_SHA_B),
        ],
    )
    def test_tag_name_returns_as_is(self, mock_list):
        """--ref v1.0.0 (not a branch) → returned as-is."""
        result = _resolve_ref(
            self._make_logger(),
            "acme/tools",
            ref="v1.0.0",
            version=None,
            no_verify=False,
        )
        assert result == "v1.0.0"

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
        return_value=[RemoteRef(name="refs/tags/v1.0.0", sha=_FAKE_SHA)],
    )
    def test_missing_tag_exits_like_check(self, mock_list):
        """--ref naming no tag or branch on a reachable remote is refused."""
        with pytest.raises(SystemExit) as exc_info:
            _resolve_ref(
                self._make_logger(),
                "acme/tools",
                ref="v9.9.9",
                version=None,
                no_verify=False,
            )
        assert exc_info.value.code == 2

    @patch(
        "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
        return_value=[RemoteRef(name="refs/tags/v1.0.0", sha=_FAKE_SHA)],
    )
    def test_missing_tag_with_no_verify_is_stored(self, mock_list):
        """--no-verify keeps the explicit opt-out for unpublished tags."""
        result = _resolve_ref(
            self._make_logger(),
            "acme/tools",
            ref="v9.9.9",
            version=None,
            no_verify=True,
        )
        assert result == "v9.9.9"

    def test_ref_lookup_uses_the_source_host(self):
        """A host-qualified source is listed on its own host, not the default."""
        with patch("apm_cli.marketplace.ref_resolver.RefResolver") as resolver_cls:
            resolver_cls.return_value.list_remote_refs.return_value = [
                RemoteRef(name="refs/tags/v1.0.0", sha=_FAKE_SHA)
            ]
            result = _resolve_ref(
                self._make_logger(),
                "acme/tools",
                ref="v1.0.0",
                version=None,
                no_verify=False,
                host="git.example.invalid",
            )
        assert result == "v1.0.0"
        resolver_cls.assert_called_once_with(host="git.example.invalid")

    def test_git_ls_remote_error_on_head_exits(self) -> None:
        """Lines 139-144: GitLsRemoteError on resolve_ref_sha → exit 2."""
        from apm_cli.commands.marketplace.plugin import _resolve_ref
        from apm_cli.marketplace.errors import GitLsRemoteError

        logger = MagicMock()
        with patch(
            "apm_cli.marketplace.ref_resolver.RefResolver.resolve_ref_sha",
            side_effect=GitLsRemoteError("pkg", "network error", "check network"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _resolve_ref(logger, "https://example.com/repo.git", None, None, False)

        assert exc_info.value.code == 2

    def test_list_remote_refs_error_exits_without_no_verify(self) -> None:
        """An unreachable remote fails a verified --ref instead of storing it."""
        from apm_cli.commands.marketplace.plugin import _resolve_ref
        from apm_cli.marketplace.errors import GitLsRemoteError

        logger = MagicMock()
        with patch(
            "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
            side_effect=GitLsRemoteError("pkg", "offline", "try later"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _resolve_ref(
                    logger, "https://example.com/repo.git", "my-feature-branch", None, False
                )

        assert exc_info.value.code == 2
        logger.error.assert_called()

    def test_list_remote_refs_error_with_no_verify_returns_unresolved(self) -> None:
        """--no-verify keeps the opt-out: warn and store the ref unresolved."""
        from apm_cli.commands.marketplace.plugin import _resolve_ref
        from apm_cli.marketplace.errors import GitLsRemoteError

        logger = MagicMock()
        with patch(
            "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
            side_effect=GitLsRemoteError("pkg", "offline", "try later"),
        ):
            result = _resolve_ref(
                logger, "https://example.com/repo.git", "my-feature-branch", None, True
            )

        assert result == "my-feature-branch"
        logger.warning.assert_called()

    def test_no_verify_on_branch_exits(self) -> None:
        """Lines 167-172: no_verify=True and ref is a branch → error + exit 2."""
        from apm_cli.commands.marketplace.plugin import _resolve_ref

        logger = MagicMock()
        # list_remote_refs returns a ref matching refs/heads/my-branch
        mock_ref = SimpleNamespace(name="refs/heads/my-branch")
        with patch(
            "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
            return_value=[mock_ref],
        ):
            with pytest.raises(SystemExit) as exc_info:
                _resolve_ref(logger, "https://example.com/repo.git", "my-branch", None, True)

        assert exc_info.value.code == 2


class TestParseTags:
    def test_all_whitespace_returns_none(self) -> None:
        """Lines 76-77: all-whitespace parts → empty list → returns None."""
        from apm_cli.commands.marketplace.plugin import _parse_tags

        result = _parse_tags("  ,  ,  ")
        assert result is None

    def test_normal_tags_returned(self) -> None:
        """Normal CSV string → list of tags."""
        from apm_cli.commands.marketplace.plugin import _parse_tags

        result = _parse_tags("tag1, tag2, tag3")
        assert result == ["tag1", "tag2", "tag3"]

    def test_none_returns_none(self) -> None:
        """None input → None."""
        from apm_cli.commands.marketplace.plugin import _parse_tags

        assert _parse_tags(None) is None


class TestVerifySource:
    def test_git_ls_remote_error_exits(self) -> None:
        """Lines 87-92: GitLsRemoteError → error + exit 2."""
        from apm_cli.commands.marketplace.plugin import _verify_source
        from apm_cli.marketplace.errors import GitLsRemoteError

        logger = MagicMock()
        with patch(
            "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
            side_effect=GitLsRemoteError("pkg", "not found", "check url"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _verify_source(logger, "https://example.com/repo.git")

        assert exc_info.value.code == 2

    def test_offline_miss_error_warns(self) -> None:
        """Lines 93-97: OfflineMissError → warning, no exit."""
        from apm_cli.commands.marketplace.plugin import _verify_source
        from apm_cli.marketplace.errors import OfflineMissError

        logger = MagicMock()
        with patch(
            "apm_cli.marketplace.ref_resolver.RefResolver.list_remote_refs",
            side_effect=OfflineMissError("pkg", "https://example.com/repo.git"),
        ):
            _verify_source(logger, "https://example.com/repo.git")

        logger.warning.assert_called()
