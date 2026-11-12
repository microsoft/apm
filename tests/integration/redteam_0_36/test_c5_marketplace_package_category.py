"""C5: `apm marketplace package add/set --category` (#2637) round trip.

Chain: codex-enabled producer -> package add without category is refused with
apm.yml byte-identical -> add/set --category -> pack writes it to Codex and
Claude outputs -> invalid categories rejected with apm.yml byte-identical.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.redteam_0_36._c5_support import C5World, output, skill_md, write

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]


def test_marketplace_package_category_round_trip(tmp_path: Path, apm_binary_path: Path) -> None:
    world = C5World.create(tmp_path, apm_binary_path)
    pkg = world.isolated.package_root / "catpkg"
    write(pkg / "apm.yml", "name: catpkg\nversion: 1.0.0\ndescription: c\n")
    write(pkg / "skills" / "c1" / "SKILL.md", skill_md("c1"))
    _, commit = world.publish("catpkg", pkg)

    producer = world.isolated.work_root / "cat-market"
    producer.mkdir()
    dump_yaml(
        {
            "name": "cat-market",
            "version": "1.0.0",
            "description": "m",
            "marketplace": {
                "owner": {"name": "acme", "url": "https://example.invalid/acme"},
                "outputs": {"claude": {}, "codex": {}},
                "packages": [],
            },
        },
        producer / "apm.yml",
    )
    source = "git.example.invalid/acme/catpkg"
    empty_manifest = (producer / "apm.yml").read_bytes()
    missing = world.apm(
        producer, "marketplace", "package", "add", source, "--ref", commit.sha, expect=2
    )
    assert "category" in output(missing).lower()
    assert (producer / "apm.yml").read_bytes() == empty_manifest
    world.apm(
        producer,
        "marketplace",
        "package",
        "add",
        source,
        "--ref",
        commit.sha,
        "--category",
        "Productivity",
    )
    assert load_yaml(producer / "apm.yml")["marketplace"]["packages"][0]["category"] == (
        "Productivity"
    )

    world.apm(producer, "marketplace", "package", "set", "catpkg", "--category", "Developer Tools")
    entry = load_yaml(producer / "apm.yml")["marketplace"]["packages"][0]
    assert entry["category"] == "Developer Tools"
    assert entry["ref"] == commit.sha
    world.apm(producer, "pack")
    for rel in (".agents/plugins/marketplace.json", ".claude-plugin/marketplace.json"):
        plugins = json.loads((producer / rel).read_text())["plugins"]
        assert [p.get("category") for p in plugins] == ["Developer Tools"], rel

    manifest_before = (producer / "apm.yml").read_bytes()
    for bad in ("", "   "):
        world.apm(producer, "marketplace", "package", "set", "catpkg", "--category", bad, expect=2)
        assert (producer / "apm.yml").read_bytes() == manifest_before
    world.apm(producer, "marketplace", "package", "set", "nope", "--category", "X", expect=2)
    assert (producer / "apm.yml").read_bytes() == manifest_before

    world.apm(
        producer,
        "marketplace",
        "package",
        "add",
        source,
        "--name",
        "catpkg-two",
        "--ref",
        commit.sha,
        "--category",
        "Productivity",
    )
    packages = load_yaml(producer / "apm.yml")["marketplace"]["packages"]
    assert [p.get("category") for p in packages] == ["Developer Tools", "Productivity"]
    world.apm(producer, "pack")
    world.apm(producer, "marketplace", "check")
