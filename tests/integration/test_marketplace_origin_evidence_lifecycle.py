"""Real-CLI catalog origin evidence for Git marketplaces (#3912).

A local alias such as ``company`` is machine-local. The lock records the
catalog's canonical origin (``source_url``) and digest (``source_digest``) for
every catalog kind, so a rebound alias cannot silently switch the marketplace
an ``apm.yml`` object-form entry resolves through.
"""

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

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_HOST = "gitlab.example.invalid"
_REMOTE_A = f"https://{_HOST}/team/catalog-a"
_REMOTE_B = f"https://{_HOST}/team/catalog-b"
_ALIAS = "company"
_PLUGIN = "example-plugin"
_SUBDIR = f"plugins/{_PLUGIN}"
_UPDATE_FLAGS = ("--target", "codex", "--parallel-downloads", "0")
_INSTALL_FLAGS = (*_UPDATE_FLAGS, "--no-policy")


def _skill_document(label: str) -> str:
    return f"---\nname: {_PLUGIN}\ndescription: Origin evidence fixture\n---\n# {label}\n"


def _publish_catalog(repositories: LocalGitRepositoryFactory, name: str, remote: str, label: str):
    repository = repositories.create(name)
    packages = LocalPackageFactory(repository.worktree / "plugins")
    package = packages.create(_PLUGIN, targets=("codex",))
    skill_source = packages.add_skill(package, _PLUGIN, _skill_document(label))
    catalog_dir = repository.worktree / ".claude-plugin"
    catalog_dir.mkdir()
    (catalog_dir / "marketplace.json").write_text(
        json.dumps(
            {
                "name": name,
                "owner": {"name": "APM Test"},
                "plugins": [
                    {
                        "name": _PLUGIN,
                        "source": {
                            "source": "git-subdir",
                            "url": remote,
                            "path": _SUBDIR,
                            "ref": "main",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    repositories.commit(repository, message=f"publish {name}")
    return repository, skill_source


def _locked_row(lock_path: Path) -> dict[str, object]:
    dependencies = load_yaml(lock_path)["dependencies"]
    assert len(dependencies) == 1
    return dependencies[0]


def _output(result) -> str:
    return " ".join(f"{result.stdout}\n{result.stderr}".split())


def test_git_catalog_origin_is_locked_and_a_rebound_alias_fails_update(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Lock origin evidence, refuse a rebound alias, and warn on a duplicate origin."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path.resolve() / "isolated", base_env=dict(os.environ)
    )
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    catalog_a, _ = _publish_catalog(repositories, "catalog-a", _REMOTE_A, "from catalog A")
    catalog_b, _ = _publish_catalog(repositories, "catalog-b", _REMOTE_B, "from catalog B")
    environment = repositories.url_rewrite_subprocess_env_many(
        ((catalog_a, _REMOTE_A), (catalog_b, _REMOTE_B))
    )
    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer",
        targets=("codex",),
        dependencies=({"name": _PLUGIN, "marketplace": _ALIAS},),
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    lock_path = consumer.root / "apm.lock.yaml"
    deployed_skill = consumer.root / ".agents" / "skills" / _PLUGIN / "SKILL.md"

    runner.run_sequence(
        (
            ("marketplace", "add", _REMOTE_A, "--name", _ALIAS, "--ref", "main"),
            ("install", *_INSTALL_FLAGS),
        ),
        expected_returncodes=(0, 0),
        scenario_id="git-catalog-origin-install",
        cwd=consumer.root,
        env=environment,
    )
    locked = _locked_row(lock_path)
    assert locked["discovered_via"] == _ALIAS
    assert locked["marketplace_plugin_name"] == _PLUGIN
    assert locked["source_url"] == _REMOTE_A
    digest = str(locked["source_digest"])
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    assert deployed_skill.read_text(encoding="utf-8") == _skill_document("from catalog A")

    # A second alias for the same catalog origin is allowed but flagged.
    (duplicate,) = runner.run_sequence(
        (("marketplace", "add", f"{_REMOTE_A}.git", "--name", "company-mirror", "--ref", "main"),),
        expected_returncodes=(0,),
        scenario_id="git-catalog-duplicate-origin",
        cwd=consumer.root,
        env=environment,
    )
    duplicate_output = _output(duplicate)
    assert "[!]" in duplicate_output
    assert f"'{_ALIAS}'" in duplicate_output
    assert "same catalog" in duplicate_output

    # Rebinding the alias on this machine must not switch the project's catalog:
    # a plain install replays the locked catalog, and an update (which
    # re-resolves through the alias) fails closed naming both origins.
    before_lock = lock_path.read_bytes()
    (_rebind, _install, update) = runner.run_sequence(
        (
            ("marketplace", "add", _REMOTE_B, "--name", _ALIAS, "--ref", "main"),
            ("install", *_INSTALL_FLAGS),
            ("update", "--yes", *_UPDATE_FLAGS),
        ),
        expected_returncodes=(0, 0, 1),
        scenario_id="git-catalog-rebound-alias-update",
        cwd=consumer.root,
        env=environment,
    )
    update_output = _output(update)
    assert _REMOTE_A in update_output
    assert _REMOTE_B in update_output
    assert f"'{_ALIAS}'" in update_output
    assert lock_path.read_bytes() == before_lock
    assert deployed_skill.read_text(encoding="utf-8") == _skill_document("from catalog A")

    # Restoring the alias to the locked origin lets update proceed again.
    runner.run_sequence(
        (
            ("marketplace", "add", _REMOTE_A, "--name", _ALIAS, "--ref", "main"),
            ("update", "--yes", *_UPDATE_FLAGS),
        ),
        expected_returncodes=(0, 0),
        scenario_id="git-catalog-restored-alias-update",
        cwd=consumer.root,
        env=environment,
    )
    assert _locked_row(lock_path)["source_url"] == _REMOTE_A
    assert deployed_skill.read_text(encoding="utf-8") == _skill_document("from catalog A")

    # Switching catalogs on purpose follows the error's advice: drop the entry,
    # install, add it back, install again.
    manifest = load_yaml(consumer.manifest_path)
    declared = manifest["dependencies"]["apm"]
    manifest["dependencies"]["apm"] = []
    dump_yaml(manifest, consumer.manifest_path)
    runner.run_sequence(
        (
            ("marketplace", "add", _REMOTE_B, "--name", _ALIAS, "--ref", "main"),
            ("install", *_INSTALL_FLAGS),
        ),
        expected_returncodes=(0, 0),
        scenario_id="git-catalog-switch-drop",
        cwd=consumer.root,
        env=environment,
    )
    manifest["dependencies"]["apm"] = declared
    dump_yaml(manifest, consumer.manifest_path)
    runner.run_sequence(
        (("install", *_INSTALL_FLAGS),),
        expected_returncodes=(0,),
        scenario_id="git-catalog-switch-readd",
        cwd=consumer.root,
        env=environment,
    )
    assert _locked_row(lock_path)["source_url"] == _REMOTE_B
    assert deployed_skill.read_text(encoding="utf-8") == _skill_document("from catalog B")


def test_pre_037_lock_without_origin_evidence_still_updates(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A lock row without origin evidence never fails closed and gains it on its next write."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path.resolve() / "isolated", base_env=dict(os.environ)
    )
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    catalog_a, skill_source = _publish_catalog(
        repositories, "catalog-a", _REMOTE_A, "from catalog A"
    )
    environment = repositories.url_rewrite_subprocess_env(catalog_a, _REMOTE_A)
    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer",
        targets=("codex",),
        dependencies=({"name": _PLUGIN, "marketplace": _ALIAS},),
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    lock_path = consumer.root / "apm.lock.yaml"

    runner.run_sequence(
        (
            ("marketplace", "add", _REMOTE_A, "--name", _ALIAS, "--ref", "main"),
            ("install", *_INSTALL_FLAGS),
        ),
        expected_returncodes=(0, 0),
        scenario_id="pre-037-lock-install",
        cwd=consumer.root,
        env=environment,
    )
    lock = load_yaml(lock_path)
    for row in lock["dependencies"]:
        row.pop("source_url", None)
        row.pop("source_digest", None)
    dump_yaml(lock, lock_path)
    legacy_lock = lock_path.read_bytes()

    # Nothing to update: the legacy row is accepted and the lock stays untouched.
    runner.run_sequence(
        (("update", "--yes", *_UPDATE_FLAGS),),
        expected_returncodes=(0,),
        scenario_id="pre-037-lock-noop-update",
        cwd=consumer.root,
        env=environment,
    )
    assert lock_path.read_bytes() == legacy_lock

    # The next real lock write records the origin evidence.
    skill_source.write_text(_skill_document("catalog A, next release"), encoding="utf-8")
    repositories.commit(catalog_a, message="advance plugin")
    runner.run_sequence(
        (("update", "--yes", *_UPDATE_FLAGS),),
        expected_returncodes=(0,),
        scenario_id="pre-037-lock-update",
        cwd=consumer.root,
        env=environment,
    )
    locked = _locked_row(lock_path)
    assert locked["source_url"] == _REMOTE_A
    assert str(locked["source_digest"]).startswith("sha256:")
