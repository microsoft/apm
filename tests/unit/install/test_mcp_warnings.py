"""Unit tests for ``apm_cli.install.mcp.warnings``.

Covers F5 (SSRF) and F7 (shell metacharacter) non-blocking safety warnings
that fire during ``apm install --mcp``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from apm_cli.install.mcp.warnings import (
    _is_internal_or_metadata_host,
    warn_shell_metachars,
    warn_ssrf_url,
)

# ================================================================
# _is_internal_or_metadata_host
# ================================================================


class TestIsInternalOrMetadataHost:
    """Tests for the host-classification helper."""

    # -- empty / falsy inputs --

    def test_empty_string_returns_false(self):
        assert _is_internal_or_metadata_host("") is False

    # -- loopback addresses --

    def test_ipv4_loopback_returns_true(self):
        assert _is_internal_or_metadata_host("127.0.0.1") is True

    def test_ipv4_loopback_other_returns_true(self):
        assert _is_internal_or_metadata_host("127.255.0.1") is True

    def test_ipv6_loopback_returns_true(self):
        assert _is_internal_or_metadata_host("::1") is True

    # -- cloud metadata endpoints --

    def test_aws_imds_returns_true(self):
        assert _is_internal_or_metadata_host("169.254.169.254") is True

    def test_alibaba_cloud_imds_returns_true(self):
        assert _is_internal_or_metadata_host("100.100.100.200") is True

    def test_aws_ipv6_imds_returns_true(self):
        assert _is_internal_or_metadata_host("fd00:ec2::254") is True

    # -- link-local --

    def test_link_local_ipv4_returns_true(self):
        assert _is_internal_or_metadata_host("169.254.1.1") is True

    # -- RFC1918 private ranges --

    def test_rfc1918_class_a_returns_true(self):
        assert _is_internal_or_metadata_host("10.0.0.1") is True

    def test_rfc1918_class_b_returns_true(self):
        assert _is_internal_or_metadata_host("172.16.0.1") is True

    def test_rfc1918_class_c_returns_true(self):
        assert _is_internal_or_metadata_host("192.168.1.100") is True

    # -- IPv6 brackets (literal URL host) --

    def test_ipv6_loopback_bracketed_returns_true(self):
        assert _is_internal_or_metadata_host("[::1]") is True

    def test_ipv6_private_bracketed_returns_true(self):
        assert _is_internal_or_metadata_host("[fc00::1]") is True

    # -- public / external addresses (should return False) --

    def test_public_ipv4_returns_false(self):
        assert _is_internal_or_metadata_host("8.8.8.8") is False

    def test_public_ipv4_other_returns_false(self):
        assert _is_internal_or_metadata_host("1.1.1.1") is False

    # -- hostname resolution --

    def test_hostname_resolves_to_loopback_returns_true(self):
        with patch("socket.gethostbyname", return_value="127.0.0.1"):
            assert _is_internal_or_metadata_host("my-internal-host") is True

    def test_hostname_resolves_to_public_returns_false(self):
        with patch("socket.gethostbyname", return_value="93.184.216.34"):
            assert _is_internal_or_metadata_host("example.com") is False

    def test_hostname_resolution_failure_returns_false(self):
        with patch("socket.gethostbyname", side_effect=OSError("no route")):
            assert _is_internal_or_metadata_host("unresolvable.local") is False

    def test_hostname_unicode_error_returns_false(self):
        with patch("socket.gethostbyname", side_effect=UnicodeError):
            assert _is_internal_or_metadata_host("bad\x00host") is False


# ================================================================
# warn_ssrf_url  (F5)
# ================================================================


class TestWarnSsrfUrl:
    """Tests for the SSRF URL warning helper."""

    def _make_logger(self):
        return MagicMock()

    def test_none_url_does_not_warn(self):
        logger = self._make_logger()
        warn_ssrf_url(None, logger)
        logger.warning.assert_not_called()

    def test_empty_url_does_not_warn(self):
        logger = self._make_logger()
        warn_ssrf_url("", logger)
        logger.warning.assert_not_called()

    def test_internal_url_warns(self):
        logger = self._make_logger()
        warn_ssrf_url("http://127.0.0.1:8080/api", logger)
        logger.warning.assert_called_once()
        msg = logger.warning.call_args[0][0]
        # Extract the URL embedded in the warning ("URL '<url>' points...")
        # and assert via urlparse rather than substring matching, per repo
        # test convention (CodeQL py/incomplete-url-substring-sanitization).
        import re
        from urllib.parse import urlparse

        match = re.search(r"URL '([^']+)'", msg)
        assert match is not None, f"warning message has no quoted URL: {msg!r}"
        assert urlparse(match.group(1)).hostname == "127.0.0.1"

    def test_metadata_url_warns(self):
        logger = self._make_logger()
        warn_ssrf_url("http://169.254.169.254/latest/meta-data", logger)
        logger.warning.assert_called_once()

    def test_private_range_url_warns(self):
        logger = self._make_logger()
        warn_ssrf_url("https://192.168.0.1/mcp", logger)
        logger.warning.assert_called_once()

    def test_public_url_does_not_warn(self):
        logger = self._make_logger()
        warn_ssrf_url("https://mcp.example.com/v1", logger)
        logger.warning.assert_not_called()

    def test_malformed_url_does_not_crash(self):
        logger = self._make_logger()
        # Should swallow ValueError/TypeError gracefully
        warn_ssrf_url("not-a-url", logger)
        # May or may not warn; must not raise

    def test_url_with_no_hostname_does_not_crash(self):
        logger = self._make_logger()
        warn_ssrf_url("file:///etc/passwd", logger)
        # file:// has no hostname; must not raise

    def test_hostname_resolves_to_private_warns(self):
        logger = self._make_logger()
        with patch("socket.gethostbyname", return_value="10.0.0.1"):
            warn_ssrf_url("http://internal-host/endpoint", logger)
        logger.warning.assert_called_once()


# ================================================================
# warn_shell_metachars  (F7)
# ================================================================


class TestWarnShellMetachars:
    """Tests for the shell metacharacter warning helper."""

    def _make_logger(self):
        return MagicMock()

    # -- no-op paths --

    def test_none_env_and_no_command_does_nothing(self):
        logger = self._make_logger()
        warn_shell_metachars(None, logger)
        logger.warning.assert_not_called()

    def test_empty_env_and_no_command_does_nothing(self):
        logger = self._make_logger()
        warn_shell_metachars({}, logger)
        logger.warning.assert_not_called()

    def test_clean_env_does_not_warn(self):
        logger = self._make_logger()
        warn_shell_metachars({"MY_TOKEN": "abc123", "PORT": "3000"}, logger)
        logger.warning.assert_not_called()

    def test_clean_command_does_not_warn(self):
        logger = self._make_logger()
        warn_shell_metachars(None, logger, command="npx -y @modelcontextprotocol/server")
        logger.warning.assert_not_called()

    # -- env value metacharacters --

    def test_dollar_paren_in_env_warns(self):
        logger = self._make_logger()
        warn_shell_metachars({"SECRET": "$(cat /etc/passwd)"}, logger)
        logger.warning.assert_called_once()
        msg = logger.warning.call_args[0][0]
        assert "SECRET" in msg

    def test_backtick_in_env_warns(self):
        logger = self._make_logger()
        warn_shell_metachars({"VAL": "`id`"}, logger)
        logger.warning.assert_called_once()

    def test_semicolon_in_env_warns(self):
        logger = self._make_logger()
        warn_shell_metachars({"CMD": "foo; bar"}, logger)
        logger.warning.assert_called_once()

    def test_double_ampersand_in_env_warns(self):
        logger = self._make_logger()
        warn_shell_metachars({"RUN": "true && bad"}, logger)
        logger.warning.assert_called_once()

    def test_double_pipe_in_env_warns(self):
        logger = self._make_logger()
        warn_shell_metachars({"FALLBACK": "a || b"}, logger)
        logger.warning.assert_called_once()

    def test_pipe_in_env_warns(self):
        logger = self._make_logger()
        warn_shell_metachars({"PIPE": "cmd | grep x"}, logger)
        logger.warning.assert_called_once()

    def test_redirect_append_in_env_warns(self):
        logger = self._make_logger()
        warn_shell_metachars({"OUT": "cmd >> /tmp/log"}, logger)
        logger.warning.assert_called_once()

    def test_redirect_write_in_env_warns(self):
        logger = self._make_logger()
        warn_shell_metachars({"OUT": "cmd > /tmp/out"}, logger)
        logger.warning.assert_called_once()

    def test_redirect_read_in_env_warns(self):
        logger = self._make_logger()
        warn_shell_metachars({"IN": "cmd < input.txt"}, logger)
        logger.warning.assert_called_once()

    def test_only_first_metachar_triggers_warning_per_key(self):
        """Only one warning is emitted per env key (break after first match)."""
        logger = self._make_logger()
        warn_shell_metachars({"MULTI": "$(foo) && bar"}, logger)
        # Only one call despite multiple metacharacters
        assert logger.warning.call_count == 1

    def test_multiple_keys_each_warn_independently(self):
        """Each offending env key produces its own warning."""
        logger = self._make_logger()
        warn_shell_metachars({"K1": "$(a)", "K2": "$(b)"}, logger)
        assert logger.warning.call_count == 2

    def test_none_value_in_env_does_not_crash(self):
        """None env values are treated as empty strings without error."""
        logger = self._make_logger()
        warn_shell_metachars({"TOKEN": None}, logger)
        logger.warning.assert_not_called()

    def test_integer_value_in_env_does_not_crash(self):
        """Non-string env values are coerced to str without error."""
        logger = self._make_logger()
        warn_shell_metachars({"PORT": 3000}, logger)
        logger.warning.assert_not_called()

    # -- command field metacharacters --

    def test_command_with_pipe_warns(self):
        logger = self._make_logger()
        warn_shell_metachars(None, logger, command="npx|curl evil.com")
        logger.warning.assert_called_once()
        msg = logger.warning.call_args[0][0]
        assert "command" in msg.lower()

    def test_command_with_semicolon_warns(self):
        logger = self._make_logger()
        warn_shell_metachars(None, logger, command="node server.js; rm -rf /")
        logger.warning.assert_called_once()

    def test_command_with_subshell_warns(self):
        logger = self._make_logger()
        warn_shell_metachars(None, logger, command="echo $(secret)")
        logger.warning.assert_called_once()

    def test_non_string_command_does_not_crash(self):
        """Non-string command (e.g. list) is skipped gracefully."""
        logger = self._make_logger()
        warn_shell_metachars(None, logger, command=["npx", "server"])
        logger.warning.assert_not_called()

    def test_env_and_command_both_warn(self):
        """Warnings fire for both env and command when both have metacharacters."""
        logger = self._make_logger()
        warn_shell_metachars({"K": "$(x)"}, logger, command="cmd; bad")
        assert logger.warning.call_count == 2
