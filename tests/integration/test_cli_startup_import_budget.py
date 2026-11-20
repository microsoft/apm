"""Installed-CLI startup import budget (owner: cli-startup-import-budget, #4118).

``apm --help`` and ``apm --version`` run on every interactive session and in
every binary-driven test. They must not import command modules or GitPython
(whose import spawns ``git version``). The guard is deterministic: it
measures the imported-module set in-process with ``-X importtime`` against
the installed console script and asserts a recorded count budget plus a
must-not-import list. Wall-clock ratio against ``python -c pass`` is recorded
as a non-gating benchmark signal only.
"""

from __future__ import annotations

import os
import re
import statistics
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

# Recorded on Linux CPython 3.12 after #4118: --help 332, --version 334
# (main before #4118: --help 767 including 37 GitPython modules).
MODULE_BUDGET = 400
MUST_NOT_IMPORT = ("git", "gitdb", "smmap", "apm_cli.commands", "apm_cli.deps.github_downloader")
_STARTUP_ARGS = (("--help",), ("--version",))
_IMPORT_LINE = re.compile(r"^import time:\s+\d+\s+\|\s+\d+\s+\|\s?(?P<name>.*)$")
_SH_EXEC = re.compile(r"""^'''exec' ['"]?(?P<python>[^'"\s]+)""", re.MULTILINE)


def _console_script_python(apm: Path) -> str:
    """Return the interpreter of a Python console script, or skip a frozen binary."""
    head = apm.read_bytes()[:4096]
    if not head.startswith(b"#!"):
        pytest.skip(f"{apm} is not a Python console script; -X importtime needs CPython")
    text = head.decode("utf-8", errors="replace")
    shebang = text.splitlines()[0][2:].strip()
    if shebang.endswith("/sh"):
        match = _SH_EXEC.search(text)
        assert match, f"cannot find interpreter in sh-wrapped console script {apm}"
        return match.group("python")
    return shebang.split()[0]


def _imported_modules(python: str, apm: Path, args: tuple[str, ...], env: dict) -> set[str]:
    result = subprocess.run(
        [python, "-X", "importtime", str(apm), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    modules = set()
    for line in result.stderr.splitlines():
        match = _IMPORT_LINE.match(line)
        if match:
            modules.add(match.group("name").strip())
    assert modules, "no -X importtime records captured"
    return modules


def _forbidden(modules: set[str]) -> list[str]:
    return sorted(
        name
        for name in modules
        if any(name == root or name.startswith(root + ".") for root in MUST_NOT_IMPORT)
    )


@pytest.fixture
def startup_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "COLUMNS": "80"})
    env.pop("PYTHONPROFILEIMPORTTIME", None)
    return env


@pytest.mark.parametrize("args", _STARTUP_ARGS, ids=lambda a: " ".join(a))
def test_installed_cli_startup_stays_within_import_budget(
    apm_binary_path: Path, startup_env: dict[str, str], args: tuple[str, ...]
) -> None:
    python = _console_script_python(apm_binary_path)
    modules = _imported_modules(python, apm_binary_path, args, startup_env)

    assert _forbidden(modules) == [], f"apm {' '.join(args)} imported forbidden modules"
    assert len(modules) <= MODULE_BUDGET, (
        f"apm {' '.join(args)} imported {len(modules)} modules (budget {MODULE_BUDGET})"
    )


def test_installed_cli_startup_ratio_is_recorded(
    apm_binary_path: Path,
    startup_env: dict[str, str],
    record_property: Callable[[str, object], None],
) -> None:
    """Non-gating benchmark: wall-clock ratio of ``apm --help`` to bare Python."""
    python = _console_script_python(apm_binary_path)

    def median_seconds(command: list[str]) -> float:
        samples = []
        for _ in range(5):
            start = time.perf_counter()
            subprocess.run(command, capture_output=True, env=startup_env, timeout=120, check=False)
            samples.append(time.perf_counter() - start)
        return statistics.median(samples)

    baseline = median_seconds([python, "-c", "pass"])
    startup = median_seconds([str(apm_binary_path), "--help"])
    record_property("apm_help_startup_ratio", round(startup / baseline, 2))
