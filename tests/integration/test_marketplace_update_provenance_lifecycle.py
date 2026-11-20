"""Real-CLI marketplace alias ownership across a moving Git branch update."""

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
_REPO = "team/platform/agent-catalog"
_REMOTE = f"https://{_HOST}/{_REPO}"
_MARKETPLACE = "example-marketplace"
_PLUGIN = "example-plugin"
_SUBDIR = f"plugins/{_PLUGIN}"
_ALIAS = f"{_PLUGIN}@{_MARKETPLACE}"


def _skill_document(revision: str) -> str:
    return (
        "---\n"
        f"name: {_PLUGIN}\n"
        "description: Marketplace update lifecycle fixture\n"
        "---\n"
        f"# {revision}\n"
    )


def _locked_dependency(lock_path: Path) -> dict[str, object]:
    dependencies = load_yaml(lock_path)["dependencies"]
    assert len(dependencies) == 1
    return dependencies[0]


@pytest.mark.parametrize("global_scope", [False, True], ids=["project", "global"])
def test_branch_update_retains_marketplace_alias_for_offline_uninstall(
    tmp_path: Path,
    apm_binary_path: Path,
    global_scope: bool,
) -> None:
    """A real branch advance preserves host-qualified provenance and removal."""
    # Canonical HOME keeps macOS /var and /private/var paths identical for ownership.
    isolated = IsolatedApmEnvironment.create(
        tmp_path.resolve() / "isolated", base_env=dict(os.environ)
    )
    environment = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("agent-catalog")
    packages = LocalPackageFactory(repository.worktree / "plugins")
    package = packages.create(_PLUGIN, targets=("codex",))
    skill_source = packages.add_skill(package, _PLUGIN, _skill_document("before update"))
    marketplace_dir = repository.worktree / ".claude-plugin"
    marketplace_dir.mkdir()
    (marketplace_dir / "marketplace.json").write_text(
        json.dumps(
            {
                "name": _MARKETPLACE,
                "owner": {"name": "APM Test"},
                "plugins": [
                    {
                        "name": _PLUGIN,
                        "source": {
                            "source": "git-subdir",
                            "url": _REMOTE,
                            "path": _SUBDIR,
                            "ref": "main",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    initial_commit = repositories.commit(repository, message="publish initial plugin")
    repositories.install_url_rewrite(repository, _REMOTE)
    (isolated.home / ".gitconfig").write_bytes(Path(environment["GIT_CONFIG_GLOBAL"]).read_bytes())
    environment = repositories.url_rewrite_subprocess_env(repository, _REMOTE)
    consumer = LocalPackageFactory(isolated.work_root).create("consumer", targets=("codex",))
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    scope_args = ("--global",) if global_scope else ()
    workspace = isolated.config_root if global_scope else consumer.root
    deploy_root = isolated.home if global_scope else consumer.root
    lock_path = workspace / "apm.lock.yaml"
    deployed_skill = deploy_root / ".agents" / "skills" / _PLUGIN / "SKILL.md"
    unrelated_skill = deploy_root / ".agents" / "skills" / "user-owned" / "SKILL.md"
    unrelated_skill.parent.mkdir(parents=True)
    unrelated_skill.write_text("User-owned content\n", encoding="utf-8")

    runner.run_sequence(
        (
            ("marketplace", "add", _REMOTE, "--name", _MARKETPLACE, "--ref", "main"),
            (
                "install",
                _ALIAS,
                *scope_args,
                "--target",
                "codex",
                "--no-policy",
                "--parallel-downloads",
                "0",
            ),
        ),
        expected_returncodes=(0, 0),
        scenario_id="marketplace-alias-install",
        cwd=consumer.root,
        env=environment,
    )
    before = _locked_dependency(lock_path)
    assert before["resolved_commit"] == initial_commit.sha
    assert before["discovered_via"] == _MARKETPLACE
    assert before["marketplace_plugin_name"] == _PLUGIN
    assert before["host"] == _HOST
    assert before["repo_url"] == _REPO
    assert before["virtual_path"] == _SUBDIR
    assert deployed_skill.read_text(encoding="utf-8") == _skill_document("before update")
    runner.run_sequence(
        (("uninstall", _ALIAS, *scope_args, "--dry-run"),),
        expected_returncodes=(0,),
        scenario_id="marketplace-alias-preview-before-update",
        cwd=consumer.root,
        env=environment,
    )

    # Advance only the producer's actual branch; consumer state is CLI-owned.
    skill_source.write_text(_skill_document("after update"), encoding="utf-8")
    updated_commit = repositories.commit(repository, message="advance plugin branch")
    runner.run_sequence(
        (("update", *scope_args, "--yes", "--target", "codex", "--parallel-downloads", "0"),),
        expected_returncodes=(0,),
        scenario_id="marketplace-alias-update",
        cwd=consumer.root,
        env=environment,
    )
    after = _locked_dependency(lock_path)
    assert after["resolved_commit"] == updated_commit.sha
    assert deployed_skill.read_text(encoding="utf-8") == _skill_document("after update")
    for field in ("discovered_via", "marketplace_plugin_name", "host", "repo_url", "virtual_path"):
        assert after.get(field) == before[field], field

    # Unregister the catalog so the offline lock lookup is the only alias source.
    runner.run_sequence(
        (
            ("marketplace", "remove", _MARKETPLACE, "--yes"),
            ("uninstall", _ALIAS, *scope_args),
        ),
        expected_returncodes=(0, 0),
        scenario_id="marketplace-alias-uninstall-offline",
        cwd=consumer.root,
        env=environment,
    )
    assert not deployed_skill.exists()
    assert unrelated_skill.read_text(encoding="utf-8") == "User-owned content\n"
    assert not lock_path.exists()
    assert not load_yaml(workspace / "apm.yml").get("dependencies", {}).get("apm")


_PROVENANCE = ("discovered_via", "marketplace_plugin_name", "source_url", "source_digest")
_TAMPERED = {
    "discovered_via": "other-marketplace",
    "marketplace_plugin_name": "other-plugin",
    "source_url": "https://gitlab.example.invalid/evil/agent-catalog",
    "source_digest": "sha256:" + "0" * 64,
}


def _output(result) -> str:
    return " ".join((result.stdout + result.stderr).split())


_REFUSE_ONLY = {
    # A current-format lock with all origin evidence cleared (S-lead rule 3).
    ("origin", "clear"),
    # An older generator stamp on a lock whose rows carry origin evidence (rule 1).
    ("apm_version", "downgrade"),
}


@pytest.mark.parametrize(
    ("field", "edit"),
    [
        *((field, edit) for field in _PROVENANCE for edit in ("tamper", "clear")),
        *sorted(_REFUSE_ONLY),
    ],
)
def test_update_restores_or_refuses_altered_marketplace_provenance(
    tmp_path: Path, apm_binary_path: Path, field: str, edit: str
) -> None:
    """#4228: update never keeps altered provenance; it restores it or fails closed."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path.resolve() / "isolated", base_env=dict(os.environ)
    )
    environment = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("agent-catalog")
    packages = LocalPackageFactory(repository.worktree / "plugins")
    package = packages.create(_PLUGIN, targets=("codex",))
    packages.add_skill(package, _PLUGIN, _skill_document("provenance"))
    (repository.worktree / ".claude-plugin").mkdir()
    (repository.worktree / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": _MARKETPLACE,
                "owner": {"name": "APM Test"},
                "plugins": [
                    {
                        "name": _PLUGIN,
                        "source": {
                            "source": "git-subdir",
                            "url": _REMOTE,
                            "path": _SUBDIR,
                            "ref": "main",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    repositories.commit(repository, message="publish plugin")
    repositories.install_url_rewrite(repository, _REMOTE)
    (isolated.home / ".gitconfig").write_bytes(Path(environment["GIT_CONFIG_GLOBAL"]).read_bytes())
    environment = repositories.url_rewrite_subprocess_env(repository, _REMOTE)
    consumer = LocalPackageFactory(isolated.work_root).create("consumer", targets=("codex",))
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    lock_path = consumer.root / "apm.lock.yaml"
    runner.run_sequence(
        (
            ("marketplace", "add", _REMOTE, "--name", _MARKETPLACE, "--ref", "main"),
            ("install", _ALIAS, "--target", "codex", "--no-policy", "--parallel-downloads", "0"),
        ),
        expected_returncodes=(0, 0),
        scenario_id="4228-install",
        cwd=consumer.root,
        env=environment,
    )
    before = _locked_dependency(lock_path)
    assert all(before.get(name) for name in _PROVENANCE), before

    lock = load_yaml(lock_path)
    if edit == "downgrade":
        lock["apm_version"] = "0.36.0"
    elif field == "origin":
        del lock["dependencies"][0]["source_url"]
        del lock["dependencies"][0]["source_digest"]
    elif edit == "tamper":
        lock["dependencies"][0][field] = _TAMPERED[field]
    else:
        del lock["dependencies"][0][field]
    dump_yaml(lock, lock_path)
    altered = lock_path.read_bytes()

    def run(*args: str):
        return runner.run(args, scenario_id=f"4228-{args[0]}", cwd=consumer.root, env=environment)

    update = run("update", "--yes", "--target", "codex", "--parallel-downloads", "0")

    if (field, edit) in _REFUSE_ONLY:
        assert update.returncode == 1, _output(update)
        assert "[x]" in _output(update) and "apm install" in _output(update), _output(update)
        assert lock_path.read_bytes() == altered
    elif update.returncode == 0:
        after = _locked_dependency(lock_path)
        assert {name: after.get(name) for name in _PROVENANCE} == {
            name: before[name] for name in _PROVENANCE
        }, _output(update)
    else:
        assert "[x]" in _output(update), _output(update)
        assert lock_path.read_bytes() == altered
    # Paired probe: install --frozen and audit --ci agree on the resulting state.
    frozen = run("install", "--frozen", "--target", "codex", "--parallel-downloads", "0")
    audit = run("audit", "--ci", "--no-policy")
    assert (frozen.returncode == 0) == (audit.returncode == 0), (
        _output(frozen) + " || " + _output(audit)
    )
