"""A required package missing from the lock is recorded by a plain ``apm install``.

``apm audit --ci`` (and ``install --frozen``) refuse a lock that does not
record a declared package the org policy requires. The refusal names plain
``apm install`` (the minimal converging remedy, not ``apm update``) and never
a policy bypass; running exactly that records the package and both replays
then pass (no install-gate catch-22).
"""

from __future__ import annotations

import subprocess

import pytest
import yaml

from apm_cli.core.remediation import REQUIRED_PACKAGE_UNLOCKED
from tests.lifecycle import driver
from tests.lifecycle.world import World
from tests.lifecycle.world.services import ORG

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.promise("P-OWN-remediation-advice", "P-INV-I7"),
]

_POLICY_ORG = "policy-require-remedy"
_REQUIRED = f"{ORG}/p-skill"


def _serve_policy(world: World, document: dict) -> None:
    tree = world.root / "require-policy-src"
    tree.mkdir()
    (tree / "apm-policy.yml").write_text(yaml.safe_dump(document), encoding="utf-8")
    bare = world.root / "require-policy.git"
    git = ("git", "-c", "user.name=lifecycle", "-c", "user.email=lifecycle@example.invalid")
    for args in (
        ("-C", str(tree), "init", "-q", "--initial-branch=main"),
        ("-C", str(tree), "add", "-A"),
        ("-C", str(tree), "commit", "-qm", "policy"),
        ("clone", "-q", "--bare", str(tree), str(bare)),
    ):
        subprocess.run((*git, *args), env=world.env, check=True, capture_output=True)
    world.services.serve(f"{_POLICY_ORG}/.github", bare)
    project_git = ("git", "-C", str(world.project))
    if not (world.project / ".git").exists():
        subprocess.run(
            (*project_git, "init", "-q", "--initial-branch=main"), env=world.env, check=True
        )
    remote = f"https://github.com/{_POLICY_ORG}/consumer.git"
    subprocess.run((*project_git, "remote", "add", "origin", remote), env=world.env, check=True)


def _declare(world: World, *specs: str) -> None:
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data.setdefault("dependencies", {})["apm"] = list(specs)
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def _flat(result) -> str:
    return " ".join((result.stdout + result.stderr).split())


def test_required_package_missing_from_the_lock_converges_with_plain_install(
    lifecycle_world: World,
) -> None:
    world = lifecycle_world
    local = world.packages["p-parent"].spec
    _declare(world, local)
    installed = driver.run(world, ["install"])
    assert installed.returncode == 0, _flat(installed)

    # The project declares a required package the lock does not record yet.
    _declare(world, local, world.packages["p-skill:git"].spec)
    _serve_policy(
        world,
        {
            "name": "require-remedy",
            "version": "1.0",
            "enforcement": "block",
            "dependencies": {"require": [_REQUIRED]},
        },
    )
    # Fail-fast (the default): the first failing check names the remedy.
    refused = driver.run(world, ["audit", "--ci"])
    assert refused.returncode == 1, _flat(refused)
    assert "1 ref mismatch -- run 'apm install' to update lockfile" in _flat(refused)
    for bypass in ("--no-policy", "APM_POLICY_DISABLE"):
        assert bypass not in _flat(refused), _flat(refused)
    # The policy check, once reached, names the same remedy.
    every = driver.run(world, ["audit", "--ci", "--no-fail-fast"])
    expected = f"1 required package(s) absent from the lockfile. Hint: {REQUIRED_PACKAGE_UNLOCKED.sentence()}"
    assert expected in _flat(every), _flat(every)

    remedy = driver.run(world, ["install"])
    assert remedy.returncode == 0, _flat(remedy)
    locked = yaml.safe_load((world.project / "apm.lock.yaml").read_text(encoding="utf-8"))
    assert _REQUIRED in {row.get("repo_url") for row in locked["dependencies"]}, locked

    for argv in (["audit", "--ci"], ["install", "--frozen"]):
        replay = driver.run(world, argv)
        assert replay.returncode == 0, (argv, _flat(replay))
