"""User-scope local-bundle rows keep ``scope: user`` in ``$APM_HOME/apm.lock.yaml`` (#3995).

``apm install -g <bundle>`` records home-relative bundle rows. An unrelated
``-g`` install rebuilds the ledger without reinstalling the bundle; the rows
must keep their path, ``scope: user``, owners and hash, and ``-g --frozen``
and ``audit --ci`` must still pass. A project bundle keeps ``scope: project``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._marketplace_pack_kit import MarketplacePackWorld, skill_md, write

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
]

_ROW_PATH = ".claude/skills/bundled/SKILL.md"


def _package(root: Path, name: str, skill: str) -> Path:
    write(root / "apm.yml", f"name: {name}\nversion: 1.0.0\ndescription: d\nlicense: MIT\n")
    write(root / ".apm" / "skills" / skill / "SKILL.md", skill_md(skill))
    return root


def _bundle(world: MarketplacePackWorld, tmp_path: Path) -> Path:
    producer = _package(world.isolated.work_root / "bundled", "bundled", "bundled")
    manifest = (producer / "apm.yml").read_text()
    write(producer / "apm.yml", manifest + "targets: [claude]\ndependencies:\n  apm: []\n")
    world.apm(producer, "install", "--no-policy")
    world.apm(producer, "pack", "-o", str(tmp_path / "out"))
    return tmp_path / "out" / "bundled-1.0.0"


def _bundle_rows(lock: dict) -> list[dict]:
    return [row for row in lock.get("deployments", []) if row["value"] == _ROW_PATH]


def test_user_bundle_rows_keep_user_scope_across_an_unrelated_rebuild(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    bundle = _bundle(world, tmp_path)
    home, apm_home = world.isolated.home, world.isolated.config_root
    write(apm_home / "apm.yml", "name: user\nversion: 1.0.0\ntargets: [claude]\n")
    (home / ".claude").mkdir(exist_ok=True)
    cwd = world.isolated.work_root

    world.apm(cwd, "install", "-g", str(bundle), "--target", "claude", "--no-policy")
    assert (home / _ROW_PATH).is_file()
    (installed,) = _bundle_rows(world.lock(apm_home))
    assert installed["scope"] == "user"
    assert installed["active_owner"] == "local-bundle"
    assert installed["content_hash"].startswith("sha256:")

    # An unrelated -g dependency install rebuilds the ledger; the bundle is not reinstalled.
    other = _package(world.isolated.work_root / "other", "other", "other")
    world.apm(cwd, "install", "-g", str(other), "--target", "claude", "--no-policy")
    assert (home / ".claude/skills/other/SKILL.md").is_file()
    assert _bundle_rows(world.lock(apm_home)) == [installed]

    world.apm(apm_home, "install", "-g", "--frozen", "--target", "claude", "--no-policy")
    world.apm(apm_home, "audit", "--ci", "--no-policy")
    assert _bundle_rows(world.lock(apm_home)) == [installed]


def test_project_bundle_rows_keep_project_scope(tmp_path: Path, apm_binary_path: Path) -> None:
    world = MarketplacePackWorld.create(tmp_path, apm_binary_path)
    bundle = _bundle(world, tmp_path)
    consumer = world.consumer("bundle-consumer", targets=("claude",))

    world.apm(consumer, "install", str(bundle), "--no-policy")
    other = _package(world.isolated.work_root / "other", "other", "other")
    world.apm(consumer, "install", str(other), "--no-policy")

    (row,) = _bundle_rows(world.lock(consumer))
    assert (row["scope"], row["active_owner"]) == ("project", "local-bundle")
    world.apm(consumer, "audit", "--ci", "--no-policy")
