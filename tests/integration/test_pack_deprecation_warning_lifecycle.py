"""``apm pack`` on a plugin-only package prints the 0.37 target deprecations (#3965).

Plugin-only packages (no ``dependencies:``) never load an ``APMPackage``, so
pack must route its raw target read through the same warning owner that
install and compile use, once per run.
"""

import json
import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = pytest.mark.e2e

_TARGET_KEY = (
    "apm.yml uses the deprecated 'target:' key; 'target:' will be removed in a future "
    "release in favor of 'targets:'. Replace 'target: claude,codex' with "
    "'targets: [claude, codex]'."
)
_VSCODE_ALIAS = (
    "apm.yml: replace `vscode` with `copilot` in 'targets:'; `vscode` is "
    "deprecated and will be removed in a future release."
)
_COMPILATION_TARGET = (
    "apm.yml sets 'compilation.target', which APM ignores; targets come from "
    "'targets:' or auto-detection. Delete it; a future release will reject it."
)


@pytest.mark.parametrize(
    ("declaration", "warning", "manifest"),
    [
        ("target: claude,codex\n", _TARGET_KEY, ".codex-plugin/plugin.json"),
        ("targets: [vscode, codex]\n", _VSCODE_ALIAS, ".github/plugin/plugin.json"),
        (
            "targets: [codex]\ncompilation:\n  target: claude\n",
            _COMPILATION_TARGET,
            ".codex-plugin/plugin.json",
        ),
    ],
    ids=["target-key", "vscode-alias", "compilation-target"],
)
def test_plugin_only_pack_warns_once_with_exact_replacement(
    tmp_path: Path,
    apm_engine_command: tuple[str, ...],
    declaration: str,
    warning: str,
    manifest: str,
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    project = isolated.work_root
    skill = project / "skills" / "hello" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: hello\ndescription: hi\n---\n# hi\n", encoding="utf-8")
    project.joinpath("apm.yml").write_text(
        "name: hello\nversion: 2.3.4\ndescription: Hello\nauthor: Ann\nlicense: MIT\n"
        + declaration,
        encoding="utf-8",
    )

    result = ApmLifecycleRunner(apm_engine_command).run(
        ("pack",), cwd=project, env=isolated.subprocess_env()
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (project / manifest).is_file()
    err = " ".join(result.stderr.split())
    assert err.count(warning) == 1, result.stderr


_LICENSE_WARNING = (
    "No 'license:' field in apm.yml; the SBOM will record NOASSERTION for this "
    "package. Add a 'license:' field to apm.yml (an SPDX expression such as "
    "MIT or Apache-2.0, or UNLICENSED) to declare it."
)


def test_pack_json_keeps_the_missing_license_warning(tmp_path: Path, apm_binary_path: Path) -> None:
    """``apm pack --json`` reports the authoring license nudge in ``warnings`` (#4203)."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    env = dict(isolated.subprocess_env())
    env.pop("CI", None)
    project = isolated.work_root / "my-package"
    project.mkdir()
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    init = runner.run(("init", "-y"), cwd=project, env=env)
    assert init.returncode == 0, init.stdout + init.stderr
    skill = project / ".apm" / "skills" / "hello" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: hello\ndescription: hi\n---\n# hi\n", encoding="utf-8")

    text = runner.run(("pack", "--dry-run"), cwd=project, env=env)
    assert text.returncode == 0, text.stdout + text.stderr
    assert " ".join((text.stdout + text.stderr).split()).count(_LICENSE_WARNING) == 1

    args = ("pack", "--archive", "--check-versions", "--strict-metadata", "--json")
    result = runner.run(args, cwd=project, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.isascii() and result.stderr.isascii()
    report = json.loads(result.stdout)
    assert report["ok"] is True, report
    assert report["warnings"].count(_LICENSE_WARNING) == 1, report
    assert _LICENSE_WARNING not in " ".join(result.stderr.split()), result.stderr
    assert sorted(report["bundle"]["files"]) == ["plugin.json", "skills/hello/SKILL.md"]
    assert (project / report["bundle"]["path"]).is_file()
