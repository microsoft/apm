"""Test module for console commands."""

import os  # noqa: F401
import shutil
import tempfile
from unittest import TestCase  # noqa: F401

import pytest  # noqa: F401
from click.testing import CliRunner

from apm_cli.cli import cli as app

runner = CliRunner()


def test_read_url():
    """Test the read-url command with proper temp directory cleanup."""
    url = "https://www.example.com"
    temp_dir = tempfile.mkdtemp()
    try:
        result = runner.invoke(app, ["read-url", url, "--output-dir", temp_dir])  # noqa: F841
        # Add appropriate assertions based on expected behavior
        # assert result.exit_code == 0
    finally:
        # Always clean up the temporary directory
        shutil.rmtree(temp_dir, ignore_errors=True)
