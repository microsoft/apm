"""Regression trap: root help and light commands must not import heavy verbs.

``apm --help``, ``apm doctor``, and ``apm config get`` must not load
``install``, ``audit``, ``pack``, ``marketplace``, ``uninstall``, or
``update``. Those modules load only when the matching verb is dispatched.
"""

from __future__ import annotations

import subprocess
import sys

_FORBIDDEN_PREFIXES = (
    "apm_cli.commands.install",
    "apm_cli.commands.audit",
    "apm_cli.commands.pack",
    "apm_cli.commands.marketplace",
    "apm_cli.commands.uninstall",
    "apm_cli.commands.update",
)

_PROBE = r"""
import sys
from click.testing import CliRunner
from apm_cli.cli import cli

result = CliRunner().invoke(cli, {argv!r})
loaded = sorted(
    m for m in sys.modules
    if m == {prefixes!r}[0] or any(m == p or m.startswith(p + '.') for p in {prefixes!r})
)
print('EXIT', result.exit_code)
print('LOADED', ','.join(loaded))
print('OUTPUT_OK', {expect!r} in result.output)
"""


def _run_probe(argv: list[str], expect: str) -> tuple[int, str, bool]:
    script = _PROBE.format(argv=argv, prefixes=_FORBIDDEN_PREFIXES, expect=expect)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    exit_code = -1
    loaded = ""
    output_ok = False
    for line in result.stdout.splitlines():
        if line.startswith("EXIT "):
            exit_code = int(line.split(" ", 1)[1])
        elif line.startswith("LOADED "):
            loaded = line.split(" ", 1)[1]
        elif line.startswith("OUTPUT_OK "):
            output_ok = line.split(" ", 1)[1] == "True"
    return exit_code, loaded, output_ok


def test_importing_cli_does_not_load_heavyweight_commands():
    code = (
        "import importlib, sys; "
        "importlib.import_module('apm_cli.cli'); "
        "print(','.join(sorted(m for m in sys.modules if any("
        "m == p or m.startswith(p + '.') for p in "
        f"{_FORBIDDEN_PREFIXES!r}))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_root_help_does_not_load_heavyweight_commands():
    exit_code, loaded, output_ok = _run_probe(["--help"], "Commands:")
    assert exit_code == 0
    assert loaded == ""
    assert output_ok


def test_root_help_lists_lazy_verbs():
    code = r"""
from click.testing import CliRunner
from apm_cli.cli import cli
result = CliRunner().invoke(cli, ['--help'])
print(result.output)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    for verb in ("install", "audit", "pack", "marketplace", "uninstall", "update"):
        assert f"\n  {verb} " in result.stdout or f"\n  {verb}\n" in result.stdout


def test_doctor_help_does_not_load_heavyweight_commands():
    exit_code, loaded, output_ok = _run_probe(["doctor", "--help"], "Usage:")
    assert exit_code == 0
    assert loaded == ""
    assert output_ok


def test_config_get_does_not_load_heavyweight_commands():
    exit_code, loaded, _output_ok = _run_probe(["config", "get"], "")
    assert loaded == ""
    assert exit_code in {0, 1, 2}


def test_install_help_does_load_install_command():
    exit_code, loaded, output_ok = _run_probe(["install", "--help"], "Usage:")
    assert exit_code == 0
    assert "apm_cli.commands.install" in loaded.split(",")
    assert output_ok
