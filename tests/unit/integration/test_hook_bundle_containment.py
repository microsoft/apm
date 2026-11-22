"""Hook bundle writes against an external Claude configuration root (#4772)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.integration.hook_bundle import copy_deployed_hook_bundle
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.path_security import PathTraversalError

pytestmark = [pytest.mark.component, pytest.mark.promise("P-INV-I5")]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    home = tmp_path / "home"
    package = home / ".apm"
    script = package / "hooks" / "scripts" / "g.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\necho guard\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    claude_root = tmp_path / "claude-profile"
    claude_root.mkdir()
    target = replace(KNOWN_TARGETS["claude"], root_dir=claude_root.as_posix())
    return SimpleNamespace(
        tmp=tmp_path, home=home, package=package, script=script, root=claude_root, target=target
    )


def _copy(world: SimpleNamespace, target_rel: str) -> list[Path]:
    written: list[Path] = []
    copy_deployed_hook_bundle(
        HookIntegrator(),
        package_path=world.package,
        hook_file_dir=world.script.parent.parent,
        project_root=world.home,
        scripts=[(world.script, target_rel)],
        managed_files=set(),
        force=False,
        target_paths=written,
        selected_bundle_files=frozenset({world.script}),
        target=world.target,
    )
    return written


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_a_script_deploys_under_an_external_claude_root(world: SimpleNamespace) -> None:
    written = _copy(world, f"{world.root.as_posix()}/hooks/local/scripts/g.sh")

    deployed = world.root / "hooks" / "local" / "scripts" / "g.sh"
    assert _files(world.root) == {"hooks/local/scripts/g.sh"}
    assert deployed.read_text(encoding="utf-8") == world.script.read_text(encoding="utf-8")
    assert written == [deployed]


def test_a_dotdot_destination_escaping_the_claude_root_is_refused(
    world: SimpleNamespace,
) -> None:
    with pytest.raises(PathTraversalError):
        _copy(world, f"{world.root.as_posix()}/hooks/local/../../../escaped/scripts/g.sh")

    assert not (world.tmp / "escaped").exists()


def test_a_destination_traversing_to_another_absolute_root_is_refused(
    world: SimpleNamespace,
) -> None:
    elsewhere = world.tmp / "elsewhere"

    with pytest.raises(PathTraversalError):
        _copy(world, f"{elsewhere.as_posix()}/hooks/local/scripts/g.sh")

    assert not elsewhere.exists()


def test_a_symlinked_hooks_dir_leaving_the_claude_root_is_refused(
    world: SimpleNamespace,
) -> None:
    outside = world.tmp / "outside"
    outside.mkdir()
    (world.root / "hooks").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathTraversalError):
        _copy(world, f"{world.root.as_posix()}/hooks/local/scripts/g.sh")

    assert _files(outside) == set()


def test_an_out_and_back_destination_creates_nothing_outside_the_claude_root(
    world: SimpleNamespace,
) -> None:
    """A '..' detour that returns inside the root never creates its detour directory."""
    detour = f"hooks/local/../../../escaped/../{world.root.name}/hooks/local/scripts/g.sh"

    written = _copy(world, f"{world.root.as_posix()}/{detour}")

    deployed = world.root / "hooks" / "local" / "scripts" / "g.sh"
    assert deployed.read_text(encoding="utf-8") == world.script.read_text(encoding="utf-8")
    assert not (world.tmp / "escaped").exists()
    assert len(written) == 1


def test_an_out_and_back_sidecar_is_reported_at_a_readable_path(world: SimpleNamespace) -> None:
    """The package.json sidecar of a JS hook is receipted where it was written."""
    script = world.script.with_suffix(".js")
    script.write_text("console.log('guard')\n", encoding="utf-8")
    world.script = script
    detour = f"hooks/local/../../../escaped/../{world.root.name}/hooks/local/scripts/g.js"

    written = _copy(world, f"{world.root.as_posix()}/{detour}")

    sidecar = world.root / "hooks" / "local" / "package.json"
    assert sidecar.read_text(encoding="utf-8").strip().startswith("{")
    assert len(written) == 2
    assert all(path.is_file() for path in written), written
    assert sidecar in written
    assert not (world.tmp / "escaped").exists()
