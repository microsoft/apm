"""Regression trap: the CLI lock command must not eagerly load the SPDX table.

The bundled SPDX identifier table (``apm_cli.export.spdx_data``) is large and is
only needed when an SBOM is actually exported. ``apm_cli.commands.lock`` is
imported on every ``apm`` invocation, so importing it must NOT transitively pull
in the SPDX table -- the import is deferred into ``lock export`` itself.
"""

from __future__ import annotations

import subprocess
import sys


def _import_probe(module: str) -> str:
    code = (
        "import importlib, sys; "
        f"importlib.import_module({module!r}); "
        "print('apm_cli.export.spdx_data' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_importing_lock_command_does_not_load_spdx_data():
    assert _import_probe("apm_cli.commands.lock") == "False"


def test_importing_sbom_does_load_spdx_data():
    # Sanity: the serializer DOES depend on the table, so the probe can tell the
    # difference -- guards against a vacuously-passing first assertion.
    assert _import_probe("apm_cli.export.sbom") == "True"
