"""Pack-to-marketplace installation contracts for versioned bundle directories."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.integration, pytest.mark.lifecycle_smoke]

_REMOTE = "https://github.com/apm-fixtures/versioned-marketplace"
_SKILL = "---\nname: versioned-skill\ndescription: Versioned package fixture\n---\n# Skill\n"


def _runner(apm_engine_command: tuple[str, ...]) -> ApmLifecycleRunner:
    # Substitute only catalog transport: the GitHub Contents API reads the same
    # committed manifest through local Git. Registration, source classification,
    # package download, validation, deployment, and lock replay remain real.
    return ApmLifecycleRunner(
        (
            apm_engine_command[0],
            "-c",
            "from apm_cli.marketplace import client\n"
            "client._FETCHERS['github'] = client._fetch_git\n"
            "from apm_cli.cli import cli\n"
            "cli()\n",
        )
    )


@pytest.mark.parametrize(
    ("version", "local"), [("1.2.3", False), ("2026.9.3", False), ("1.2.3", True)]
)
def test_pack_bundle_installs_from_versioned_marketplace_directory(
    tmp_path: Path,
    apm_engine_command: tuple[str, ...],
    version: str,
    local: bool,
) -> None:
    """A pack-generated dotted directory survives Git resolution and lock replay."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    runner = _runner(apm_engine_command)
    packages = LocalPackageFactory(isolated.package_root)
    producer = packages.create("my-plugin", version=version, targets=("claude",))
    packages.add_skill(producer, "versioned-skill", _SKILL)
    manifest = load_yaml(producer.manifest_path)
    manifest.update({"dependencies": {"apm": []}, "includes": ["skills"]})
    dump_yaml(manifest, producer.manifest_path)
    marketplace = isolated.work_root / "marketplace"
    runner.run_sequence(
        (
            ("install", "--no-policy"),
            ("pack", "--output", str(marketplace / "plugins")),
        ),
        expected_returncodes=(0, 0),
        scenario_id="versioned-marketplace-producer",
        cwd=producer.root,
        env=environment,
    )
    virtual_path = f"plugins/my-plugin-{version}"
    bundle = marketplace / virtual_path
    assert (bundle / "plugin.json").is_file()
    (marketplace / ".claude-plugin").mkdir()
    (marketplace / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "versioned-marketplace",
                "owner": {"name": "APM Tests"},
                "plugins": [{"name": "my-plugin", "source": f"./{virtual_path}"}],
            }
        ),
        encoding="utf-8",
    )
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("versioned-marketplace", source_tree=marketplace)
    commit = repositories.commit(repository, message="seed packed versioned marketplace")
    environment = repositories.url_rewrite_subprocess_env(repository, _REMOTE)
    consumer = LocalPackageFactory(isolated.work_root).create("consumer", targets=("claude",))
    runner.run_sequence(
        (
            (
                "marketplace",
                "add",
                str(marketplace) if local else _REMOTE,
                "--name",
                "versioned-marketplace",
                "--ref",
                commit.sha,
            ),
        ),
        expected_returncodes=(0,),
        scenario_id="versioned-marketplace-consumer",
        cwd=consumer.root,
        env=environment,
    )
    install_args = (
        "install",
        "my-plugin@versioned-marketplace",
        "--target",
        "claude",
        "--no-policy",
    )
    before_manifest = consumer.manifest_path.read_bytes()
    preview = runner.run(
        (*install_args, "--dry-run"),
        scenario_id="versioned-marketplace-preview",
        cwd=consumer.root,
        env=environment,
    )
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert consumer.manifest_path.read_bytes() == before_manifest
    assert not (consumer.root / "apm.lock.yaml").exists()
    assert not (consumer.root / ".claude" / "skills").exists()
    installed = runner.run(
        install_args,
        scenario_id="versioned-marketplace-install",
        cwd=consumer.root,
        env=environment,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert (consumer.root / ".claude" / "skills" / "versioned-skill" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == _SKILL
    locked = load_yaml(consumer.root / "apm.lock.yaml")["dependencies"]
    assert len(locked) == 1
    if not local:
        assert locked[0]["virtual_path"] == virtual_path
        assert locked[0]["resolved_commit"] == commit.sha
    replay = runner.run(
        ("install", "--no-policy"),
        scenario_id="versioned-marketplace-replay",
        cwd=consumer.root,
        env=environment,
    )
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert load_yaml(consumer.root / "apm.lock.yaml")["dependencies"] == locked


@pytest.mark.parametrize(
    "name", ["unknown", "versioned-file", "invalid", "invalid-unversioned", "traversal"]
)
def test_git_marketplace_rejects_unknown_files_and_invalid_versioned_packages(
    tmp_path: Path,
    apm_engine_command: tuple[str, ...],
    name: str,
) -> None:
    """Explicit paths cannot turn arbitrary files or invalid content into packages."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    runner = _runner(apm_engine_command)
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("invalid-marketplace")
    plugins = repository.worktree / "plugins"
    plugins.mkdir()
    (plugins / "unknown.txt").write_text("Not an APM package", encoding="utf-8")
    (plugins / "file-1.2.3").write_text("Not a directory", encoding="utf-8")
    invalid = plugins / "invalid-1.2.3"
    invalid.mkdir()
    (invalid / "plugin.json").write_text("{not valid JSON", encoding="utf-8")
    unversioned = plugins / "invalid-package"
    unversioned.mkdir()
    (unversioned / "plugin.json").write_text("{not valid JSON", encoding="utf-8")
    entries = {
        "unknown": "./plugins/unknown.txt",
        "versioned-file": "./plugins/file-1.2.3",
        "invalid": "./plugins/invalid-1.2.3",
        "invalid-unversioned": "./plugins/invalid-package",
        "traversal": "./plugins/../invalid-1.2.3",
    }
    (repository.worktree / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "invalid-marketplace",
                "owner": {"name": "APM Tests"},
                "plugins": [{"name": name, "source": path} for name, path in entries.items()],
            }
        ),
        encoding="utf-8",
    )
    commit = repositories.commit(repository, message="seed invalid marketplace packages")
    environment = repositories.url_rewrite_subprocess_env(repository, _REMOTE)
    consumers = LocalPackageFactory(isolated.work_root)
    consumer = consumers.create(f"consumer-{name}", targets=("claude",))
    before_manifest = consumer.manifest_path.read_bytes()
    runner.run_sequence(
        (
            (
                "marketplace",
                "add",
                _REMOTE,
                "--name",
                "invalid-marketplace",
                "--ref",
                commit.sha,
            ),
        ),
        expected_returncodes=(0,),
        scenario_id=f"invalid-marketplace-{name}-register",
        cwd=consumer.root,
        env=environment,
    )
    result = runner.run(
        ("install", f"{name}@invalid-marketplace", "--no-policy"),
        scenario_id=f"invalid-marketplace-{name}-install",
        cwd=consumer.root,
        env=environment,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert consumer.manifest_path.read_bytes() == before_manifest
    assert not (consumer.root / "apm.lock.yaml").exists()
    assert not (consumer.root / ".claude" / "skills").exists()
