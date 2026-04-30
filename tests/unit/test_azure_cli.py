"""Unit tests for AzureCliBearerProvider and AzureCliBearerError."""

import subprocess
import threading  # noqa: F401
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.azure_cli import (
    AzureCliBearerError,
    AzureCliBearerProvider,
)

# A plausible JWT-shaped string (starts with eyJ, length > 100).
FAKE_JWT = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9." + "a" * 200


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_is_available_when_az_on_path(self):
        with patch("apm_cli.core.azure_cli.shutil.which", return_value="/usr/bin/az"):
            provider = AzureCliBearerProvider()
            assert provider.is_available() is True

    def test_is_available_when_az_missing(self):
        with patch("apm_cli.core.azure_cli.shutil.which", return_value=None):
            provider = AzureCliBearerProvider()
            assert provider.is_available() is False


# ---------------------------------------------------------------------------
# get_bearer_token
# ---------------------------------------------------------------------------


class TestGetBearerToken:
    def test_get_bearer_raises_when_az_missing(self):
        with patch("apm_cli.core.azure_cli.shutil.which", return_value=None):
            provider = AzureCliBearerProvider()
            with pytest.raises(AzureCliBearerError) as exc_info:
                provider.get_bearer_token()
            assert exc_info.value.kind == "az_not_found"

    def test_get_bearer_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = FAKE_JWT + "\n"
        mock_result.stderr = ""

        with (
            patch("apm_cli.core.azure_cli.shutil.which", return_value="/usr/bin/az"),
            patch("apm_cli.core.azure_cli.subprocess.run", return_value=mock_result),
        ):
            provider = AzureCliBearerProvider()
            token = provider.get_bearer_token()
            assert token == FAKE_JWT
            # Verify cache is populated (tuple of (token, expires_at) since #856 follow-up F4)
            cached_token, cached_expiry = provider._cache[AzureCliBearerProvider.ADO_RESOURCE_ID]
            assert cached_token == FAKE_JWT
            assert cached_expiry is None  # bare-JWT fallback path -- no expiry parsed

    def test_get_bearer_caches_result(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = FAKE_JWT + "\n"
        mock_result.stderr = ""

        with (
            patch("apm_cli.core.azure_cli.shutil.which", return_value="/usr/bin/az"),
            patch(
                "apm_cli.core.azure_cli.subprocess.run",
                return_value=mock_result,
            ) as mock_run,
        ):
            provider = AzureCliBearerProvider()
            token1 = provider.get_bearer_token()
            token2 = provider.get_bearer_token()
            assert token1 == token2 == FAKE_JWT
            # subprocess.run should be called exactly once
            mock_run.assert_called_once()

    def test_get_bearer_not_logged_in(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Please run 'az login' to setup account."

        with (
            patch("apm_cli.core.azure_cli.shutil.which", return_value="/usr/bin/az"),
            patch("apm_cli.core.azure_cli.subprocess.run", return_value=mock_result),
        ):
            provider = AzureCliBearerProvider()
            with pytest.raises(AzureCliBearerError) as exc_info:
                provider.get_bearer_token()
            err = exc_info.value
            assert err.kind == "not_logged_in"
            assert "az login" in (err.stderr or "")

    def test_get_bearer_subprocess_timeout(self):
        with (
            patch("apm_cli.core.azure_cli.shutil.which", return_value="/usr/bin/az"),
            patch(
                "apm_cli.core.azure_cli.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="az", timeout=30),
            ),
        ):
            provider = AzureCliBearerProvider()
            with pytest.raises(AzureCliBearerError) as exc_info:
                provider.get_bearer_token()
            assert exc_info.value.kind == "subprocess_error"

    def test_get_bearer_invalid_token_format(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "garbage-not-a-jwt"
        mock_result.stderr = ""

        with (
            patch("apm_cli.core.azure_cli.shutil.which", return_value="/usr/bin/az"),
            patch("apm_cli.core.azure_cli.subprocess.run", return_value=mock_result),
        ):
            provider = AzureCliBearerProvider()
            with pytest.raises(AzureCliBearerError) as exc_info:
                provider.get_bearer_token()
            assert exc_info.value.kind == "subprocess_error"


# ---------------------------------------------------------------------------
# get_current_tenant_id
# ---------------------------------------------------------------------------


class TestGetCurrentTenantId:
    def test_get_current_tenant_id_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "72f988bf-86f1-41af-91ab-2d7cd011db47\n"
        mock_result.stderr = ""

        with patch("apm_cli.core.azure_cli.subprocess.run", return_value=mock_result):
            provider = AzureCliBearerProvider()
            tenant = provider.get_current_tenant_id()
            assert tenant == "72f988bf-86f1-41af-91ab-2d7cd011db47"

    def test_get_current_tenant_id_returns_none_on_failure(self):
        with patch(
            "apm_cli.core.azure_cli.subprocess.run",
            side_effect=OSError("az not found"),
        ):
            provider = AzureCliBearerProvider()
            assert provider.get_current_tenant_id() is None


# ---------------------------------------------------------------------------
# clear_cache
# ---------------------------------------------------------------------------


class TestClearCache:
    def test_clear_cache_drops_token(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = FAKE_JWT + "\n"
        mock_result.stderr = ""

        with (
            patch("apm_cli.core.azure_cli.shutil.which", return_value="/usr/bin/az"),
            patch(
                "apm_cli.core.azure_cli.subprocess.run",
                return_value=mock_result,
            ) as mock_run,
        ):
            provider = AzureCliBearerProvider()
            provider.get_bearer_token()
            assert mock_run.call_count == 1

            provider.clear_cache()

            provider.get_bearer_token()
            assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_thread_safety_concurrent_calls(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = FAKE_JWT + "\n"
        mock_result.stderr = ""

        with (
            patch("apm_cli.core.azure_cli.shutil.which", return_value="/usr/bin/az"),
            patch(
                "apm_cli.core.azure_cli.subprocess.run",
                return_value=mock_result,
            ) as mock_run,
        ):
            provider = AzureCliBearerProvider()
            num_threads = 20

            with ThreadPoolExecutor(max_workers=num_threads) as pool:
                futures = [pool.submit(provider.get_bearer_token) for _ in range(num_threads)]
                results = [f.result() for f in as_completed(futures)]

            # All threads got the same token
            assert all(r == FAKE_JWT for r in results)
            # Singleflight under the lock guarantees exactly one subprocess call
            # even under heavy thread contention. Tightened in #856 follow-up C7+C8.
            assert mock_run.call_count == 1
