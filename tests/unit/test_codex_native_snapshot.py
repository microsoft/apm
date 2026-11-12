"""Native transport generations preserve admitted package bytes and publisher identity."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from apm_cli.codex_plugins.snapshot import prepare_snapshot
from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.utils.content_hash import compute_native_bundle_hash

pytestmark = pytest.mark.component


def test_complete_selection_excludes_only_transport_metadata(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/config").write_text("transport only")
    (tmp_path / ".apm-pin").write_text("transport only")
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources/.apm-pin").write_text("publisher bytes")
    (tmp_path / "SKILL.md").write_text("publisher root skill")
    plan = DeployableSourcePlan.create(
        SimpleNamespace(install_path=tmp_path),
        [],
        skill_subset=None,
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=False,
        native_plugin_root=tmp_path,
    )
    assert plan.paths == frozenset({"resources/.apm-pin", "SKILL.md"})


def test_complete_selection_rejects_symlinks_before_git_pruning(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        DeployableSourcePlan.create(
            SimpleNamespace(install_path=source),
            [],
            skill_subset=None,
            hooks_approved=False,
            canvas_approved=False,
            skip_bin=False,
            native_plugin_root=source,
        )


def test_snapshot_keeps_ignored_resources_and_does_not_rewrite_manifest(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    manifest = source / ".codex-plugin/plugin.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"name": "example", "version": "1.0.0"}))
    (source / ".gitignore").write_text("resource.bin\n")
    (source / "resource.bin").write_bytes(b"\x00\x01\x02")
    paths = frozenset({".codex-plugin/plugin.json", ".gitignore", "resource.bin"})
    plan = DeployableSourcePlan(source_root=source, paths=paths)
    expected = compute_native_bundle_hash(source)
    snapshot = prepare_snapshot(
        plan, tmp_path / "transport", "apm-owned", "example", expected_hash=expected
    )
    assert compute_native_bundle_hash(snapshot.path / "plugins/example") == expected
    assert (
        snapshot.path / "plugins/example/.codex-plugin/plugin.json"
    ).read_bytes() == manifest.read_bytes()
    catalog = json.loads((snapshot.path / ".agents/plugins/marketplace.json").read_text())
    assert catalog["plugins"][0]["source"]["sha"] == snapshot.content_commit
    assert catalog["plugins"][0]["source"]["source"] == "url"
    assert snapshot.catalog_commit != snapshot.content_commit
    assert len(snapshot.catalog_commit) == 40


def test_snapshot_rejects_changed_source_after_admission(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "resource").write_text("admitted")
    plan = DeployableSourcePlan(source_root=source, paths=frozenset({"resource"}))
    expected = compute_native_bundle_hash(source)
    (source / "resource").write_text("changed after scan")
    with pytest.raises(ValueError, match="changed"):
        prepare_snapshot(
            plan, tmp_path / "transport", "apm-owned", "example", expected_hash=expected
        )
    assert not (tmp_path / "transport").exists()


def test_snapshot_does_not_accept_unselected_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "selected").write_text("selected")
    (source / "unselected").write_text("not admitted")
    plan = DeployableSourcePlan(source_root=source, paths=frozenset({"selected"}))
    with pytest.raises(ValueError, match="complete"):
        prepare_snapshot(
            plan,
            tmp_path / "transport",
            "apm-owned",
            "example",
            expected_hash=compute_native_bundle_hash(source),
        )
    assert not (tmp_path / "transport").exists()
