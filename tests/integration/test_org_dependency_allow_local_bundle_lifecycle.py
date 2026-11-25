"""APMLifecycle: the org dependency allow list judges a local bundle by its packages.

PROMISE: P-INV-I7 -- a refused command leaves durable state as it found it.
Under ``dependencies.allow``, ``apm install <bundle>`` admits a bundle only
when every package its embedded lockfile records is allowed (#4646). The
author-chosen bundle name (``_local/<name>``) never satisfies the allow list:
a bundle named like an allowed package that records a disallowed one is
refused, and so is a bundle that records no package at all, before any file
is written.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.world import World
from tests.lifecycle.world.fixtures import (  # noqa: F401
    _world_dir,
    lifecycle_template,
    lifecycle_world,
)
from tests.lifecycle.world.scopes import use_policy
from tests.lifecycle.world.services import ALLOWED_PACKAGE, DENIED_PACKAGE, ORG
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
]


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _apm(world: World, *args: str) -> CommandResult:
    return driver.run(world, list(args), entry_point=driver.driver_command("binary"))


def _ok(world: World, *args: str) -> CommandResult:
    result = _apm(world, *args)
    assert result.returncode == 0, _evidence(result)
    return result


def _text(result: CommandResult) -> str:
    return " ".join(f"{result.stdout} {result.stderr}".split())


def _state(world: World) -> dict[str, bytes]:
    root = world.project
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _pack(world: World, out: str) -> Path:
    _ok(world, "pack", "--archive", "-o", str(world.root / out))
    (archive,) = sorted((world.root / out).glob("*.zip"))
    return archive


def _refused(world: World, bundle: str, violation: str) -> str:
    before = _state(world)
    result = _apm(world, "install", bundle)
    text = _text(result)
    assert result.returncode == 1, _evidence(result)
    assert f"Policy violation: {violation}" in text, text
    assert "Install blocked by org policy" in text, text
    assert text.isascii(), text
    assert _state(world) == before
    return text


def test_allow_list_judges_a_local_bundle_by_the_packages_it_records(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    allowed_spec = world.packages[f"{ALLOWED_PACKAGE}:git"].spec
    # Every bundle this project packs is named like the allowed package.
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["name"] = ALLOWED_PACKAGE
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    _ok(world, "install", allowed_spec)
    allowed_bundle = _pack(world, "allowed")

    use_policy(world, "allow-list")

    # A bundle that records no package has nothing the allow list can match.
    empty = _refused(
        world,
        world.packages[f"{DENIED_PACKAGE}:bundle"].spec,
        f"_local/{DENIED_PACKAGE} -- the bundle records no packages for "
        "dependencies.allow to match",
    )
    assert "Run 'apm install <package>'" in empty, empty
    # The printed remedy: install the content from an allowed package.
    _ok(world, "install", allowed_spec)

    lock = world.project / "apm.lock.yaml"
    recorded = lock.read_bytes()
    _ok(world, "install", str(allowed_bundle))
    assert lock.read_bytes() != recorded
    assert "local_deployed_files" in lock.read_text(encoding="utf-8")

    # Setup only: record a disallowed package so the next pack embeds it.
    _ok(world, "install", world.packages[f"{DENIED_PACKAGE}:git"].spec, "--no-policy")
    mixed_bundle = _pack(world, "mixed")

    text = _refused(
        world,
        str(mixed_bundle),
        f"github.com/{ORG}/{DENIED_PACKAGE} -- not in allowed sources",
    )
    assert f"Policy violation: _local/{ALLOWED_PACKAGE}" not in text, text
