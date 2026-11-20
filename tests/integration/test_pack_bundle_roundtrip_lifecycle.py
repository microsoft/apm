"""`apm pack` bundles carry coherent versions and install as bundles (#3408, #3604, #3649).

Chain: stale authored plugin.json (root + .claude-plugin) -> pack every bundle
format -> every bundled plugin.json carries the apm.yml version, sources are
byte-identical, the warning names the authored file -> pack again is
deterministic -> install the bundle directory and the archive into consumers.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest

from tests.integration._marketplace_pack_kit import (
    MarketplacePackWorld,
    output,
    remote_url,
    skill_md,
    tree_bytes,
    write,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]


def _producer(world: MarketplacePackWorld) -> Path:
    root = world.isolated.work_root / "bundler"
    write(
        root / "apm.yml",
        "name: bundler\nversion: 1.0.3\ndescription: b\nlicense: MIT\ntargets: [claude]\n"
        "dependencies:\n  apm: []\n",
    )
    write(root / ".apm" / "skills" / "bs" / "SKILL.md", skill_md("bs"))
    write(root / ".apm" / "agents" / "ba.agent.md", "---\ndescription: a\n---\n# ba\n")
    write(root / ".claude-plugin" / "plugin.json", '{"name": "bundler", "version": "1.0.2"}\n')
    write(root / "plugin.json", '{"name": "bundler", "version": "1.0.2", "author": "x"}\n')
    return root


def _stable(tree: dict[str, bytes]) -> dict[str, bytes]:
    """Drop the pack timestamp, which is metadata rather than bundle content."""
    return {
        rel: re.sub(rb"packed_at: '[^']*'", b"", data) if rel.endswith("apm.lock.yaml") else data
        for rel, data in tree.items()
    }


@pytest.mark.parametrize("fmt", [(), ("--format", "claude-plugin"), ("--format", "plugin")])
def test_pack_bundle_versions_sources_and_determinism(
    tmp_path: Path, apm_binary_path: Path, fmt: tuple[str, ...]
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    producer = _producer(world)
    world.apm(producer, "install", "--no-policy")
    sources_before = tree_bytes(producer)

    first = world.apm(producer, "pack", *fmt, "-o", str(tmp_path / "out1"))
    bundle = tmp_path / "out1" / "bundler-1.0.3"
    manifests = sorted(bundle.rglob("plugin.json"))
    assert manifests, sorted(p.name for p in bundle.rglob("*"))
    for manifest in manifests:
        assert json.loads(manifest.read_text())["version"] == "1.0.3", manifest
    assert "plugin.json declares version 1.0.2" in output(first)
    assert tree_bytes(producer) == sources_before
    assert (bundle / "skills" / "bs" / "SKILL.md").is_file()

    world.apm(producer, "pack", *fmt, "-o", str(tmp_path / "out2"))
    assert _stable(tree_bytes(tmp_path / "out2" / "bundler-1.0.3")) == _stable(tree_bytes(bundle))
    assert tree_bytes(producer) == sources_before


@pytest.mark.parametrize("archive", [False, True])
def test_packed_bundle_installs_as_bundle_not_source(
    tmp_path: Path, apm_binary_path: Path, archive: bool
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    producer = _producer(world)
    world.apm(producer, "install", "--no-policy")
    args = ("pack", "-o", str(tmp_path / "out"))
    world.apm(producer, *((*args, "--archive") if archive else args))
    artifact = tmp_path / "out" / ("bundler-1.0.3.zip" if archive else "bundler-1.0.3")
    assert artifact.exists()

    consumer = world.consumer("bundle-consumer", targets=("claude",))
    manifest_before = (consumer / "apm.yml").read_bytes()
    world.apm(consumer, "install", str(artifact), "--no-policy")
    assert (consumer / ".claude" / "skills" / "bs" / "SKILL.md").is_file()
    # #3604: real pack output must still install as a bundle, never be recorded
    # as a local source dependency in apm.yml.
    assert (consumer / "apm.yml").read_bytes() == manifest_before
    lock_text = (consumer / "apm.lock.yaml").read_text()
    assert "bundler-1.0.3" not in re.findall(r"local_path: (.*)", lock_text)
    world.apm(consumer, "audit", "--ci", "--no-policy")

    second = world.apm(consumer, "install", str(artifact), "--no-policy")
    assert (consumer / "apm.yml").read_bytes() == manifest_before
    assert "error" not in output(second).lower()


def test_source_package_with_plugin_json_installs_as_local_dependency(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3604: `apm install DIR` of an APM source package with a root plugin.json."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    producer = _producer(world)
    consumer = world.consumer("source-consumer", targets=("claude",))
    world.apm(consumer, "install", str(producer), "--no-policy")
    manifest = (consumer / "apm.yml").read_text()
    assert "bundler" in manifest
    assert (consumer / ".claude" / "skills" / "bs" / "SKILL.md").is_file()
    lock_before = (consumer / "apm.lock.yaml").read_bytes()
    world.apm(consumer, "install", "--no-policy")
    assert (consumer / "apm.lock.yaml").read_bytes() == lock_before
    world.apm(consumer, "uninstall", str(producer))
    assert not (consumer / ".claude" / "skills" / "bs").exists()
    assert tree_bytes(producer).keys() >= {"apm.yml", "plugin.json"}


def test_apm_format_pack_carries_skills_deployed_under_agents(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """``--format apm`` bundles copilot skills deployed to ``.agents/skills/`` (#4202)."""
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    package = world.isolated.package_root / "skillpkg"
    write(
        package / "apm.yml", "name: skillpkg\nversion: 1.0.0\ndescription: s\ntargets: [copilot]\n"
    )
    write(package / ".apm" / "skills" / "ks" / "SKILL.md", skill_md("ks", "body\n"))
    _, commit = world.publish("skillpkg", package)
    producer = world.consumer(
        "producer", targets=("copilot",), deps=({"git": remote_url("skillpkg"), "ref": commit.sha},)
    )
    world.apm(producer, "install")
    deployed = ".agents/skills/ks/SKILL.md"
    assert (producer / deployed).is_file()
    recorded = {p for dep in world.lock(producer)["dependencies"] for p in dep["deployed_files"]}
    assert ".agents/skills/ks" in recorded, recorded
    sources_before = tree_bytes(producer)

    out = tmp_path / "out"
    packed = world.apm(producer, "pack", "--format", "apm", "--archive", "-o", str(out))
    assert "No deployed files found" not in output(packed), output(packed)
    # Pack only adds the generated Copilot plugin manifest to the project.
    after = tree_bytes(producer)
    assert {k: v for k, v in after.items() if k != ".github/plugin/plugin.json"} == sources_before
    archive = out / "producer-0.1.0.zip"
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert f"producer-0.1.0/{deployed}" in names, names
        assert bundle.read(f"producer-0.1.0/{deployed}") == (producer / deployed).read_bytes()

    target = world.consumer("bundle-target", targets=("copilot",))
    unpacked = world.apm(target, "unpack", str(archive))
    assert "No files were unpacked" not in output(unpacked), output(unpacked)
    assert (target / deployed).read_bytes() == (producer / deployed).read_bytes()
