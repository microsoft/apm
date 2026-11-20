"""Build the lifecycle template world once per pytest worker (#4094).

Layout under ``base`` (an ``IsolatedApmEnvironment`` root, cloned per test)::

    home/            HOME; home/.apm is APM_HOME (user manifest + approvals)
    cache/           APM_CACHE_DIR
    packages/        every world package (local-path sources)
    work/project/    the consumer project (apm.yml, executables gate on)
    work/elsewhere/  the cwd of user-scope commands (no apm.yml)
    bundles/         ``apm pack`` output of p-exec (default plugin format)
    bundles-apm/     ``apm pack --format apm`` output of p-exec (what ``apm unpack`` reads)
    executables/     trap ``claude`` / ``codex`` binaries that must never run

APM_HOME/config.json enables the experimental ``canvas`` primitive.

Shared per worker, never cloned (``shared-<base name>`` beside ``base``)::

    repositories/    bare git repositories served by the local HTTPS server
    services/        CA material of the recording proxy
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from tests.lifecycle.world.contract import PackageRef, World
from tests.lifecycle.world.packages import GIT_PUBLISHED, author_packages
from tests.lifecycle.world.services import (
    MARKETPLACE_NAME,
    MARKETPLACE_REPO,
    ORG,
    POLICY_FILE,
    POLICY_REPO,
    WorldServices,
    policy_documents,
    policy_org,
)
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

TAG = "v1.0.0"
_NATIVE = frozenset({"p-native-claude", "p-native-codex", "p-native-claude-bin"})
_TRAPPED = ("claude", "codex")


def _manifest(name: str) -> dict[str, object]:
    return {
        "name": name,
        "version": "1.0.0",
        "targets": ["copilot"],
        "dependencies": {"apm": []},
        # The executables gate is on and nothing is approved yet.
        "executables": {"allow": {}},
    }


def _write_manifest(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(_manifest(name)), encoding="utf-8")


def _traps(root: Path) -> Path:
    """Native CLIs on PATH that record any call: APM must never run them."""
    directory = root / "executables"
    directory.mkdir()
    for name in _TRAPPED:
        trap = directory / name
        trap.write_text(f"#!/bin/sh\ntouch '{root / f'{name}-was-run'}'\nexit 91\n", "ascii")
        trap.chmod(0o755)
    return directory


def _publish(shared: Path, env: dict[str, str], packages) -> dict[str, Path]:
    """Commit each served repository; return ``owner/repo`` -> bare repository."""
    factory = LocalGitRepositoryFactory(shared / "repositories", env=env)
    served: dict[str, Path] = {}

    def publish(owner: str, repo: str, tree: Path) -> None:
        repository = factory.create(f"{owner}__{repo.lstrip('.')}", source_tree=tree)
        commit = factory.commit(repository, message=f"{owner}/{repo} {TAG}")
        factory.tag(repository, TAG, commit)
        served[f"{owner}/{repo}"] = repository.origin

    for name in GIT_PUBLISHED:
        publish(ORG, name, packages[name].root)
    trees = shared / "trees"
    for variant, document in policy_documents().items():
        tree = trees / policy_org(variant)
        tree.mkdir(parents=True)
        (tree / POLICY_FILE).write_text(yaml.safe_dump(document), encoding="utf-8")
        publish(policy_org(variant), POLICY_REPO, tree)
    catalog = trees / MARKETPLACE_REPO
    (catalog / ".claude-plugin").mkdir(parents=True)
    plugins = [
        {
            "name": name,
            "description": f"Lifecycle world package {name}",
            "source": {"source": "github", "repo": f"{ORG}/{name}", "ref": TAG},
        }
        for name in GIT_PUBLISHED
    ]
    document = {"name": MARKETPLACE_NAME, "owner": {"name": "APM Lifecycle"}, "plugins": plugins}
    (catalog / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(document, indent=2) + "\n", encoding="ascii"
    )
    publish(ORG, MARKETPLACE_REPO, catalog)
    return served


def _engine(args: tuple[str, ...], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        (sys.executable, "-m", "apm_cli.cli", *args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _pack_bundle(
    source: Path, output: Path, env: dict[str, str], *, bundle_format: str | None = None
) -> Path:
    """Pack *source* with the engine CLI; return the bundle it wrote."""
    output.mkdir()
    work = output.parent / f"pack-src-{output.name}"
    shutil.copytree(source, work)
    fmt = ("--format", bundle_format) if bundle_format else ()
    # Packing must not leave receipts or cache in the template's HOME.
    scratch = output.parent / f"pack-home-{output.name}"
    env = {
        **env,
        "HOME": str(scratch),
        "USERPROFILE": str(scratch),
        "APM_HOME": str(scratch / ".apm"),
        "APM_CACHE_DIR": str(scratch / "cache"),
    }
    (scratch / ".apm").mkdir(parents=True)
    result = None
    if bundle_format == "apm":
        # The APM layout embeds the lockfile, so resolve first.
        result = _engine(("install", "--target", "copilot"), work, env)
    if result is None or result.returncode == 0:
        result = _engine(("pack", *fmt, "--archive", "-o", str(output)), work, env)
    shutil.rmtree(work)
    shutil.rmtree(scratch)
    bundles = sorted(output.iterdir())
    if result.returncode != 0 or len(bundles) != 1:
        raise RuntimeError(f"apm pack failed ({result.returncode}): {result.stdout}{result.stderr}")
    return bundles[0]


def build_template(base: Path) -> World:
    """Author the template world under *base* (which must not exist yet) and start services."""
    base = base.parent.resolve() / base.name
    shared = base.parent / f"shared-{base.name}"
    shared.mkdir()
    isolated = IsolatedApmEnvironment.create(base, base_env=dict(os.environ))
    authored = author_packages(isolated.package_root)
    repositories = _publish(shared, isolated.subprocess_env(), authored)
    services = WorldServices(repositories).start(shared / "services")

    env = isolated.subprocess_env()
    env["APM_NON_INTERACTIVE"] = "1"
    # Wide output keeps one diagnostic per line, so I9 never sees wrap artifacts.
    env["COLUMNS"] = "400"
    env["CLAUDE_CONFIG_DIR"] = str(isolated.home / ".claude")
    env["CODEX_HOME"] = str(isolated.home / ".codex")
    env["PATH"] = os.pathsep.join((str(_traps(base)), env.get("PATH", "")))
    env = services.child_environment(env)
    for user_root in (".copilot", ".claude", ".codex"):
        (isolated.home / user_root).mkdir()

    project = isolated.work_root / "project"
    _write_manifest(project / "apm.yml", "lifecycle-consumer")
    _write_manifest(isolated.config_root / "apm.yml", "lifecycle-user")
    user_cwd = isolated.work_root / "elsewhere"
    user_cwd.mkdir()
    try:
        # Canvas is an experimental primitive; the world's user has turned it on.
        enabled = _engine(("experimental", "enable", "canvas"), user_cwd, env)
        if enabled.returncode != 0:
            raise RuntimeError(f"enabling canvas failed: {enabled.stdout}{enabled.stderr}")
        bundle = _pack_bundle(authored["p-exec"].root, base / "bundles", env)
        # ``apm unpack`` reads only the legacy APM bundle layout.
        apm_bundle = _pack_bundle(
            authored["p-exec"].root, base / "bundles-apm", env, bundle_format="apm"
        )
    except Exception:
        services.close()
        raise

    packages = {
        name: PackageRef(
            name,
            str(package.root),
            package.kinds,
            "native-plugin" if name in _NATIVE else "local-path",
        )
        for name, package in authored.items()
    }
    for name in GIT_PUBLISHED:
        kinds = authored[name].kinds
        packages[f"{name}:git"] = PackageRef(name, f"{ORG}/{name}#{TAG}", kinds, "git")
        packages[f"{name}:marketplace"] = PackageRef(
            name, f"{name}@{MARKETPLACE_NAME}", kinds, "marketplace"
        )
    packages["p-exec:bundle"] = PackageRef(
        "p-exec", str(bundle), authored["p-exec"].kinds, "local-bundle"
    )
    packages["p-exec:apm-bundle"] = PackageRef(
        "p-exec", str(apm_bundle), authored["p-exec"].kinds, "local-bundle"
    )
    return World(
        base,
        project,
        isolated.home,
        isolated.config_root,
        env,
        packages,
        services,
        cache_root=isolated.cache_root,
        user_cwd=user_cwd,
    )
