"""Real-binary lifecycle coverage for local marketplace audit sources."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]


def _write_marketplace(
    root: Path,
    name: str,
    plugin_source: str | None,
    *,
    plugin_root: str = "",
) -> Path:
    """Write one local marketplace manifest with an optional string source."""
    marketplace = root / name
    marketplace.mkdir()
    plugins = []
    if plugin_source is not None:
        plugins.append(
            {
                "name": "local-plugin",
                "description": "Local audit lifecycle plugin",
                "source": plugin_source,
            }
        )
    manifest = {"name": name, "plugins": plugins}
    if plugin_root:
        manifest["metadata"] = {"pluginRoot": plugin_root}
    (marketplace / "marketplace.json").write_text(json.dumps(manifest), encoding="utf-8")
    return marketplace


def test_local_marketplace_audit_strict_lifecycle(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Strict audit accepts clean local plugins and rejects skipped escapes."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    runner = ApmLifecycleRunner((str(apm_binary_path),))

    clean = _write_marketplace(isolated.work_root, "clean-local", "./plugins/clean")
    clean_plugin = clean / "plugins" / "clean"
    clean_plugin.mkdir(parents=True)
    (clean_plugin / "apm.yml").write_text(
        "dependencies:\n  apm:\n    - clean-local@clean-local\n",
        encoding="utf-8",
    )

    skipped = _write_marketplace(isolated.work_root, "skipped-local", "./plugins/missing")
    empty = _write_marketplace(isolated.work_root, "empty-local", None)

    outside = isolated.work_root / "outside"
    outside.mkdir()
    (outside / "apm.yml").write_text("dependencies:\n  apm: [outside@outside]\n", encoding="utf-8")
    traversal = _write_marketplace(isolated.work_root, "traversal-local", "../outside")

    symlink = _write_marketplace(isolated.work_root, "symlink-local", "./plugins/escape")
    symlink_target = symlink / "plugins" / "escape"
    symlink_target.parent.mkdir()
    try:
        symlink_target.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    commands = (
        ("marketplace", "add", str(clean), "--name", "clean-local"),
        ("marketplace", "audit", "clean-local", "--strict", "--verbose"),
        ("marketplace", "add", str(skipped), "--name", "skipped-local"),
        ("marketplace", "audit", "skipped-local", "--strict", "--verbose"),
        ("marketplace", "add", str(empty), "--name", "empty-local"),
        ("marketplace", "audit", "empty-local", "--strict", "--verbose"),
        ("marketplace", "add", str(traversal), "--name", "traversal-local"),
        ("marketplace", "audit", "traversal-local", "--strict", "--verbose"),
        ("marketplace", "add", str(symlink), "--name", "symlink-local"),
        ("marketplace", "audit", "symlink-local", "--strict", "--verbose"),
    )
    results = runner.run_sequence(
        commands,
        expected_returncodes=(0, 0, 0, 1, 0, 1, 0, 1, 0, 1),
        scenario_id="marketplace-local-strict-audit",
        cwd=isolated.work_root,
        env=environment,
    )

    outputs = tuple(result.stdout + result.stderr for result in results)
    assert "Summary: 1 clean, 0 bypass warnings" in outputs[1]
    assert "--strict: no plugins were audited" in outputs[3]
    assert "--strict: no plugins were audited" in outputs[5]
    assert "traversal" in outputs[7]
    assert "outside" in outputs[9]


def test_local_marketplace_audit_strict_with_plugin_root(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Strict audit resolves bare local sources through metadata.pluginRoot."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    marketplace = _write_marketplace(
        isolated.work_root,
        "rooted-local",
        "hello",
        plugin_root="plugins",
    )
    plugin = marketplace / "plugins" / "hello"
    plugin.mkdir(parents=True)
    (plugin / "apm.yml").write_text(
        "dependencies:\n  apm:\n    - hello@rooted-local\n",
        encoding="utf-8",
    )

    results = ApmLifecycleRunner((str(apm_binary_path),)).run_sequence(
        (
            ("marketplace", "add", str(marketplace), "--name", "rooted-local"),
            ("marketplace", "audit", "rooted-local", "--strict", "--verbose"),
        ),
        expected_returncodes=(0, 0),
        scenario_id="marketplace-local-plugin-root-audit",
        cwd=isolated.work_root,
        env=isolated.subprocess_env(),
    )

    assert "Summary: 1 clean, 0 bypass warnings" in results[1].stdout + results[1].stderr
