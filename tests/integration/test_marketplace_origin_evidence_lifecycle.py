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
from tests.utils.lockfile_write_counter import INOTIFY_AVAILABLE, count_lockfile_writes

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
_REMOTE_PLUGIN = f"https://{_HOST}/team/plugin-source"
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
    # A lock as APM 0.36 wrote it: an older generator stamp, a pre-0.37 lockfile
    # version, unframed hashes, and no origin evidence (#4228).
    lock = load_yaml(lock_path)
    lock["apm_version"] = "0.36.0"
    lock["lockfile_version"] = "1"
    for row in lock["dependencies"]:
        row.pop("source_url", None)
        row.pop("source_digest", None)
        row.pop("content_hash", None)
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
    # One-way: the rewritten lock carries the current generator stamp.
    assert not str(load_yaml(lock_path)["apm_version"]).startswith("0.36"), load_yaml(lock_path)


def test_failed_update_keeps_the_lock_after_reverifying_provenance(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Restored catalog provenance reaches apm.lock.yaml only with a completed update."""
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
        scenario_id="reverify-then-fail-install",
        cwd=consumer.root,
        env=environment,
    )
    locked_bytes = lock_path.read_bytes()

    # The catalog changes (so update restores its digest) and the plugin's
    # source goes away (so the update fails after that re-verification).
    catalog_path = catalog_a.worktree / ".claude-plugin" / "marketplace.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["metadata"] = {"description": "republished"}
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    skill_source.unlink()
    for path in sorted((catalog_a.worktree / _SUBDIR).rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    (catalog_a.worktree / _SUBDIR).rmdir()
    repositories.commit(catalog_a, message="republish catalog without the plugin")

    (failed,) = runner.run_sequence(
        (("update", "--yes", *_UPDATE_FLAGS),),
        expected_returncodes=(1,),
        scenario_id="reverify-then-fail-update",
        cwd=consumer.root,
        env=environment,
    )
    # The re-verification did restore a row before the update failed.
    assert "Restored marketplace provenance for" in _output(failed), _output(failed)
    assert f"({_PLUGIN}@{_ALIAS})" in _output(failed), _output(failed)
    assert lock_path.read_bytes() == locked_bytes, _output(failed)


def test_noop_update_records_restored_provenance_in_one_lock_write(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A no-op update whose re-verification restores a catalog digest persists it once (#4430).

    The plugin lives in its own repository, so republishing the catalog moves
    only the catalog digest: no ref changes, yet the restored provenance must
    reach apm.lock.yaml through the update's single lockfile write.
    """
    isolated = IsolatedApmEnvironment.create(
        tmp_path.resolve() / "isolated", base_env=dict(os.environ)
    )
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    plugin_repo = repositories.create("plugin-source")
    packages = LocalPackageFactory(plugin_repo.worktree / "plugins")
    package = packages.create(_PLUGIN, targets=("codex",))
    packages.add_skill(package, _PLUGIN, _skill_document("separate source"))
    repositories.commit(plugin_repo, message="publish plugin")
    catalog_a = repositories.create("catalog-a")
    catalog_path = catalog_a.worktree / ".claude-plugin" / "marketplace.json"
    catalog_path.parent.mkdir()
    catalog = {
        "name": "catalog-a",
        "owner": {"name": "APM Test"},
        "plugins": [
            {
                "name": _PLUGIN,
                "source": {
                    "source": "git-subdir",
                    "url": _REMOTE_PLUGIN,
                    "path": _SUBDIR,
                    "ref": "main",
                },
            }
        ],
    }
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    repositories.commit(catalog_a, message="publish catalog")
    environment = repositories.url_rewrite_subprocess_env_many(
        ((catalog_a, _REMOTE_A), (plugin_repo, _REMOTE_PLUGIN))
    )
    factory = LocalPackageFactory(isolated.work_root)
    dependencies = ({"name": _PLUGIN, "marketplace": _ALIAS},)
    consumer = factory.create("consumer", targets=("codex",), dependencies=dependencies)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    lock_path = consumer.root / "apm.lock.yaml"
    runner.run_sequence(
        (
            ("marketplace", "add", _REMOTE_A, "--name", _ALIAS, "--ref", "main"),
            ("install", *_INSTALL_FLAGS),
        ),
        expected_returncodes=(0, 0),
        scenario_id="restored-provenance-install",
        cwd=consumer.root,
        env=environment,
    )
    stale = _locked_row(lock_path)

    catalog["metadata"] = {"description": "republished"}
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    repositories.commit(catalog_a, message="republish catalog metadata")
    # Oracle: what a fresh install records against the republished catalog.
    fresh = factory.create("fresh", targets=("codex",), dependencies=dependencies)
    runner.run_sequence(
        (("install", *_INSTALL_FLAGS),),
        expected_returncodes=(0,),
        scenario_id="restored-provenance-oracle",
        cwd=fresh.root,
        env=environment,
    )
    expected = _locked_row(fresh.root / "apm.lock.yaml")
    assert expected["source_digest"] != stale["source_digest"]
    assert expected["resolved_commit"] == stale["resolved_commit"]

    update = ("update", "--yes", *_UPDATE_FLAGS)
    if INOTIFY_AVAILABLE:
        with count_lockfile_writes(consumer.root) as counter:
            (updated,) = runner.run_sequence(
                (update,),
                expected_returncodes=(0,),
                scenario_id="restored-provenance-update",
                cwd=consumer.root,
                env=environment,
            )
        assert counter.writes == 1, (counter.events, _output(updated))
    else:
        (updated,) = runner.run_sequence(
            (update,),
            expected_returncodes=(0,),
            scenario_id="restored-provenance-update",
            cwd=consumer.root,
            env=environment,
        )
    assert "Restored marketplace provenance for" in _output(updated), _output(updated)
    row = _locked_row(lock_path)
    for field in ("source_url", "source_digest", "resolved_commit"):
        assert row[field] == expected[field], (field, _output(updated))
    restored_bytes = lock_path.read_bytes()

    runner.run_sequence(
        (
            ("audit", "--ci", "--no-policy"),
            ("install", *_INSTALL_FLAGS, "--frozen"),
            update,
        ),
        expected_returncodes=(0, 0, 0),
        scenario_id="restored-provenance-converged",
        cwd=consumer.root,
        env=environment,
    )
    assert lock_path.read_bytes() == restored_bytes


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_update_refuses_a_locked_plugin_removed_from_its_catalog(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """#4967: a plugin the publisher removed fails update without a write or a fetch.

    The plugin lives in its own repository, so a fetch for it would show its
    remote in the git trace; the refusal must come from re-verifying the
    catalog before anything is planned.
    """
    isolated = IsolatedApmEnvironment.create(
        tmp_path.resolve() / "isolated", base_env=dict(os.environ)
    )
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    plugin_repo = repositories.create("plugin-source")
    packages = LocalPackageFactory(plugin_repo.worktree / "plugins")
    package = packages.create(_PLUGIN, targets=("codex",))
    packages.add_skill(package, _PLUGIN, _skill_document("separate source"))
    repositories.commit(plugin_repo, message="publish plugin")
    catalog_repo = repositories.create("catalog-a")
    catalog_path = catalog_repo.worktree / ".claude-plugin" / "marketplace.json"
    catalog_path.parent.mkdir()
    catalog = {
        "name": "catalog-a",
        "owner": {"name": "APM Test"},
        "plugins": [
            {
                "name": _PLUGIN,
                "source": {
                    "source": "git-subdir",
                    "url": _REMOTE_PLUGIN,
                    "path": _SUBDIR,
                    "ref": "main",
                },
            }
        ],
    }
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    repositories.commit(catalog_repo, message="publish catalog")
    environment = repositories.url_rewrite_subprocess_env_many(
        ((catalog_repo, _REMOTE_A), (plugin_repo, _REMOTE_PLUGIN))
    )
    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer",
        targets=("codex",),
        dependencies=({"name": _PLUGIN, "marketplace": _ALIAS},),
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    lock_path = consumer.root / "apm.lock.yaml"
    deployed_root = consumer.root / ".agents"
    runner.run_sequence(
        (
            ("marketplace", "add", _REMOTE_A, "--name", _ALIAS, "--ref", "main"),
            ("install", *_INSTALL_FLAGS),
        ),
        expected_returncodes=(0, 0),
        scenario_id="removed-plugin-install",
        cwd=consumer.root,
        env=environment,
    )
    assert _locked_row(lock_path)["marketplace_plugin_name"] == _PLUGIN
    locked_bytes = lock_path.read_bytes()
    deployed = _tree_bytes(deployed_root)
    assert deployed, "install deployed nothing"

    catalog["plugins"] = []
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    repositories.commit(catalog_repo, message="remove the plugin from the catalog")
    trace = tmp_path / "git-trace.log"

    (failed,) = runner.run_sequence(
        (("update", "--yes", *_UPDATE_FLAGS),),
        expected_returncodes=(1,),
        scenario_id="removed-plugin-update",
        cwd=consumer.root,
        env={**environment, "GIT_TRACE": str(trace)},
    )

    output = _output(failed)
    assert "Cannot re-verify the marketplace provenance of" in output, output
    assert f"{_PLUGIN}@{_ALIAS}" in output, output
    assert lock_path.read_bytes() == locked_bytes, output
    assert _tree_bytes(deployed_root) == deployed, output
    git_commands = trace.read_text(encoding="utf-8") if trace.exists() else ""
    assert "plugin-source" not in git_commands, git_commands
