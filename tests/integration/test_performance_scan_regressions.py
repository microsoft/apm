"""Deterministic regressions for the consolidated October performance scans."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.commands.marketplace.plugin import _has_marketplace_block

pytestmark = pytest.mark.component


@pytest.mark.parametrize(
    ("module", "dependency"),
    [
        ("apm_cli.commands.update", "git.exc"),
        ("apm_cli.commands.marketplace.plugin", "yaml"),
    ],
)
def test_command_import_defers_its_error_only_dependency(module: str, dependency: str) -> None:
    """Check the reported import edge, not unrelated transitive imports."""
    code = """
import builtins
import importlib
import json
import sys

module, dependency = sys.argv[1:]
original_import = builtins.__import__
attempts = []
def track_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == dependency and globals and globals.get("__name__") == module:
        attempts.append(name)
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = track_import
importlib.import_module(module)
print(json.dumps(attempts))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, module, dependency],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("marketplace:\n  name: example\n", True),
        ("marketplace: {}\n", True),
        ("marketplace: null\n", False),
        ("name: example\n", False),
        ("", False),
        ("- marketplace\n", False),
        ("marketplace: [\n", False),
    ],
)
def test_marketplace_probe_retains_yaml_behavior(
    tmp_path: Path, content: str, expected: bool
) -> None:
    manifest = tmp_path / "apm.yml"
    manifest.write_text(content, encoding="utf-8")
    assert _has_marketplace_block(manifest) is expected


def test_marketplace_probe_retains_missing_and_unreadable_behavior(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    assert _has_marketplace_block(manifest) is False
    manifest.write_text("marketplace: {}\n", encoding="utf-8")
    with patch("apm_cli.utils.yaml_io.load_yaml", side_effect=OSError("permission denied")):
        assert _has_marketplace_block(manifest) is False
