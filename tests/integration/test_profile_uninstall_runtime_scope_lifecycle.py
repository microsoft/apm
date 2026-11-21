"""Selected-profile uninstall stays inside the runtime whose profiles split (#3661).

Claude (``CLAUDE_CONFIG_DIR``) and Hermes (``HERMES_HOME``) profiles share
one user manifest and lockfile. When ``apm uninstall -g`` keeps a package
because another profile of runtime A still uses it, only A's selected
profile loses its files and lock rows. The other runtime's default root is
not a profile the user selected, so it keeps its files exactly like a
target without profiles (``copilot``) does.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_RUNTIMES = {"claude": ("CLAUDE_CONFIG_DIR", ".claude"), "hermes": ("HERMES_HOME", ".hermes")}


def _skills(root: Path) -> set[str]:
    return {path.parent.name for path in (root / "skills").glob("*/SKILL.md")}


def _lock_rows(lock_path: Path) -> set[str]:
    lock = load_yaml(lock_path) or {}
    return {
        row
        for dependency in lock.get("dependencies") or []
        for row in dependency.get("deployed_files") or []
        if row.endswith("SKILL.md")
    }


def _declared(manifest_path: Path) -> list[str]:
    manifest = load_yaml(manifest_path) or {}
    return list((manifest.get("dependencies") or {}).get("apm") or [])


@pytest.mark.parametrize("selected", sorted(_RUNTIMES))
def test_profile_uninstall_keeps_other_runtime_default_root(
    tmp_path: Path, apm_binary_path: Path, selected: str
) -> None:
    other = next(name for name in _RUNTIMES if name != selected)
    env_var, _ = _RUNTIMES[selected]
    isolated = IsolatedApmEnvironment.create(tmp_path / "w", base_env=dict(os.environ))
    home = isolated.home
    manifest_path = isolated.config_root / "apm.yml"
    lock_path = isolated.config_root / "apm.lock.yaml"
    dump_yaml({"name": "user", "version": "0.1.0"}, manifest_path)
    factory = LocalPackageFactory(isolated.package_root)
    package = factory.create("profile-skills")
    factory.add_skill(package, "demo", "---\nname: demo\ndescription: Demo.\n---\nDemo.\n")
    profile = isolated.root / "profiles" / selected
    roots = {name: home / directory for name, (_var, directory) in _RUNTIMES.items()}
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=600)

    def run(*args: str, selected_root: Path | None, step: str) -> CommandResult:
        env = isolated.subprocess_env()
        for var, _directory in _RUNTIMES.values():
            env.pop(var, None)
        if selected_root is not None:
            env[env_var] = str(selected_root)
        result = runner.run(args, scenario_id=step, cwd=isolated.work_root, env=env)
        assert result.returncode == 0, f"[{step}]\n{result.stdout}\n{result.stderr}"
        return result

    pkg = str(package.root)
    run(
        "install",
        "-g",
        pkg,
        "--target",
        "claude,hermes,copilot",
        "--no-policy",
        selected_root=None,
        step="1-defaults",
    )
    run(
        "install",
        "-g",
        pkg,
        "--target",
        selected,
        "--no-policy",
        selected_root=profile,
        step="2-profile",
    )
    agents = home / ".agents"
    assert _skills(profile) == _skills(roots[other]) == _skills(roots[selected]) == {"demo"}
    assert _skills(agents) == {"demo"}

    run("uninstall", "-g", pkg, selected_root=profile, step="3-uninstall-profile")
    assert _skills(profile) == set()
    assert _skills(roots[selected]) == {"demo"}
    assert _skills(roots[other]) == {"demo"}, "other runtime's default root was touched"
    assert _skills(agents) == {"demo"}
    rows = _lock_rows(lock_path)
    assert f"{_RUNTIMES[other][1]}/skills/demo/SKILL.md" in rows, rows
    assert f"{_RUNTIMES[selected][1]}/skills/demo/SKILL.md" in rows, rows
    assert not any(row.startswith(profile.as_posix()) for row in rows), rows
    assert len(_declared(manifest_path)) == 1

    # Uninstalling from the defaults removes the package everywhere.
    run("uninstall", "-g", pkg, selected_root=None, step="4-uninstall-defaults")
    for root in (*roots.values(), agents, profile):
        assert _skills(root) == set(), root
    assert _declared(manifest_path) == []


def test_batched_profile_uninstall_scopes_each_package_to_its_own_split(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """One uninstall of two packages removes what two separate uninstalls would.

    ``alpha`` lives in two Hermes homes and only the default Claude root;
    ``beta`` lives in two Claude profiles and only the default Hermes root.
    With ``HERMES_HOME`` selecting alpha's second home, alpha's split runtime
    is Hermes and beta's is Claude, so neither loses the other runtime's files.
    """
    isolated = IsolatedApmEnvironment.create(tmp_path / "w", base_env=dict(os.environ))
    home = isolated.home
    manifest_path = isolated.config_root / "apm.yml"
    lock_path = isolated.config_root / "apm.lock.yaml"
    dump_yaml({"name": "user", "version": "0.1.0"}, manifest_path)
    factory = LocalPackageFactory(isolated.package_root)
    packages = {}
    for name in ("alpha", "beta"):
        package = factory.create(f"{name}-pkg")
        factory.add_skill(package, name, f"---\nname: {name}\ndescription: D.\n---\n{name}\n")
        packages[name] = str(package.root)
    claude, hermes = home / ".claude", home / ".hermes"
    hermes_home = isolated.root / "profiles" / "hermes-2"
    claude_profile = isolated.root / "profiles" / "claude-2"
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=600)

    def run(*args: str, step: str, **selected: Path) -> None:
        env = isolated.subprocess_env()
        for var, _directory in _RUNTIMES.values():
            env.pop(var, None)
        env.update({var: str(root) for var, root in selected.items()})
        result = runner.run(args, scenario_id=step, cwd=isolated.work_root, env=env)
        assert result.returncode == 0, f"[{step}]\n{result.stdout}\n{result.stderr}"

    install = ("install", "-g", "--no-policy", "--target")
    run(*install, "claude,hermes", packages["alpha"], step="1-alpha-defaults")
    run(*install, "hermes", packages["alpha"], step="2-alpha-hermes-2", HERMES_HOME=hermes_home)
    run(*install, "claude,hermes", packages["beta"], step="3-beta-defaults")
    run(
        *install,
        "claude",
        packages["beta"],
        step="4-beta-claude-2",
        CLAUDE_CONFIG_DIR=claude_profile,
    )
    before = {
        "claude": _skills(claude),
        "hermes": _skills(hermes),
        "hermes-2": _skills(hermes_home),
        "claude-2": _skills(claude_profile),
    }
    assert before == {
        "claude": {"alpha", "beta"},
        "hermes": {"alpha", "beta"},
        "hermes-2": {"alpha"},
        "claude-2": {"beta"},
    }, before

    run(
        "uninstall",
        "-g",
        packages["alpha"],
        packages["beta"],
        step="5-batched-uninstall",
        HERMES_HOME=hermes_home,
    )
    # alpha (split in Hermes) leaves only the selected Hermes home; beta
    # (split in Claude) leaves only the selected (default) Claude root.
    assert _skills(hermes_home) == set()
    assert _skills(claude) == {"alpha"}, "batched uninstall removed alpha's Claude files"
    assert _skills(hermes) == {"alpha", "beta"}, "batched uninstall removed beta's Hermes files"
    assert _skills(claude_profile) == {"beta"}
    rows = _lock_rows(lock_path)
    assert {".claude/skills/alpha/SKILL.md", ".hermes/skills/beta/SKILL.md"} <= rows, rows
    assert ".claude/skills/beta/SKILL.md" not in rows, rows
    assert not any(row.startswith(hermes_home.as_posix()) for row in rows), rows
    assert set(_declared(manifest_path)) == set(packages.values())
