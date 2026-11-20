"""Phase-3 unit tests for ``apm_cli.install.validation``.

Covers branches not hit by existing validation tests:

* ``_is_tls_failure`` -- SSLError, TLS prefix, CERTIFICATE_VERIFY_FAILED,
  chained causes, max-depth guard
* ``_log_tls_failure`` -- non-verbose and verbose branches
* ``_local_path_failure_reason`` -- all branches (non-local, not-exists,
  not-dir, no-markers)
* ``_local_path_no_markers_hint`` -- no found, with logger, without logger,
  > 5 items
* ``_validate_package_exists`` -- local exists with apm.yml, with SKILL.md,
  not dir, virtual + enforce_only, virtual subdir non-github,
  ADO enforce_only, github fallback enforce_only, fallback invalid slug,
  exception parse fallback
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# _is_tls_failure
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _log_tls_failure
# ---------------------------------------------------------------------------


class TestLogTlsFailure:
    @pytest.fixture(autouse=True)
    def _default_trust_selection(self, monkeypatch):
        """Pin the unrecorded trust selection whose guidance these tests assert."""
        import apm_cli.core.tls_trust as tls

        monkeypatch.setattr(tls, "_LAST_TLS_SELECTION", None)

    def test_non_verbose_emits_single_warning(self) -> None:
        from apm_cli.install.validation import _log_tls_failure

        logger = MagicMock()
        exc = RuntimeError("TLS verification failed")
        _log_tls_failure("example.com", exc, verbose_log=None, logger=logger)
        logger.warning.assert_called_once()
        message = logger.warning.call_args[0][0]
        assert "system trust store" in message
        assert "REQUESTS_CA_BUNDLE" in message


# ---------------------------------------------------------------------------
# _local_path_failure_reason
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _local_path_no_markers_hint
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _validate_package_exists: local paths
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _validate_package_exists: virtual + enforce_only
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _validate_package_exists: ADO enforce_only
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _validate_package_exists: exception parse fallback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _validate_package_exists: TLS failure path
# ---------------------------------------------------------------------------
