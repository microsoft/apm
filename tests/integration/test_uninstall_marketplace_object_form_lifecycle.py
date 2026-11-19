"""Real-CLI uninstall of an object-form marketplace dependency (#3928).

``apm.yml`` can declare a marketplace package as ``{name, marketplace}``.
``apm uninstall <plugin>@<marketplace>`` must select that declaration and
remove it, its lock row and its deployed files without consulting the
marketplace, exactly as it does for the string form.
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


def test_uninstall_selects_object_form_marketplace_declaration_offline(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """``plugin@alias`` removes an object-form entry with no marketplace access."""
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
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    lock_path = consumer.root / "apm.lock.yaml"
    deployed_skill = consumer.root / ".agents" / "skills" / _PLUGIN / "SKILL.md"

    runner.run_sequence(
        (
            ("marketplace", "add", _REMOTE_A, "--name", _ALIAS, "--ref", "main"),
            ("install", *_INSTALL_FLAGS),
        ),
        expected_returncodes=(0, 0),
        scenario_id="object-form-install",
        cwd=consumer.root,
        env=online,
    )
    assert load_yaml(lock_path)["dependencies"][0]["discovered_via"] == _ALIAS
    assert deployed_skill.read_text(encoding="utf-8") == _skill_document("from catalog A")
    modules_root = consumer.root / "apm_modules"
    checkouts = [path for path in modules_root.rglob(_PLUGIN) if path.is_dir()]
    assert checkouts

    # Unregister the alias and drop the catalog rewrite: the marketplace is
    # now unreachable, so only apm.yml and the lock can answer.
    runner.run_sequence(
        (("marketplace", "remove", _ALIAS, "--yes"),),
        expected_returncodes=(0,),
        scenario_id="object-form-unregister",
        cwd=consumer.root,
        env=online,
    )
    offline = isolated.subprocess_env()
    manifest_before = consumer.manifest_path.read_bytes()
    lock_before = lock_path.read_bytes()

    # A second declaration of the same package (its Git form) must not lose its
    # checkout, files and lock row: removing only one of them fails closed.
    manifest = load_yaml(consumer.manifest_path)
    manifest["dependencies"]["apm"].append(
        {"git": f"{_REMOTE_A}.git", "path": f"plugins/{_PLUGIN}"}
    )
    dump_yaml(manifest, consumer.manifest_path)
    shared_manifest = consumer.manifest_path.read_bytes()
    (refused,) = runner.run_sequence(
        (("uninstall", f"{_PLUGIN}@{_ALIAS}"),),
        expected_returncodes=(1,),
        scenario_id="object-form-uninstall-shared-coordinate",
        cwd=consumer.root,
        env=offline,
    )
    assert "Remove both together" in " ".join((refused.stdout + refused.stderr).split())
    assert consumer.manifest_path.read_bytes() == shared_manifest
    assert lock_path.read_bytes() == lock_before
    assert deployed_skill.exists()
    assert all(path.is_dir() for path in checkouts)
    consumer.manifest_path.write_bytes(manifest_before)

    (preview,) = runner.run_sequence(
        (("uninstall", f"{_PLUGIN}@{_ALIAS}", "--dry-run"),),
        expected_returncodes=(0,),
        scenario_id="object-form-uninstall-dry-run",
        cwd=consumer.root,
        env=offline,
    )
    assert _PLUGIN in preview.stdout
    assert "from apm_modules/" in preview.stdout
    assert consumer.manifest_path.read_bytes() == manifest_before
    assert lock_path.read_bytes() == lock_before
    assert deployed_skill.exists()
    assert all(path.is_dir() for path in checkouts)

    # The alias matches case-insensitively, as registry lookups do.
    runner.run_sequence(
        (("uninstall", f"{_PLUGIN}@{_ALIAS.upper()}"),),
        expected_returncodes=(0,),
        scenario_id="object-form-uninstall",
        cwd=consumer.root,
        env=offline,
    )
    assert not load_yaml(consumer.manifest_path).get("dependencies", {}).get("apm")
    assert not lock_path.exists() or not load_yaml(lock_path).get("dependencies")
    assert not deployed_skill.exists()
    assert not any(path.exists() for path in checkouts)
