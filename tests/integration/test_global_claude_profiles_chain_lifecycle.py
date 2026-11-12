"""``-g`` chains across Claude profiles never cross-delete or resurrect (#3424, #3555, #3649).

Three Claude profiles (default ``~/.claude``, P1, P2) share one user manifest
and lockfile. Git-sourced packages are installed into different profiles,
the cache is cleaned between steps, and update / deps list / uninstall run
per profile. After every step the deployed skills and lockfile rows of every
profile are asserted so cross-profile deletion or resurrection is caught.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._policy_scope_kit import PolicyScopeWorld, output, run_step
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.local_github_https_fixture import (
    local_github_https_fixture,
    openssl_executable,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]


def _skills(root: Path) -> set[str]:
    return {path.parent.name for path in (root / "skills").glob("*/SKILL.md")}


def _lock_skills(lock_path: Path, home: Path, root: Path) -> set[str]:
    if not lock_path.exists():
        return set()
    resolved = root.resolve()
    try:
        prefix = resolved.relative_to(home.resolve()).as_posix()
    except ValueError:
        prefix = resolved.as_posix()
    prefix = f"{prefix}/skills/"
    lock = load_yaml(lock_path) or {}
    rows = {
        row
        for dependency in lock.get("dependencies") or []
        for row in dependency.get("deployed_files") or []
    }
    return {row[len(prefix) :].split("/", 1)[0] for row in rows if row.startswith(prefix)}


def _declared(manifest_path: Path) -> list[str]:
    manifest = load_yaml(manifest_path) or {}
    return list((manifest.get("dependencies") or {}).get("apm") or [])


def test_global_claude_profiles_chain_with_cache_clean(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg_a = world.publish_package("pkg-a")
    pkg_b = world.publish_package("pkg-b")
    home = world.isolated.home
    manifest_path = world.isolated.config_root / "apm.yml"
    lock_path = world.isolated.config_root / "apm.lock.yaml"
    dump_yaml({"name": "user-home", "version": "0.1.0"}, manifest_path)
    profiles = {
        "default": home / ".claude",
        "p1": world.isolated.root / "profiles" / "p1",
        "p2": home / "profiles" / "p2",
    }
    cwd = world.isolated.work_root
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        base_env = fx.child_environment(world.git_env())

        def env_for(profile: str) -> dict[str, str]:
            env = dict(base_env)
            env["CLAUDE_CONFIG_DIR"] = "" if profile == "default" else str(profiles[profile])
            return env

        def g(profile: str, *args: str, step: str, expected: int = 0):
            return run_step(
                runner,
                args,
                cwd=cwd,
                env=env_for(profile),
                step=f"{step}@{profile}",
                expected=expected,
            )

        def state() -> dict[str, tuple[set[str], set[str]]]:
            return {
                name: (_skills(root), _lock_skills(lock_path, home, root))
                for name, root in profiles.items()
            }

        install = ("install", "-g", "--target", "claude", "--no-policy")

        g("p1", *install, f"{pkg_a.slug}#v1.0.0", step="1-install-a")
        assert state()["p1"] == ({"pkg-a-skill"}, {"pkg-a-skill"})
        assert state()["default"] == (set(), set())
        assert state()["p2"] == (set(), set())

        g("p2", *install, f"{pkg_b.slug}#v1.0.0", step="2-install-b")
        after_2 = state()
        assert after_2["p1"] == ({"pkg-a-skill"}, {"pkg-a-skill"}), after_2
        assert "pkg-b-skill" in after_2["p2"][0], after_2
        assert after_2["p2"][0] == after_2["p2"][1], after_2

        g("default", *install, f"{pkg_b.slug}#v1.0.0", step="3-install-b")
        after_3 = state()
        assert after_3["p1"] == after_2["p1"], after_3
        assert after_3["p2"] == after_2["p2"], after_3
        assert "pkg-b-skill" in after_3["default"][0], after_3
        assert after_3["default"][0] == after_3["default"][1], after_3
        assert sorted(_declared(manifest_path)) == sorted(
            [f"{pkg_a.slug}#v1.0.0", f"{pkg_b.slug}#v1.0.0"]
        )

        # A bare restore deploys the whole user manifest into the selected
        # profile only; a second restore after a cache wipe is a no-op.
        g("default", "cache", "clean", "--yes", step="4-cache-clean")
        g("default", *install, step="4b-restore")
        after_4 = state()
        assert after_4["default"] == ({"pkg-a-skill", "pkg-b-skill"},) * 2, after_4
        assert after_4["p1"] == after_3["p1"], after_4
        assert after_4["p2"] == after_3["p2"], after_4
        lock_before = lock_path.read_bytes()
        g("default", "cache", "clean", "--yes", step="4c-cache-clean")
        g("default", *install, step="4d-restore-again")
        assert state() == after_4, state()
        assert lock_path.read_bytes() == lock_before, "repeat restore churned the lockfile"
        after_3 = after_4

        # deps list -g agrees with the manifest in every profile.
        for profile in profiles:
            r = g(profile, "deps", "list", "-g", step="5-deps-list")
            for pkg in (pkg_a, pkg_b):
                assert pkg.name in output(r), output(r)

        # update in p1 leaves the other profiles byte-identical.
        g("default", "cache", "clean", "--yes", step="6-cache-clean")
        g("p1", "update", "-g", "--yes", step="6b-update")
        after_6 = state()
        assert after_6["default"] == after_3["default"], after_6
        assert after_6["p2"] == after_3["p2"], after_6

        # uninstall pkg-b from p2 only.
        g("p2", "uninstall", "-g", pkg_b.slug, step="7-uninstall-b")
        after_7 = state()
        assert "pkg-b-skill" not in after_7["p2"][0], after_7
        assert "pkg-b-skill" not in after_7["p2"][1], after_7
        assert "pkg-b-skill" in after_7["default"][0], after_7
        assert "pkg-b-skill" in after_7["default"][1], after_7
        assert after_7["p1"] == after_6["p1"], after_7
        assert f"{pkg_b.slug}#v1.0.0" in _declared(manifest_path)

        # uninstall pkg-b from default -> no profile has it, manifest drops it.
        g("default", "uninstall", "-g", pkg_b.slug, step="8-uninstall-b")
        after_8 = state()
        for profile, (disk, rows) in after_8.items():
            assert "pkg-b-skill" not in disk, (profile, after_8)
            assert "pkg-b-skill" not in rows, (profile, after_8)
        assert f"{pkg_b.slug}#v1.0.0" not in _declared(manifest_path)
        assert "pkg-a-skill" in after_8["p1"][0]

        # uninstall pkg-a from every profile that still holds it.
        for profile in profiles:
            if "pkg-a-skill" in state()[profile][1]:
                g(profile, "uninstall", "-g", pkg_a.slug, step="9-uninstall-a")
        for profile, (disk, rows) in state().items():
            assert disk == set(), (profile, state())
            assert rows == set(), (profile, state())
        assert _declared(manifest_path) == []
