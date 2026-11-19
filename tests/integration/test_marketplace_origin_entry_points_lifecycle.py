"""Real-CLI marketplace origin check on every resolution entry point (#3959).

#3912 made `apm update` fail closed when an alias was rebound to another
catalog. The CLI `plugin@alias` install intercept (project and `-g`) and
`apm outdated` resolved through the rebound alias without that check.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.integration.test_marketplace_origin_evidence_lifecycle import (
    _ALIAS,
    _INSTALL_FLAGS,
    _PLUGIN,
    _REMOTE_A,
    _REMOTE_B,
    _output,
    _publish_catalog,
    _skill_document,
)
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

_DECLARED = {"name": _PLUGIN, "marketplace": _ALIAS}


def _assert_refused(result) -> None:
    output = _output(result)
    assert result.returncode == 1, output
    assert _REMOTE_A in output
    assert _REMOTE_B in output
    assert f"'{_ALIAS}'" in output
    assert "Nothing was changed" in output


@pytest.mark.parametrize("global_scope", [False, True], ids=["project", "global"])
def test_rebound_alias_fails_closed_on_cli_install_and_outdated(
    tmp_path: Path,
    apm_binary_path: Path,
    global_scope: bool,
) -> None:
    """`apm install plugin@alias` and `apm outdated` refuse a rebound alias."""
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
        dependencies=() if global_scope else (_DECLARED,),
    )
    scope_args = ("-g",) if global_scope else ()
    workspace = isolated.config_root if global_scope else consumer.root
    deploy_root = isolated.home if global_scope else consumer.root
    manifest_path = workspace / "apm.yml"
    if global_scope:
        dump_yaml(
            {
                "name": "user",
                "version": "0.1.0",
                "targets": ["codex"],
                "dependencies": {"apm": [_DECLARED]},
            },
            manifest_path,
        )
    lock_path = workspace / "apm.lock.yaml"
    deployed_skill = deploy_root / ".agents" / "skills" / _PLUGIN / "SKILL.md"
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    runner.run_sequence(
        (
            ("marketplace", "add", _REMOTE_A, "--name", _ALIAS, "--ref", "main"),
            ("install", *scope_args, *_INSTALL_FLAGS),
        ),
        expected_returncodes=(0, 0),
        scenario_id="entry-points-install",
        cwd=consumer.root,
        env=environment,
    )
    assert deployed_skill.read_text(encoding="utf-8") == _skill_document("from catalog A")
    manifest_before = manifest_path.read_bytes()
    lock_before = lock_path.read_bytes()

    (_rebind, install, outdated) = runner.run_sequence(
        (
            ("marketplace", "add", _REMOTE_B, "--name", _ALIAS, "--ref", "main"),
            ("install", *scope_args, f"{_PLUGIN}@{_ALIAS}", *_INSTALL_FLAGS),
            ("outdated", *scope_args),
        ),
        expected_returncodes=(0, 1, 1),
        scenario_id="entry-points-rebound-alias",
        cwd=consumer.root,
        env=environment,
    )
    _assert_refused(install)
    _assert_refused(outdated)
    assert manifest_path.read_bytes() == manifest_before
    assert lock_path.read_bytes() == lock_before
    assert deployed_skill.read_text(encoding="utf-8") == _skill_document("from catalog A")

    # The lock still matches the manifest, so a frozen replay keeps working.
    runner.run_sequence(
        (("install", *scope_args, "--frozen", *_INSTALL_FLAGS),),
        expected_returncodes=(0,),
        scenario_id="entry-points-frozen-replay",
        cwd=consumer.root,
        env=environment,
    )
    assert lock_path.read_bytes() == lock_before
    assert deployed_skill.read_text(encoding="utf-8") == _skill_document("from catalog A")
