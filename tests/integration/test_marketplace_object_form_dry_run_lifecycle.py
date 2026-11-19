"""Real-CLI ``apm install --dry-run`` of object-form marketplace dependencies (#3960).

A locked ``{name, marketplace}`` declaration previews through the same lock
replay that ``apm install`` uses, in project and user scope, with the
marketplace reachable or not, and with or without a materialized copy. The
preview writes nothing.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_marketplace_origin_evidence_lifecycle import (
    _ALIAS,
    _INSTALL_FLAGS,
    _PLUGIN,
    _REMOTE_A,
    _output,
    _publish_catalog,
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

_DECLARATION = {"name": _PLUGIN, "marketplace": _ALIAS}


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
@pytest.mark.parametrize("reachable", [True, False], ids=["online", "offline"])
def test_install_dry_run_previews_locked_object_form_marketplace_dependency(
    tmp_path: Path,
    apm_binary_path: Path,
    user_scope: bool,
    reachable: bool,
) -> None:
    """Dry-run previews the locked package and changes nothing, warm or cold."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path.resolve() / "isolated", base_env=dict(os.environ)
    )
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    catalog, _ = _publish_catalog(repositories, "catalog-a", _REMOTE_A, "from catalog A")
    online = repositories.url_rewrite_subprocess_env(catalog, _REMOTE_A)
    scope: tuple[str, ...] = ("-g",) if user_scope else ()
    if user_scope:
        root = isolated.home / ".apm"
        cwd = isolated.work_root
        dump_yaml(
            {
                "name": "user",
                "version": "0.1.0",
                "targets": ["codex"],
                "dependencies": {"apm": [_DECLARATION]},
            },
            root / "apm.yml",
        )
    else:
        consumer = LocalPackageFactory(isolated.work_root).create(
            "consumer", targets=("codex",), dependencies=(_DECLARATION,)
        )
        root = cwd = consumer.root
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=300)

    def run(env: dict[str, str], *args: str, expected: int = 0):
        (result,) = runner.run_sequence(
            (args,),
            expected_returncodes=(expected,),
            scenario_id="object-form-dry-run",
            cwd=cwd,
            env=env,
        )
        return result

    run(online, "marketplace", "add", _REMOTE_A, "--name", _ALIAS, "--ref", "main")
    run(online, "install", *scope, *_INSTALL_FLAGS)
    assert load_yaml(root / "apm.lock.yaml")["dependencies"][0]["discovered_via"] == _ALIAS

    env = online
    if not reachable:
        # Unregister the alias and drop the rewrite: only apm.yml and the lock remain.
        run(online, "marketplace", "remove", _ALIAS, "--yes")
        env = isolated.subprocess_env()

    for cold in (False, True):
        if cold:
            shutil.rmtree(root / "apm_modules")
        before = _snapshot(root)
        preview = run(env, "install", *scope, "--dry-run", *_INSTALL_FLAGS)
        output = _output(preview)
        assert "Dry run mode" in output
        assert _PLUGIN in output
        assert "Cannot compute install path" not in output
        assert _snapshot(root) == before
