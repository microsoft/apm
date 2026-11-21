"""APMLifecycle: the org dependency deny list covers local bundles and pack.

PROMISE: P-INV-I7 -- a refused command leaves durable state as it found it.
A local bundle whose package the org policy denies is refused by
``apm install <bundle>`` like a denied git dependency, before any file is
written, and so is a bundle whose embedded lockfile records a denied package.
``apm pack`` (``--dry-run`` and ``--archive`` too) refuses a project whose
lockfile holds a denied package, so its content never ships. With no
deny in force, an approved dependency's attested hooks file packs and pack
does not claim otherwise.
"""

from __future__ import annotations

import zipfile

import pytest

from tests.lifecycle import driver
from tests.lifecycle.world import World
from tests.lifecycle.world.fixtures import (  # noqa: F401
    _world_dir,
    lifecycle_template,
    lifecycle_world,
)
from tests.lifecycle.world.scopes import use_policy
from tests.lifecycle.world.services import DENIED_PACKAGE, ORG
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
]

_HOOK_FILE = f"hooks/{DENIED_PACKAGE}-session.json"
_UNATTESTED = "is not attested in apm.lock.yaml"


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


def test_denied_local_bundle_is_refused_before_any_write(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    use_policy(world, "deny-pkg")
    spec = world.packages[f"{DENIED_PACKAGE}:bundle"].spec
    before = _state(world)

    result = _apm(world, "install", spec)

    text = _text(result)
    assert result.returncode == 1, _evidence(result)
    assert f"_local/{DENIED_PACKAGE} -- denied by pattern: **/{DENIED_PACKAGE}" in text, text
    assert "Install blocked by org policy" in text, text
    assert "apm policy status" in text, text
    assert _state(world) == before

    preview = _ok(world, "install", spec, "--dry-run")
    assert f"Would be blocked by policy: _local/{DENIED_PACKAGE}" in _text(preview)
    assert _state(world) == before


def test_pack_refuses_denied_dependency_and_packs_attested_hooks(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    _ok(world, "install", world.packages[f"{DENIED_PACKAGE}:git"].spec)
    _ok(world, "approve", f"{ORG}/{DENIED_PACKAGE}")
    _ok(world, "install")
    assert (world.project / ".github" / _HOOK_FILE).is_file()

    # Approved and attested: the hooks file packs, and pack does not say otherwise.
    allowed = _ok(world, "pack", "--archive", "-o", str(world.root / "allowed"))
    assert _UNATTESTED not in _text(allowed), _evidence(allowed)
    (archive,) = sorted((world.root / "allowed").glob("*.zip"))
    with zipfile.ZipFile(archive) as bundle:
        assert any(name.endswith(_HOOK_FILE) for name in bundle.namelist()), bundle.namelist()

    use_policy(world, "deny-pkg")
    before = _state(world)
    denied = f"github.com/{ORG}/{DENIED_PACKAGE} -- denied by pattern: **/{DENIED_PACKAGE}"
    out = world.root / "denied"
    for argv in (("pack", "--dry-run"), ("pack", "--archive", "-o", str(out))):
        result = _apm(world, *argv)
        text = _text(result)
        assert result.returncode == 1, _evidence(result)
        assert denied in text, text
        assert "Pack blocked by org policy" in text, text
        assert _HOOK_FILE not in text, text
    assert not out.exists() or not any(out.iterdir())
    assert _state(world) == before

    # The bundle packed before the deny records the denied package, so a
    # local install of it is refused although its own name is allowed.
    install = _apm(world, "install", str(archive))
    text = _text(install)
    assert install.returncode == 1, _evidence(install)
    assert denied in text, text
    assert _state(world) == before
