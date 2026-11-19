"""Real-CLI ``apm prune`` of object-form marketplace dependencies (#3960).

``apm prune`` keeps a declared, locked ``{name, marketplace}`` package with
the marketplace reachable or not, and removes it once the declaration is gone.
``--dry-run`` previews the same decision and writes nothing.
"""

from __future__ import annotations

import os
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


@pytest.mark.parametrize("reachable", [True, False], ids=["online", "offline"])
def test_prune_keeps_declared_object_form_marketplace_dependency(
    tmp_path: Path,
    apm_binary_path: Path,
    reachable: bool,
) -> None:
    """Prune keeps the declared package, then removes it once undeclared."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path.resolve() / "isolated", base_env=dict(os.environ)
    )
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    catalog, _ = _publish_catalog(repositories, "catalog-a", _REMOTE_A, "from catalog A")
    online = repositories.url_rewrite_subprocess_env(catalog, _REMOTE_A)
    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer",
        targets=("codex",),
        dependencies=({"name": _PLUGIN, "marketplace": _ALIAS},),
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=300)

    def run(env: dict[str, str], *args: str):
        (result,) = runner.run_sequence(
            (args,),
            expected_returncodes=(0,),
            scenario_id="object-form-prune",
            cwd=consumer.root,
            env=env,
        )
        return result

    run(online, "marketplace", "add", _REMOTE_A, "--name", _ALIAS, "--ref", "main")
    run(online, "install", *_INSTALL_FLAGS)
    lock_path = consumer.root / "apm.lock.yaml"
    assert load_yaml(lock_path)["dependencies"][0]["discovered_via"] == _ALIAS
    checkouts = [path for path in (consumer.root / "apm_modules").rglob(_PLUGIN) if path.is_dir()]
    assert checkouts

    env = online
    if not reachable:
        # Unregister the alias and drop the rewrite: only apm.yml and the lock remain.
        run(online, "marketplace", "remove", _ALIAS, "--yes")
        env = isolated.subprocess_env()
    lock_before = lock_path.read_bytes()

    for args in (("prune", "--dry-run"), ("prune",)):
        output = _output(run(env, *args))
        assert "No orphaned packages found" in output
        assert lock_path.read_bytes() == lock_before
        assert all(path.is_dir() for path in checkouts)

    manifest = load_yaml(consumer.manifest_path)
    manifest["dependencies"]["apm"] = []
    dump_yaml(manifest, consumer.manifest_path)
    preview = _output(run(env, "prune", "--dry-run"))
    assert "Found 1 orphaned package(s)" in preview
    assert all(path.is_dir() for path in checkouts)
    run(env, "prune")
    assert not any(path.exists() for path in checkouts)
