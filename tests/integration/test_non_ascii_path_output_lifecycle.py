"""APMLifecycle: a non-ASCII package path prints escaped, never raw (#4330).

PROMISE: P-INV-I6 -- output is printable ASCII, so a cp1252 terminal never
raises UnicodeEncodeError (source: .github/instructions/encoding.instructions.md)
CELL: OP=install|deps-list|uninstall SRC=local PRIM=skill SCOPE=project TGT=claude
OWNER: src/apm_cli/utils/console.py (the ASCII Rich console every helper prints through)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I6"),
]

_NAME = "pkg-\u00fcber"
_ESCAPED = "pkg-\\xfcber"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _assert_escaped(result: CommandResult, expected_lines: tuple[str, ...]) -> None:
    assert result.returncode == 0, _evidence(result)
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), _evidence(result)
        assert "\u00fc" not in stream, _evidence(result)
    lines = [line.rstrip() for line in result.stdout.splitlines()]
    for expected in expected_lines:
        assert expected in lines, f"missing {expected!r}\n{_evidence(result)}"


def test_non_ascii_package_path_is_echoed_escaped(tmp_path: Path, apm_binary_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    env["COLUMNS"] = "500"
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    # The project directory is ASCII, so the oracle's cwd exemption cannot
    # excuse a raw echo of the package path.
    packages = LocalPackageFactory(isolated.work_root)
    package = packages.create(_NAME)
    packages.add_skill(package, "uber-skill", "---\nname: uber-skill\ndescription: d\n---\nBody\n")
    project = packages.create("consumer", targets=("claude",))
    escaped_path = str(package.root).encode("ascii", "backslashreplace").decode("ascii")

    def apm(*args: str) -> CommandResult:
        return runner.run(args, cwd=project.root, env=env)

    installed = apm("install", str(package.root))
    _assert_escaped(
        installed,
        (f"[+] {escaped_path}", f"  [+] {escaped_path} (local)"),
    )
    assert (project.root / ".claude" / "skills" / "uber-skill" / "SKILL.md").is_file()

    listed = apm("deps", "list")
    _assert_escaped(listed, ())
    assert f"_local/{_ESCAPED}" in listed.stdout, _evidence(listed)

    removed = apm("uninstall", str(package.root))
    _assert_escaped(
        removed,
        (
            f"[+] _local/{_ESCAPED} - found in apm.yml",
            f"[i] Removed _local/{_ESCAPED} from dependencies.apm in apm.yml",
        ),
    )
    assert not (project.root / ".claude" / "skills" / "uber-skill").exists()
