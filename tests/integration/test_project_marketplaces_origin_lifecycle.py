"""Real-CLI lifecycle for the root ``marketplaces:`` map against remote catalogs (#2903).

The catalogs are Git repositories behind HTTPS URLs (rewritten to local
fixture repositories), so the lock records each catalog's origin
(``source_url``) and the resolved commit. Covers the file winning over a lock
written through a registration, ``apm update`` fetching the committed origin
with no registration, and ``marketplace.allow`` judging the locked origin as
well as the origins written in ``apm.yml``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

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
_PLUGIN = "example-plugin"
_SUBDIR = f"plugins/{_PLUGIN}"
_FLAGS = ("--target", "codex", "--parallel-downloads", "0")
_NO_POLICY = (*_FLAGS, "--no-policy")
# The policy leaf path doubles as the cache key auto-discovery derives from
# the project's ADO origin, so ``apm install`` enforces it as ``cached``.
_POLICY_SOURCE = "dev.azure.com/contoso/apm/apm-policy"
_POLICY_REMOTE = "https://dev.azure.com/contoso/project/_git/marketplace-allow"
_POLICY_DIAGNOSTIC = f"Policy: org:{_POLICY_SOURCE} (cached"


def _skill_document(label: str) -> str:
    return f"---\nname: {_PLUGIN}\ndescription: Project map fixture\n---\n# {label}\n"


def _publish_catalog(repositories: LocalGitRepositoryFactory, name: str, remote: str, label: str):
    repository = repositories.create(name)
    packages = LocalPackageFactory(repository.worktree / "plugins")
    package = packages.create(_PLUGIN, targets=("codex",))
    skill = packages.add_skill(package, _PLUGIN, _skill_document(label))
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
    return repository, skill


def _locked_row(project: Path) -> dict[str, object]:
    dependencies = load_yaml(project / "apm.lock.yaml")["dependencies"]
    assert len(dependencies) == 1, dependencies
    return dependencies[0]


def _set_marketplaces(manifest_path: Path, entries: list[dict[str, str]]) -> None:
    manifest = load_yaml(manifest_path)
    manifest["marketplaces"] = entries
    dump_yaml(manifest, manifest_path)


def _output(result) -> str:
    return " ".join(f"{result.stdout}\n{result.stderr}".split())


def _write_policy(project: Path, env: dict[str, str]) -> None:
    for args in (
        ("git", "init", "--initial-branch=main"),
        ("git", "remote", "add", "origin", _POLICY_REMOTE),
    ):
        subprocess.run(args, cwd=project, env=env, capture_output=True, check=True, timeout=30)
    strict = project / ".apm-policy" / "strict.yml"
    strict.parent.mkdir()
    dump_yaml(
        {
            "name": "marketplace-allow-contract",
            "version": "1",
            "enforcement": "block",
            "fetch_failure": "block",
            "marketplace": {"allow": [f"{_HOST}/team/catalog-b"]},
        },
        strict,
    )
    leaf = project.joinpath(*PurePosixPath(_POLICY_SOURCE).parts)
    leaf.parent.mkdir(parents=True)
    dump_yaml({"name": "marketplace-allow-leaf", "extends": ".apm-policy/strict.yml"}, leaf)


@pytest.fixture
def scenario(tmp_path: Path):
    isolated = IsolatedApmEnvironment.create(
        tmp_path.resolve() / "isolated", base_env=dict(os.environ)
    )
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    catalog_a, _ = _publish_catalog(repositories, "catalog-a", _REMOTE_A, "from catalog A")
    catalog_b, skill_b = _publish_catalog(repositories, "catalog-b", _REMOTE_B, "from catalog B")
    env = repositories.url_rewrite_subprocess_env_many(
        ((catalog_a, _REMOTE_A), (catalog_b, _REMOTE_B))
    )

    def publish_b(label: str) -> None:
        skill_b.write_text(_skill_document(label), encoding="utf-8")
        repositories.commit(catalog_b, message=label)

    return isolated, env, publish_b


def _consumer(isolated: IsolatedApmEnvironment, name: str) -> Path:
    return (
        LocalPackageFactory(isolated.work_root)
        .create(
            name,
            targets=("codex",),
            dependencies=({"name": _PLUGIN, "marketplace": "acme"},),
        )
        .root
    )


def test_root_map_wins_over_a_lock_written_through_a_registration(scenario, apm_binary_path):
    """Lock via registration A, then commit a map naming B: install follows the file."""
    isolated, env, publish_b = scenario
    project = _consumer(isolated, "consumer")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    skill = project / ".agents" / "skills" / _PLUGIN / "SKILL.md"

    runner.run_sequence(
        (
            ("marketplace", "add", _REMOTE_A, "--name", "acme", "--ref", "main"),
            ("install", *_NO_POLICY),
        ),
        expected_returncodes=(0, 0),
        scenario_id="map-lock-via-registration",
        cwd=project,
        env=env,
    )
    locked = _locked_row(project)
    assert locked["source_url"] == _REMOTE_A
    assert re.fullmatch(r"[0-9a-f]{40}", str(locked["resolved_commit"]))
    assert skill.read_text(encoding="utf-8") == _skill_document("from catalog A")

    _set_marketplaces(project / "apm.yml", [{"name": "acme", "source": _REMOTE_B, "ref": "main"}])
    before = (project / "apm.lock.yaml").read_bytes()
    (frozen,) = runner.run_sequence(
        (("install", "--frozen", *_NO_POLICY),),
        expected_returncodes=(1,),
        scenario_id="map-origin-changed-frozen",
        cwd=project,
        env=env,
    )
    frozen_output = _output(frozen)
    assert _REMOTE_A in frozen_output and _REMOTE_B in frozen_output
    assert "without --frozen" in frozen_output
    assert (project / "apm.lock.yaml").read_bytes() == before
    (install,) = runner.run_sequence(
        (("install", *_NO_POLICY),),
        expected_returncodes=(0,),
        scenario_id="map-origin-changed-install",
        cwd=project,
        env=env,
    )
    install_output = _output(install)
    assert "[i] Marketplace 'acme' in apm.yml now names" in install_output
    assert install_output.count("Re-resolving through apm.yml") == 1
    locked = _locked_row(project)
    assert locked["source_url"] == _REMOTE_B
    assert re.fullmatch(r"[0-9a-f]{40}", str(locked["resolved_commit"]))
    assert skill.read_text(encoding="utf-8") == _skill_document("from catalog B")

    # With no registration at all, apm update fetches the committed origin.
    publish_b("from catalog B v2")
    runner.run_sequence(
        (("marketplace", "remove", "acme", "--yes"), ("update", "--yes", *_FLAGS)),
        expected_returncodes=(0, 0),
        scenario_id="map-update-without-registration",
        cwd=project,
        env=env,
    )
    updated = _locked_row(project)
    assert updated["source_url"] == _REMOTE_B
    assert updated["resolved_commit"] != locked["resolved_commit"]
    assert skill.read_text(encoding="utf-8") == _skill_document("from catalog B v2")


def test_marketplace_allow_judges_locked_and_declared_origins(scenario, apm_binary_path):
    """A lock from a disallowed catalog is refused even when apm.yml declares allowed ones."""
    isolated, env, _publish_b = scenario
    project = _consumer(isolated, "governed")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    # The lock is written through a registration of disallowed catalog A.
    runner.run_sequence(
        (
            ("marketplace", "add", _REMOTE_A, "--name", "acme", "--ref", "main"),
            ("install", *_NO_POLICY),
        ),
        expected_returncodes=(0, 0),
        scenario_id="allow-lock-via-registration",
        cwd=project,
        env=env,
    )
    assert _locked_row(project)["source_url"] == _REMOTE_A
    # apm.yml declares only an allowed origin, under a name the lock does not use.
    _set_marketplaces(
        project / "apm.yml", [{"name": "approved", "source": _REMOTE_B, "ref": "main"}]
    )
    _write_policy(project, env)
    audit_args = (
        "audit",
        "--ci",
        "--no-fail-fast",
        "--no-drift",
        "--format",
        "json",
        "--policy",
        _POLICY_SOURCE,
    )
    _warm, install, audit = runner.run_sequence(
        (
            (
                "policy",
                "status",
                "--policy-source",
                _POLICY_SOURCE,
                "--json",
                "--check",
                "--no-cache",
            ),
            ("install", *_FLAGS),
            audit_args,
        ),
        expected_returncodes=(0, 1, 1),
        scenario_id="allow-locked-origin-refused",
        cwd=project,
        env=env,
    )
    install_output = _output(install)
    assert _POLICY_DIAGNOSTIC in install_output, install_output
    assert f"{_PLUGIN}@acme" in install_output
    assert "marketplace.allow" in install_output
    checks = {check["name"]: check for check in json.loads(audit.stdout)["checks"]}
    assert checks["marketplace-allow"]["passed"] is False
    assert f"{_HOST}/team/catalog-a" in json.dumps(checks["marketplace-allow"])

    # A declared origin outside the list is refused as well.
    _set_marketplaces(project / "apm.yml", [{"name": "acme", "source": _REMOTE_A, "ref": "main"}])
    (declared,) = runner.run_sequence(
        (("install", *_FLAGS),),
        expected_returncodes=(1,),
        scenario_id="allow-declared-origin-refused",
        cwd=project,
        env=env,
    )
    declared_output = _output(declared)
    assert "marketplace 'acme'" in declared_output
    assert "marketplace.allow" in declared_output
