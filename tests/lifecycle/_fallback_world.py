"""Minimal fallback world until lane T-0W lands ``tests/lifecycle/world`` (#4093, #4094).

It follows the T-0 / T-0W interface contract (``PackageRef``, ``Services``,
``World``, ``build_template``, ``clone`` and the ``lifecycle_template`` /
``lifecycle_world`` fixtures) with two local-path packages: ``p-skill`` (one
skill) and ``p-exec`` (one hook). Whichever of #4093 and #4094 lands second
removes this module.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

_SKILL = "---\nname: p-skill\ndescription: Fallback world skill.\n---\n\nUse the fallback skill.\n"
_HOOK = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo P"}]}]}}


@dataclass(frozen=True)
class PackageRef:
    name: str
    spec: str
    kinds: frozenset[str]
    source: str


class Services:
    """The fallback world serves nothing: every endpoint refuses."""

    git_base_url = marketplace_url = mcp_registry_url = proxy_url = ""

    def policy_url(self, variant: str) -> str:
        raise NotImplementedError(f"fallback world has no policy service ({variant})")

    def host_alias(self, shape: str) -> str:
        raise NotImplementedError(f"fallback world has no host aliases ({shape})")

    def egress_log(self) -> list[dict]:
        return []


@dataclass
class World:
    root: Path
    project: Path
    home: Path
    apm_home: Path
    env: dict[str, str]
    packages: dict[str, PackageRef]
    services: Services = field(default_factory=Services)
    cache_root: Path | None = None


def build_template(base: Path) -> World:
    """Author the packages and an empty consumer project under *base*."""
    base = base.parent.resolve() / base.name
    isolated = IsolatedApmEnvironment.create(base, base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.package_root)
    skill = factory.create("p-skill")
    factory.add_skill(skill, "p-skill", _SKILL)
    hook = factory.create("p-exec")
    factory.add_hook(hook, "session", _HOOK)
    project = isolated.work_root / "project"
    project.mkdir()
    manifest = {
        "name": "lifecycle-consumer",
        "version": "1.0.0",
        "targets": ["copilot"],
        "dependencies": {"apm": []},
        # The executables gate is on and nothing is approved yet.
        "executables": {"allow": {}},
    }
    (project / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    env = isolated.subprocess_env()
    env["APM_NON_INTERACTIVE"] = "1"
    # Wide output keeps one diagnostic per line, so I9 never sees wrap artifacts.
    env["COLUMNS"] = "400"
    packages = {
        "p-skill": PackageRef("p-skill", str(skill.root), frozenset({"skill"}), "local-path"),
        "p-exec": PackageRef("p-exec", str(hook.root), frozenset({"hook"}), "local-path"),
    }
    return World(
        base,
        project,
        isolated.home,
        isolated.config_root,
        env,
        packages,
        cache_root=isolated.cache_root,
    )


def clone(template: World, dest: Path) -> World:
    """Copy *template* to *dest*, rewriting every absolute path it carries."""
    shutil.copytree(template.root, dest, symlinks=True)
    old, new = str(template.root), str(dest)

    def moved(path: Path) -> Path:
        return Path(str(path).replace(old, new, 1))

    return World(
        dest,
        moved(template.project),
        moved(template.home),
        moved(template.apm_home),
        {key: value.replace(old, new) for key, value in template.env.items()},
        {
            name: PackageRef(ref.name, ref.spec.replace(old, new, 1), ref.kinds, ref.source)
            for name, ref in template.packages.items()
        },
        template.services,
        moved(template.cache_root) if template.cache_root else None,
    )


@pytest.fixture(scope="session")
def lifecycle_template(tmp_path_factory: pytest.TempPathFactory) -> World:
    return build_template(tmp_path_factory.mktemp("lifecycle-template") / "world")


@pytest.fixture
def lifecycle_world(lifecycle_template: World, tmp_path: Path) -> World:
    return clone(lifecycle_template, tmp_path / "world")
