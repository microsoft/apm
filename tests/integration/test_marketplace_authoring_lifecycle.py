"""Installed-CLI contracts for explicitly registered live authoring files."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from apm_cli.models.dependency.reference import DependencyReference
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary, pytest.mark.lifecycle_smoke]


def _catalog(path: Path, packages: list[dict], *, plugin_root: str = "") -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "name": "alice-dev",
                "version": "1.0.0",
                "marketplace": {
                    "owner": {"name": "Alice"},
                    "packages": packages,
                    "metadata": {"pluginRoot": plugin_root} if plugin_root else {},
                },
            }
        ),
        encoding="utf-8",
    )


def test_live_local_catalog_register_browse_view_install_and_edit(tmp_path, apm_binary_path):
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    environment = isolated.subprocess_env(overrides={"COLUMNS": "240"})
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    author = isolated.work_root / "author"
    packages = LocalPackageFactory(author / "tools/group")
    tool = packages.create("one", targets=("copilot",))
    packages.add_skill(
        tool, "chosen", "---\nname: chosen\ndescription: Selected skill\n---\n# Chosen\n"
    )
    unused = packages.create("two", targets=("copilot",))
    packages.add_skill(
        unused, "unused", "---\nname: unused\ndescription: Unselected\n---\n# Unused\n"
    )
    packages.add_hook(
        unused,
        "unselected",
        {"hooks": {"preToolUse": [{"bash": "echo must-not-run > hook-executed.txt"}]}},
    )
    path = author / "apm.yml"
    entries = [{"name": name, "source": f"./tools/group/{name}"} for name in ("one", "two")]
    _catalog(path, entries, plugin_root="tools")
    stale_catalog = author / ".claude-plugin/marketplace.json"
    stale_catalog.parent.mkdir()
    stale_catalog.write_text('{"name":"stale-generated-catalog","plugins":[]}', encoding="utf-8")
    source_files = ArtifactSnapshot.capture(author)
    consumer = LocalPackageFactory(isolated.work_root).create("consumer", targets=("copilot",))
    consumer_before = ArtifactSnapshot.capture(consumer.root)

    results = runner.run_sequence(
        (
            ("marketplace", "add", os.path.relpath(path, consumer.root), "--offline"),
            ("marketplace", "browse", "alice-dev", "--offline"),
            ("view", "one@alice-dev"),
        ),
        expected_returncodes=(0, 0, 0),
        scenario_id="live-authoring-discovery",
        cwd=consumer.root,
        env=environment,
    )
    assert "2 plugins" in results[0].stdout
    assert "one" in results[1].stdout and "two" in results[1].stdout
    assert "one" in results[2].stdout
    assert_unchanged(consumer_before, ArtifactSnapshot.capture(consumer.root))
    assert_unchanged(source_files, ArtifactSnapshot.capture(author))
    _catalog(
        path,
        [entries[0], {"name": "new-entry", "source": "./tools/group/two"}],
        plugin_root="tools",
    )
    edited = runner.run(
        ("marketplace", "browse", "alice-dev", "--offline"), cwd=consumer.root, env=environment
    )
    assert edited.returncode == 0, edited.stdout + edited.stderr
    assert "new-entry" in edited.stdout
    installed = runner.run(("install", "one@alice-dev"), cwd=consumer.root, env=environment)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    skills = list(consumer.root.rglob("SKILL.md"))
    assert any("chosen" in file.as_posix() for file in skills)
    assert all("unused" not in file.as_posix() for file in skills)
    lock = yaml.safe_load((consumer.root / "apm.lock.yaml").read_text(encoding="utf-8"))
    assert len(lock["dependencies"]) == 1
    assert lock["dependencies"][0]["source"] == "local"
    assert Path(lock["dependencies"][0]["local_path"]) == tool.root
    manifest = yaml.safe_load(consumer.manifest_path.read_text(encoding="utf-8"))
    assert "one" in str(manifest["dependencies"]["apm"])


def test_remote_entry_records_real_source_and_replays_without_preview(tmp_path, apm_binary_path):
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    packages = LocalPackageFactory(isolated.package_root)
    tool = packages.create("remote-tool", targets=("copilot",))
    packages.add_skill(
        tool, "portable", "---\nname: portable\ndescription: Remote skill\n---\n# Portable\n"
    )
    repos = LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env())
    repository = repos.create("remote-tool", source_tree=tool.root)
    commit = repos.commit(repository, message="seed remote tool")
    repos.tag(repository, "v0.1.0", commit)
    remote_url = "https://git.example.test/alice/remote-tool"
    environment = repos.url_rewrite_subprocess_env(repository, remote_url)
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    author = isolated.work_root / "author"
    author.mkdir()
    path = author / "apm.yml"
    _catalog(path, [{"name": "remote-tool", "source": remote_url, "ref": "v0.1.0"}])
    consumer = LocalPackageFactory(isolated.work_root).create("consumer", targets=("copilot",))
    # Cached-only resolution cannot invent a tag pin. Failure must not create
    # a registration, and the explicit online operation uses only owned Git.
    failed = runner.run(
        ("marketplace", "add", str(path), "--offline"), cwd=consumer.root, env=environment
    )
    assert failed.returncode == 1
    assert "offline" in (failed.stdout + failed.stderr).lower()
    runner.run_sequence(
        (("marketplace", "add", str(path)), ("install", "remote-tool@alice-dev")),
        expected_returncodes=(0, 0),
        scenario_id="authoring-remote-install",
        cwd=consumer.root,
        env=environment,
    )
    manifest_text = consumer.manifest_path.read_text(encoding="utf-8")
    lock_text = (consumer.root / "apm.lock.yaml").read_text(encoding="utf-8")
    entry = yaml.safe_load(manifest_text)["dependencies"]["apm"][0]
    dependency = (
        DependencyReference.parse_from_dict(entry)
        if isinstance(entry, dict)
        else DependencyReference.parse(entry)
    )
    assert dependency.repo_url == "alice/remote-tool"
    assert dependency.host == "git.example.test"
    assert str(author) not in manifest_text
    locked_dependency = yaml.safe_load(lock_text)["dependencies"][0]
    assert locked_dependency["repo_url"] == "alice/remote-tool"
    assert locked_dependency["resolved_commit"] == commit.sha
    assert "local_path" not in locked_dependency
    # A second consumer has no registered discovery source and no author file.
    replay_env = IsolatedApmEnvironment.create(tmp_path / "replay", base_env=dict(os.environ))
    replay = LocalPackageFactory(replay_env.work_root).create("replay", targets=("copilot",))
    replay.manifest_path.write_text(manifest_text, encoding="utf-8")
    (replay.root / "apm.lock.yaml").write_text(lock_text, encoding="utf-8")
    path.unlink()
    author.rmdir()
    # Reuse the owned Git rewrite while pointing home/config at a fresh scope.
    replay_environment = replay_env.subprocess_env()
    for key, value in environment.items():
        if (
            key.startswith("GIT_CONFIG_KEY_")
            or key.startswith("GIT_CONFIG_VALUE_")
            or key == "GIT_CONFIG_COUNT"
        ):
            replay_environment[key] = value
    result = runner.run(("install", "--frozen"), cwd=replay.root, env=replay_environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert any("portable" in file.as_posix() for file in replay.root.rglob("SKILL.md"))


@pytest.mark.parametrize(
    "invalid", [None, "marketplace: [", "name: missing\n", "marketplace: []\n"]
)
def test_failed_reregistration_preserves_registered_source(tmp_path, apm_binary_path, invalid):
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    valid = isolated.work_root / "valid"
    valid.mkdir()
    path = valid / "apm.yml"
    _catalog(path, [])
    registered = runner.run(
        ("marketplace", "add", str(path)), cwd=isolated.work_root, env=environment
    )
    assert registered.returncode == 0, registered.stdout + registered.stderr
    registry = isolated.home / ".apm" / "marketplaces.json"
    before = registry.read_bytes()
    invalid_root = isolated.work_root / "invalid"
    invalid_root.mkdir()
    bad_path = invalid_root / "apm.yml"
    if invalid is not None:
        bad_path.write_text(invalid, encoding="utf-8")
    result = runner.run(
        ("marketplace", "add", str(bad_path), "--name", "alice-dev"),
        cwd=isolated.work_root,
        env=environment,
    )
    assert result.returncode == 1
    assert "apm.yml" in result.stdout + result.stderr
    assert registry.read_bytes() == before
    # A registered source is live on reads too: a later edit or deletion must
    # fail actionably rather than serving the previous successful snapshot.
    if invalid is None:
        path.unlink()
    else:
        path.write_text(invalid, encoding="utf-8")
    read = runner.run(
        ("marketplace", "browse", "alice-dev"), cwd=isolated.work_root, env=environment
    )
    assert read.returncode == 1
    assert "apm.yml" in read.stdout + read.stderr
    assert registry.read_bytes() == before


@pytest.mark.parametrize("source_kind", ["json", "git"])
def test_existing_catalog_sources_still_register_without_authoring_resolution(
    tmp_path, apm_binary_path, source_kind
):
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    catalog_root = isolated.package_root / "existing"
    catalog_root.mkdir()
    catalog = catalog_root / "marketplace.json"
    catalog.write_text(
        json.dumps(
            {
                "name": "existing",
                "owner": {"name": "Alice"},
                "plugins": [
                    {"name": "legacy-tool", "source": {"source": "github", "repo": "alice/tool"}}
                ],
            }
        ),
        encoding="utf-8",
    )
    if source_kind == "git":
        repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
        repository = repositories.create("existing", source_tree=catalog_root)
        repositories.commit(repository, message="seed existing catalog")
        source = str(repository.origin)
    else:
        source = str(catalog)
    before = ArtifactSnapshot.capture(catalog_root)
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    results = runner.run_sequence(
        (("marketplace", "add", source), ("marketplace", "browse", "existing")),
        expected_returncodes=(0, 0),
        scenario_id=f"existing-{source_kind}-catalog",
        cwd=isolated.work_root,
        env=environment,
    )
    assert "legacy-tool" in results[1].stdout
    assert_unchanged(before, ArtifactSnapshot.capture(catalog_root))
