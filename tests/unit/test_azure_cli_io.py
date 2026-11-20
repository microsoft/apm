"""Unit tests for AzureCliBearerProvider and AzureCliBearerError (filesystem, process and CLI boundary cases)."""

import subprocess
import threading  # noqa: F401
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.core.azure_cli import (
    AzureCliBearerError,
    AzureCliBearerProvider,
)

pytestmark = pytest.mark.component

# A plausible JWT-shaped string (starts with eyJ, length > 100).


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Windows az.cmd resolution (regression for microsoft/apm#1430)
# ---------------------------------------------------------------------------


@pytest.mark.windows_compat
class TestWindowsAzCmdResolution:
    """Regression: subprocess.run must receive the shutil.which-resolved
    path, not the bare "az" token. On Windows the resolver returns
    "C:\\\\...\\\\az.cmd" and CreateProcessW cannot find "az" alone."""

    WINDOWS_AZ_CMD = r"C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"

    def test_init_absolute_path_skips_resolution(self, tmp_path: Path) -> None:
        """Trust a native absolute path, not a POSIX-only rooted spelling.

        On Python 3.13+ Windows, /opt/custom/az is rooted but not absolute:
        it has no drive. Use a real platform-native absolute path here.
        """
        az_command = str(tmp_path / "custom" / "az")
        with patch("shutil.which") as mock_which:
            provider = AzureCliBearerProvider(az_command=az_command)
            mock_which.assert_not_called()
            assert provider._az_command == az_command

    def test_get_current_tenant_id_returns_none_without_subprocess_when_az_missing(self):
        """Explicit early-return guard: when az was not resolved at __init__,
        get_current_tenant_id() must short-circuit to None rather than
        passing None as argv[0] (which would TypeError and only be caught
        by the broad except). Mirrors get_bearer_token's is_available()
        pre-check."""
        with (
            patch("shutil.which", return_value=None),
            patch("subprocess.run") as mock_run,
        ):
            provider = AzureCliBearerProvider()
            assert provider.get_current_tenant_id() is None
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# get_bearer_token
# ---------------------------------------------------------------------------


class TestGetBearerToken:
    def test_get_bearer_subprocess_timeout(self):
        with (
            patch("shutil.which", return_value="/usr/bin/az"),
            patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="az", timeout=30),
            ),
        ):
            provider = AzureCliBearerProvider()
            with pytest.raises(AzureCliBearerError) as exc_info:
                provider.get_bearer_token()
            assert exc_info.value.kind == "subprocess_error"


# ---------------------------------------------------------------------------
# get_current_tenant_id
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# clear_cache
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------
