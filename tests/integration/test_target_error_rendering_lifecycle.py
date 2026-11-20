"""Installed-CLI contract: target errors are formatted once, by the CLI (#3871).

``TargetResolutionError`` messages used to carry their own ``[x]`` status
prefix, so Click printed ``Error: [x] Unknown target ...`` and logger callers
had to suppress their own symbol. The message is now plain text; the CLI
output owner (Click here, ``CommandLogger.error`` elsewhere) adds the one
status prefix.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]


@pytest.mark.parametrize(
    ("manifest", "args", "expected"),
    [
        pytest.param(
            "name: c\nversion: 0.1.0\n",
            ("install", "--no-policy", "--target", "bogus", "./pkg"),
            "Error: Unknown target 'bogus'",
            id="flag-via-click",
        ),
        pytest.param(
            "name: c\nversion: 0.1.0\ntargets: [bogus]\n",
            ("install", "--no-policy"),
            "Error: Unknown target 'bogus'",
            id="manifest-via-click",
        ),
    ],
)
def test_target_error_has_one_status_prefix(
    tmp_path: Path, apm_binary_path: Path, manifest: str, args: tuple[str, ...], expected: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = dict(isolated.subprocess_env())
    env.pop("CI", None)
    project = isolated.work_root / "consumer"
    project.mkdir(parents=True)
    (project / "apm.yml").write_text(manifest, encoding="ascii")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    result = runner.run(args, cwd=project, env=env)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert expected in output, output
    assert "Error: [x]" not in output and "[x] [x]" not in output, output
    assert output.count("Unknown target 'bogus'") == 1, output


def test_logger_rendered_target_error_has_one_status_prefix(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """``apm compile -g`` reports through CommandLogger, which adds the one ``[x]``."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = dict(isolated.subprocess_env())
    env.pop("CI", None)
    (isolated.config_root / "apm.yml").write_text(
        "name: u\nversion: 0.1.0\ntargets: [bogus]\n", encoding="ascii"
    )
    (isolated.config_root / "apm_modules").mkdir(parents=True, exist_ok=True)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    result = runner.run(("compile", "-g"), cwd=isolated.work_root, env=env)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert output.count("[x] Unknown target 'bogus'") == 1, output
    assert "[x] [x]" not in output and "Error: [x]" not in output, output
