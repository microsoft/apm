"""Real-binary contract: uninstall keeps survivors' skills: selection (#3760).

Uninstalling an unrelated package re-integrates the surviving dependencies.
That rebuild must deploy the same effective skill selection install deployed,
not every skill of a surviving bundle.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_BUNDLE_SKILLS = ("alpha", "beta", "delta", "gamma")


def _evidence(result: CommandResult) -> str:
    """Return stable process evidence when a lifecycle command misbehaves."""
    return (
        f"command={result.command!r}\n"
        f"returncode={result.returncode}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )


def _write_bundle(package: Path) -> None:
    """Author a package that ships four skills."""
    (package / "apm.yml").write_text("name: bundle\nversion: 1.0.0\n", encoding="utf-8")
    for name in _BUNDLE_SKILLS:
        skill = package / ".apm" / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Demo skill.\n---\n# {name}\n",
            encoding="utf-8",
        )


def _write_unrelated(package: Path) -> None:
    """Author an unrelated instruction-only package."""
    (package / "apm.yml").write_text("name: pkg-c\nversion: 1.0.0\n", encoding="utf-8")
    instructions = package / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "c.instructions.md").write_text(
        '---\ndescription: "Unrelated."\napplyTo: "**"\n---\n\nUnrelated.\n',
        encoding="utf-8",
    )


def _write_manifest(manifest: Path, bundle: str, unrelated: str) -> None:
    """Pin one skill of the bundle and add the unrelated package."""
    manifest.write_text(
        "name: consumer\n"
        "version: 1.0.0\n"
        "targets: [copilot]\n"
        "dependencies:\n"
        "  apm:\n"
        f"    - path: {bundle}\n"
        "      skills: [alpha]\n"
        f"    - {unrelated}\n"
        "  mcp: []\n",
        encoding="utf-8",
    )


def _deployed_skill_dirs(deploy_root: Path) -> set[str]:
    skills_root = deploy_root / ".agents" / "skills"
    if not skills_root.is_dir():
        return set()
    return {path.name for path in skills_root.iterdir() if path.is_dir()}


def _locked_files(lockfile: Path) -> set[str]:
    data = yaml.safe_load(lockfile.read_text(encoding="utf-8")) or {}
    files: set[str] = set()
    for dependency in data.get("dependencies") or []:
        files.update(dependency.get("deployed_files") or [])
    return files


@pytest.mark.parametrize("scope", ["project", "user"])
def test_uninstall_unrelated_package_keeps_survivor_skill_selection(
    tmp_path: Path,
    apm_binary_path: Path,
    scope: str,
) -> None:
    """Uninstalling pkg-c leaves only the selected alpha skill of the bundle."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    bundle = isolated.work_root / "bundle"
    unrelated = isolated.work_root / "pkg-c"
    bundle.mkdir()
    unrelated.mkdir()
    _write_bundle(bundle)
    _write_unrelated(unrelated)

    if scope == "user":
        manifest_dir = isolated.home / ".apm"
        deploy_root = isolated.home
        cwd = isolated.work_root
        scope_args: tuple[str, ...] = ("-g",)
        bundle_spec, unrelated_spec = str(bundle), str(unrelated)
    else:
        manifest_dir = isolated.work_root / "project"
        deploy_root = manifest_dir
        cwd = manifest_dir
        scope_args = ()
        bundle_spec, unrelated_spec = "../bundle", "../pkg-c"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    _write_manifest(manifest_dir / "apm.yml", bundle_spec, unrelated_spec)
    lockfile = manifest_dir / "apm.lock.yaml"
    runner = ApmLifecycleRunner((str(apm_binary_path),))

    def run(args: tuple[str, ...], scenario_id: str) -> CommandResult:
        return runner.run(
            args,
            scenario_id=f"uninstall-skill-selection-{scope}-{scenario_id}",
            cwd=cwd,
            env=isolated.subprocess_env(),
        )

    installed = run(("install", *scope_args), "install")
    assert installed.returncode == 0, _evidence(installed)
    assert _deployed_skill_dirs(deploy_root) == {"alpha"}, _evidence(installed)

    removed = run(("uninstall", *scope_args, unrelated_spec), "uninstall")
    assert removed.returncode == 0, _evidence(removed)
    assert _deployed_skill_dirs(deploy_root) == {"alpha"}, _evidence(removed)
    locked = _locked_files(lockfile)
    assert ".agents/skills/alpha/SKILL.md" in locked, _evidence(removed)
    unrecorded = {
        name for name in _deployed_skill_dirs(deploy_root) if f".agents/skills/{name}" not in locked
    }
    assert not unrecorded, _evidence(removed)

    if scope == "project":
        audit = run(("audit", "--ci"), "audit")
        assert audit.returncode == 0, _evidence(audit)
