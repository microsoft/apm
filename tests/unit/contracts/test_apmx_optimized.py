"""Optimized direct-script launch ABI used by the frozen companion executable."""

import subprocess
import sys

import pytest

import apm_cli.apmx as apmx_module

pytestmark = pytest.mark.e2e


def test_optimized_direct_launcher_retains_command_help() -> None:
    """Cross the executable boundary with the frozen build's optimization level."""
    result = subprocess.run(
        [sys.executable, "-OO", apmx_module.__file__, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Run one explicit CONTRACT file" in result.stdout
    assert "package-relative .contract.md paths" in result.stdout
