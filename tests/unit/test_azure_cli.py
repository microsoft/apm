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

    def test_is_available_stable_after_construction(self):
        """is_available reflects construction-time resolution only.

        Once the provider is built, mid-process PATH changes (or shutil.which
        side effects) must NOT flip the answer. This is the contract that
        keeps is_available() consistent with get_bearer_token()'s pre-check
        and avoids a "True now / az_not_found a moment later" race.
        """
        with patch("apm_cli.core.azure_cli.shutil.which", return_value="/usr/bin/az"):
            provider = AzureCliBearerProvider()
        # Re-enter a context where which() would now say "missing": the
        # already-constructed provider must still report True.
        with patch("apm_cli.core.azure_cli.shutil.which", return_value=None):
            assert provider.is_available() is True


# ---------------------------------------------------------------------------
# Windows az.cmd resolution (regression for microsoft/apm#1430)
# ---------------------------------------------------------------------------


class TestWindowsAzCmdResolution:
    """Regression: subprocess.run must receive the shutil.which-resolved
    path, not the bare "az" token. On Windows the resolver returns
    "C:\\\\...\\\\az.cmd" and CreateProcessW cannot find "az" alone."""

    WINDOWS_AZ_CMD = r"C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"

    def test_init_resolves_via_shutil_which(self):
        with patch(
            "apm_cli.core.azure_cli.shutil.which", return_value=self.WINDOWS_AZ_CMD
        ) as mock_which:
            provider = AzureCliBearerProvider()
            mock_which.assert_called_once_with("az")
            assert provider._az_command == self.WINDOWS_AZ_CMD

    def test_init_stores_none_when_not_on_path(self):
        with patch("apm_cli.core.azure_cli.shutil.which", return_value=None):
            provider = AzureCliBearerProvider()
            assert provider._az_command is None

    def test_init_absolute_path_skips_resolution(self):
        """Caller passed an explicit absolute path -- trust verbatim."""
        with patch("apm_cli.core.azure_cli.shutil.which") as mock_which:
            provider = AzureCliBearerProvider(az_command="/opt/custom/az")
            mock_which.assert_not_called()
            assert provider._az_command == "/opt/custom/az"

    def test_init_relative_with_separator_still_resolves(self):
        """A relative-with-separator token like 'subdir/az' must NOT bypass
        shutil.which; otherwise a caller could hand subprocess.run a
        CWD-relative path that the OS resolves against the wrong
        directory. Only absolute paths are trusted verbatim."""
        with patch("apm_cli.core.azure_cli.shutil.which", return_value="/usr/bin/az") as mock_which:
            provider = AzureCliBearerProvider(az_command="subdir/az")
            mock_which.assert_called_once_with("subdir/az")
            assert provider._az_command == "/usr/bin/az"

    def test_get_bearer_token_invokes_resolved_az_cmd_path(self):
        """The exact Windows shape: shutil.which returns az.cmd; verify it
        flows into subprocess.run as the argv[0]. Without the fix, argv[0]
        would be the bare "az" token and CreateProcessW would fail."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = FAKE_JWT + "\n"
        mock_result.stderr = ""

        with (
            patch("apm_cli.core.azure_cli.shutil.which", return_value=self.WINDOWS_AZ_CMD),
            patch("apm_cli.core.azure_cli.subprocess.run", return_value=mock_result) as mock_run,
        ):
            provider = AzureCliBearerProvider()
            provider.get_bearer_token()
            assert mock_run.call_count == 1
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == self.WINDOWS_AZ_CMD, (
                f"subprocess.run must receive the resolved az.cmd path, got: {cmd[0]!r}"
            )

    def test_bare_az_would_raise_filenotfound_but_resolved_path_succeeds(self):
        """Explicit regression trap for the #1430 cascade.

        Pre-fix: subprocess.run(["az", ...]) raised FileNotFoundError on
        Windows (CreateProcessW does not honor PATHEXT for az.cmd), which
        _run_get_access_token caught as AzureCliBearerError(kind=
        "subprocess_error"). The error propagated and was rendered as the
        misleading "az present but not logged in" Case 3 diagnostic.

        Post-fix: the constructor resolves to the .cmd absolute path, so
        subprocess.run receives a path CreateProcessW CAN find and the
        bearer succeeds. This test pins both halves of the contract:
        bare 'az' -> FileNotFoundError; resolved path -> success.
        """
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = FAKE_JWT + "\n"
        mock_result.stderr = ""

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "az":
                # Simulate the Windows CreateProcessW behavior pre-fix.
                raise FileNotFoundError(2, "No such file or directory", "az")
            return mock_result

        with (
            patch("apm_cli.core.azure_cli.shutil.which", return_value=self.WINDOWS_AZ_CMD),
            patch("apm_cli.core.azure_cli.subprocess.run", side_effect=fake_run),
        ):
            provider = AzureCliBearerProvider()
            # Would raise AzureCliBearerError(kind='subprocess_error') pre-fix.
            token = provider.get_bearer_token()
            assert token == FAKE_JWT

    def test_get_current_tenant_id_invokes_resolved_az_cmd_path(self):
        """Same regression for the get_current_tenant_id() probe -- this
        was the second swallowed-failure that drove the misleading Case 3
        diagnostic on Windows."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "72f988bf-86f1-41af-91ab-2d7cd011db47\n"
        mock_result.stderr = ""

        with (
            patch("apm_cli.core.azure_cli.shutil.which", return_value=self.WINDOWS_AZ_CMD),
            patch("apm_cli.core.azure_cli.subprocess.run", return_value=mock_result) as mock_run,
        ):
            provider = AzureCliBearerProvider()
            provider.get_current_tenant_id()
            assert mock_run.call_count == 1
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == self.WINDOWS_AZ_CMD

    def test_get_current_tenant_id_returns_none_without_subprocess_when_az_missing(self):
        """Explicit early-return guard: when az was not resolved at __init__,
        get_current_tenant_id() must short-circuit to None rather than
        passing None as argv[0] (which would TypeError and only be caught
        by the broad except). Mirrors get_bearer_token's is_available()
        pre-check."""
        with (
            patch("apm_cli.core.azure_cli.shutil.which", return_value=None),
            patch("apm_cli.core.azure_cli.subprocess.run") as mock_run,
        ):
            provider = AzureCliBearerProvider()
            assert provider.get_current_tenant_id() is None
            mock_run.assert_not_called()


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
