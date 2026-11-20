"""Scope and policy variants of a cloned lifecycle world (#4094).

Scopes: ``project`` (the default world), ``user`` (deploys under HOME, the
user manifest and approvals under APM_HOME = HOME/.apm) and ``user`` with
APM_HOME outside HOME (``apm_home_outside_home``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import yaml

from tests.lifecycle.world import registry, routes
from tests.lifecycle.world.contract import World
from tests.lifecycle.world.services import ADO_BEARER, ADO_PAT, HOST_ALIASES, policy_org
from tests.lifecycle.world.shims import AZ_TOKEN_FILE

# A sha256 no served policy has: the hash-mismatch variant pins it.
WRONG_POLICY_HASH = "sha256:" + "0" * 64


def apm_home_outside_home(world: World) -> World:
    """Move APM_HOME (user manifest, approvals) to ``<root>/apm-home``, outside HOME."""
    target = world.root / "apm-home"
    shutil.move(str(world.apm_home), target)
    env = dict(world.env)
    env["APM_HOME"] = str(target)
    return replace(world, apm_home=target, env=env)


def use_policy(world: World, variant: str, host: str = "github") -> None:
    """Make org policy *variant* govern ``world.project``, as its git remote does for a user.

    ``host`` picks the remote's host: ``github`` (Contents API on api.github.com),
    ``ghe`` (GHE Cloud, ``https://<host>/api/v3``) or ``ado`` (Azure DevOps Items API).
    """
    org = policy_org(variant)
    git = ("git", "-C", str(world.project))
    if not (world.project / ".git").exists():
        subprocess.run((*git, "init", "-q", "--initial-branch=main"), env=world.env, check=True)
    remote = {
        "github": f"https://github.com/{org}/consumer.git",
        "ghe": f"https://{HOST_ALIASES['ghe'][0]}/{org}/consumer.git",
        "ado": f"https://{HOST_ALIASES['ado'][0]}/{org}/lifecycle/_git/consumer",
    }[host]
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


def use_registry(world: World, scope: str = "project") -> None:
    """Make the world package registry the default registry of *scope*'s manifest.

    Shorthand specs such as ``lifecycle/p-skill#1.0.0`` (``packages["p-skill:registry"]``)
    then resolve through it; the token and origin binding are already in the world.
    """
    root = world.project if scope == "project" else world.apm_home
    manifest = root / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["registries"] = {registry.NAME: {"url": registry.URL}, "default": registry.NAME}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def use_ado_pat(world: World) -> None:
    """Give the CLI the world's Azure DevOps PAT (``ADO_APM_PAT``)."""
    world.env["ADO_APM_PAT"] = ADO_PAT


def login_azure(world: World) -> None:
    """Log the world's ``az`` stub in: it then hands APM the world's ADO bearer."""
    config = Path(world.env["AZURE_CONFIG_DIR"])
    config.mkdir(parents=True, exist_ok=True)
    token = {
        "accessToken": ADO_BEARER,
        "expiresOn": "2099-01-01 00:00:00.000000",
        "tenant": "lifecycle-tenant",
        "tokenType": "Bearer",
    }
    (config / AZ_TOKEN_FILE).write_text(json.dumps(token), encoding="ascii")


def use_registry_proxy(world: World, *, only: bool = True) -> None:
    """Route package downloads through the world's enterprise registry proxy.

    Sets ``PROXY_REGISTRY_URL`` and its token, and ``PROXY_REGISTRY_ONLY=1``
    when *only*. Pair with ``world.services.forbid_direct()`` to prove nothing
    goes direct.
    """
    world.env["PROXY_REGISTRY_URL"] = routes.PROXY_URL
    world.env["PROXY_REGISTRY_TOKEN"] = routes.PROXY_TOKEN
    if only:
        world.env["PROXY_REGISTRY_ONLY"] = "1"
    else:
        world.env.pop("PROXY_REGISTRY_ONLY", None)
