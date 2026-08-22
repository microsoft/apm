"""End-to-end: ``resolve_marketplace_plugin`` against a real local marketplace.

Verifies the full resolution chain for an in-marketplace plugin source:

1. Register a local marketplace (bare repo) via the CLI.
2. Call ``resolve_marketplace_plugin`` against the manifest.
3. Parse the returned canonical through ``DependencyReference``.
4. Assert the dependency is recognised as local and points at the on-disk
   plugin directory (the contract ``LocalDependencySource`` relies on).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.agent_plugins import (
    PLUGIN_SCHEMA_ID,
    UnsupportedAgentPluginVersionError,
)
from apm_cli.bundle.local_bundle import route_agent_plugin_package
from apm_cli.commands.marketplace import marketplace as marketplace_group
from apm_cli.marketplace import registry
from apm_cli.marketplace.resolver import resolve_marketplace_plugin
from apm_cli.models.dependency.reference import DependencyReference

GIT_AVAILABLE = shutil.which("git") is not None
pytestmark = pytest.mark.skipif(not GIT_AVAILABLE, reason="git executable not available")


MANIFEST = {
    "name": "local-mkt",
    "owner": "test",
    "plugins": [
        {
            "name": "skill-a",
            "source": "./skills/skill-a",
            "version": "0.1.0",
        }
    ],
}


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = str(tmp_path / ".apm")
    Path(config_dir).mkdir()
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    monkeypatch.setattr(registry, "_registry_cache", None)


def _seed_marketplace(
    repo: Path,
    *,
    manifest: dict | None = None,
    plugin_manifest: dict | None = None,
) -> None:
    """Create a working-dir git repo + a skill dir + commit everything."""
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e.x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e.x",
    }
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, env=env
    )
    (repo / "marketplace.json").write_text(json.dumps(manifest or MANIFEST))
    if plugin_manifest is None:
        skill_dir = repo / "skills" / "skill-a"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# skill-a\n")
    else:
        plugin_dir = repo / "plugins" / "schema-plugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text(json.dumps(plugin_manifest))
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "seed"],
        check=True,
        capture_output=True,
        env=env,
    )


def test_install_resolves_local_marketplace_to_on_disk_path(tmp_path: Path) -> None:
    repo = tmp_path / "mkt"
    _seed_marketplace(repo)

    runner = CliRunner()
    result = runner.invoke(marketplace_group, ["add", str(repo), "--name", "local-mkt"])
    assert result.exit_code == 0, result.output

    resolved = resolve_marketplace_plugin("skill-a", "local-mkt")

    # Resolver hands install side a local-path canonical
    assert resolved.dependency_reference is None
    canonical = resolved.canonical
    assert DependencyReference.is_local_path(canonical), canonical

    # The canonical points at the actual on-disk skill directory
    skill_path = Path(canonical)
    assert skill_path.exists(), f"resolver canonical does not exist on disk: {skill_path}"
    assert (skill_path / "SKILL.md").is_file()

    # Round-trip the canonical through DependencyReference to confirm it parses
    # as a local dependency (the gate LocalDependencySource branches on).
    dep_ref = DependencyReference.parse(canonical)
    assert dep_ref.is_local
    assert Path(dep_ref.local_path).resolve() == skill_path.resolve()


def test_install_rejects_local_marketplace_symlink_escape(tmp_path: Path) -> None:
    repo = tmp_path / "mkt"
    _seed_marketplace(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# outside\n")
    skill_path = repo / "skills" / "skill-a"
    shutil.rmtree(skill_path)
    try:
        skill_path.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    runner = CliRunner()
    result = runner.invoke(marketplace_group, ["add", str(repo), "--name", "local-mkt"])
    assert result.exit_code == 0, result.output

    with pytest.raises(ValueError, match="outside"):
        resolve_marketplace_plugin("skill-a", "local-mkt")


@pytest.mark.parametrize(
    ("schema_id", "expected"),
    [
        (PLUGIN_SCHEMA_ID, "native"),
        ("https://agent-plugins.org/schemas/2.0.0/plugin.schema.json", "unsupported"),
        (None, "legacy"),
    ],
)
def test_local_marketplace_source_reaches_canonical_schema_router(
    tmp_path: Path,
    schema_id: str | None,
    expected: str,
) -> None:
    repo = tmp_path / "mkt"
    manifest = {
        "name": "local-mkt",
        "owner": "test",
        "plugins": [
            {
                "name": "schema-plugin",
                "source": "./plugins/schema-plugin",
                "version": "1.0.0",
            }
        ],
    }
    plugin_manifest = {"name": "schema.plugin", "version": "1.0.0"}
    if schema_id is not None:
        plugin_manifest["$schema"] = schema_id
    _seed_marketplace(
        repo,
        manifest=manifest,
        plugin_manifest=plugin_manifest,
    )

    runner = CliRunner()
    result = runner.invoke(marketplace_group, ["add", str(repo), "--name", "local-mkt"])
    assert result.exit_code == 0, result.output
    resolved = resolve_marketplace_plugin("schema-plugin", "local-mkt")
    package_root = Path(resolved.canonical)

    if expected == "unsupported":
        with pytest.raises(UnsupportedAgentPluginVersionError, match="supports only"):
            route_agent_plugin_package(package_root)
        return
    detection = route_agent_plugin_package(package_root)
    if expected == "native":
        assert detection is not None
        assert detection.plugin is not None
        assert detection.plugin.identity.name == "schema.plugin"
    else:
        assert detection is None
