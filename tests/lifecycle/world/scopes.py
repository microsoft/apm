"""Scope and policy variants of a cloned lifecycle world (#4094).

Scopes: ``project`` (the default world), ``user`` (deploys under HOME, the
user manifest and approvals under APM_HOME = HOME/.apm) and ``user`` with
APM_HOME outside HOME (``apm_home_outside_home``).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace

import yaml

from tests.lifecycle.world.contract import World
from tests.lifecycle.world.services import policy_org

# A sha256 no served policy has: the hash-mismatch variant pins it.
WRONG_POLICY_HASH = "sha256:" + "0" * 64


def apm_home_outside_home(world: World) -> World:
    """Move APM_HOME (user manifest, approvals) to ``<root>/apm-home``, outside HOME."""
    target = world.root / "apm-home"
    shutil.move(str(world.apm_home), target)
    env = dict(world.env)
    env["APM_HOME"] = str(target)
    return replace(world, apm_home=target, env=env)


def use_policy(world: World, variant: str) -> None:
    """Make org policy *variant* govern ``world.project``, as its git remote does for a user."""
    org = policy_org(variant)
    git = ("git", "-C", str(world.project))
    if not (world.project / ".git").exists():
        subprocess.run((*git, "init", "-q", "--initial-branch=main"), env=world.env, check=True)
    remote = f"https://github.com/{org}/consumer.git"
    existing = subprocess.run(
        (*git, "remote"), env=world.env, capture_output=True, text=True, check=True
    ).stdout.split()
    verb = "set-url" if "origin" in existing else "add"
    subprocess.run((*git, "remote", verb, "origin", remote), env=world.env, check=True)
    if variant == "hash-mismatch":
        manifest = world.project / "apm.yml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["policy"] = {"hash": WRONG_POLICY_HASH}
        manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
