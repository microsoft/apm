"""``apm install DIR`` of an APM source with a root plugin.json audits clean (#3649).

#3604 routes a single-argument ``apm install DIR`` whose directory has both
``apm.yml`` and a root ``plugin.json`` to a local dependency.  Install
classifies it as ``apm_package`` and deploys only its ``.apm/`` primitives,
exactly like the same package fetched over Git.  ``apm audit --ci`` must
replay the same decision; it must not normalize the source as a legacy
plugin and then report root ``agents/`` as unintegrated drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._dependency_graph_world import NO_POLICY, World, write, write_package

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_AGENT = "---\nname: helper\ndescription: Helper\n---\nHelp.\n"


def _plugin_source(root: Path, *, with_manifest: bool) -> Path:
    if with_manifest:
        write_package(root, "plug")
    write(root / "plugin.json", '{"name": "plug", "version": "1.0.0", "description": "p"}\n')
    write(root / "agents" / "helper.md", _AGENT)
    return root


@pytest.mark.parametrize("form", ["single-arg", "manifest"])
def test_apm_source_with_plugin_json_installs_and_audits_clean(
    tmp_path: Path, apm_binary_path: Path, form: str
) -> None:
    world = World.create(tmp_path, apm_binary_path)
    _plugin_source(world.isolated.work_root / "plug", with_manifest=True)
    if form == "single-arg":
        world.manifest([])
        world.apm("install", NO_POLICY, "../plug")
    else:
        world.manifest(["../plug"])
        world.apm("install", NO_POLICY)

    (entry,) = world.locked().values()
    assert entry["package_type"] == "apm_package"
    assert entry["local_path"] == "../plug"
    assert world.deployed() == {"plug.instructions.md"}
    assert not (world.project / ".github" / "apm.yml").exists()
    assert not (world.project / ".github" / "agents").exists()

    lock_before = world.lock_bytes()
    world.apm("install", NO_POLICY, "../plug")
    assert world.lock_bytes() == lock_before
    world.apm("install", NO_POLICY, "--frozen")
    assert world.lock_bytes() == lock_before

    audit = world.apm("audit", "--ci", NO_POLICY, expect=None)
    assert audit.returncode == 0, audit.stdout + audit.stderr
    assert not (world.project / ".github" / "agents").exists()

    world.apm("uninstall", "../plug")
    assert world.deployed() == set()
    assert not world.modules()


def test_legacy_local_plugin_still_replays_its_agents(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A manifestless local plugin keeps deploying (and auditing) root agents."""
    world = World.create(tmp_path, apm_binary_path)
    _plugin_source(world.isolated.work_root / "lp", with_manifest=False)
    world.manifest(["../lp"])
    world.apm("install", NO_POLICY)

    (entry,) = world.locked().values()
    assert entry["package_type"] == "marketplace_plugin"
    assert world.deployed(".github/agents") == {"helper.agent.md"}
    audit = world.apm("audit", "--ci", NO_POLICY, expect=None)
    assert audit.returncode == 0, audit.stdout + audit.stderr
