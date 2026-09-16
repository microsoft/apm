"""Regression trap: root help and light commands must not import heavy verbs.

``apm --help``, ``apm doctor --help``, and ``apm config get`` must not load
``install``, ``audit``, ``pack``, ``marketplace``, ``uninstall``, ``update``,
or ``prune``. Those modules load only when the matching verb is dispatched.
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
    "apm_cli.commands.prune",
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
    exit_code, loaded, output_ok = _run_probe(
        ["config", "get", "auto-integrate"], "auto-integrate:"
    )
    assert exit_code == 0
    assert loaded == ""
    assert output_ok


def test_install_help_does_load_install_command():
    exit_code, loaded, output_ok = _run_probe(["install", "--help"], "Usage:")
    assert exit_code == 0
    assert "apm_cli.commands.install" in loaded.split(",")
    assert output_ok


def test_lazy_stub_short_help_matches_resolved_command():
    from apm_cli.cli import _LAZY_COMMANDS, _LazyCommand

    mismatches: list[str] = []
    for name, module, attr, help_text in _LAZY_COMMANDS:
        stub = _LazyCommand(name, module, attr, help_text)
        real = stub.resolve()
        stub_short = stub.get_short_help_str(120)
        real_short = real.get_short_help_str(120)
        if stub_short != real_short:
            mismatches.append(f"{name}: stub={stub_short!r} real={real_short!r}")
    assert not mismatches, "Lazy stub short_help drifted from the real command: " + "; ".join(
        mismatches
    )


def test_shell_complete_does_not_load_heavyweight_commands():
    code = (
        "import sys\n"
        "from click.core import Context\n"
        "from apm_cli.cli import cli\n"
        "items = cli.shell_complete(Context(cli), '')\n"
        "print('COUNT', len(items))\n"
        "print('LOADED', ','.join(sorted(m for m in sys.modules if any("
        "m == p or m.startswith(p + '.') for p in "
        f"{_FORBIDDEN_PREFIXES!r}))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    count_line = next(line for line in result.stdout.splitlines() if line.startswith("COUNT "))
    loaded_line = next(line for line in result.stdout.splitlines() if line.startswith("LOADED "))
    assert int(count_line.split(" ", 1)[1]) > 0
    assert loaded_line == "LOADED "
