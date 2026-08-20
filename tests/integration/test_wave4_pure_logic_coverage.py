"""Wave 4 integration tests -- pure logic modules (no I/O mocking needed).

Targets:
    - install/plan.py  (117 miss, 20% cov)
    - marketplace/yml_editor.py  (145 miss, 0% cov)
    - bundle/unpacker.py  (109 miss, 11% cov)

All three modules are exercisable with tmp_path and no network mocking.
"""

from __future__ import annotations

import tarfile
from pathlib import Path
from typing import Any

import pytest

# ===================================================================
# install/plan.py -- pure-function update-plan builder
# ===================================================================


class _FakeLockedDep:
    """Minimal stand-in for LockedDependency."""

    def __init__(
        self,
        repo_url: str = "https://github.com/owner/repo",
        resolved_ref: str | None = "main",
        resolved_commit: str | None = "abc1234567890",
        content_hash: str | None = "sha256:deadbeef",
        deployed_files: list[str] | None = None,
        virtual_path: str | None = None,
    ) -> None:
        self.repo_url = repo_url
        self.resolved_ref = resolved_ref
        self.resolved_commit = resolved_commit
        self.content_hash = content_hash
        self.deployed_files = deployed_files or []
        self.virtual_path = virtual_path

    def get_unique_key(self) -> str:
        if self.virtual_path:
            return f"{self.repo_url}/{self.virtual_path}"
        return self.repo_url


class _FakeLockFile:
    """Minimal stand-in for LockFile."""

    def __init__(self, deps: dict[str, _FakeLockedDep] | None = None) -> None:
        self.dependencies = deps or {}


class _FakeDepRef:
    """Minimal stand-in for DependencyReference."""

    def __init__(
        self,
        repo_url: str = "https://github.com/owner/repo",
        reference: str | None = "main",
        is_local: bool = False,
        local_path: str | None = None,
        is_virtual: bool = False,
        virtual_path: str | None = None,
        host: str | None = None,
        resolved_reference: Any = None,
    ) -> None:
        self.repo_url = repo_url
        self.reference = reference
        self.is_local = is_local
        self.local_path = local_path
        self.is_virtual = is_virtual
        self.virtual_path = virtual_path
        self.host = host
        self.resolved_reference = resolved_reference


class _FakeResolvedRef:
    """Minimal stand-in for ResolvedReference."""

    def __init__(
        self,
        ref_name: str | None = None,
        original_ref: str | None = None,
        resolved_commit: str | None = None,
    ) -> None:
        self.ref_name = ref_name
        self.original_ref = original_ref
        self.resolved_commit = resolved_commit


class TestPlanEntryDataclass:
    """Test PlanEntry frozen dataclass properties."""

    def test_has_changes_true_for_update(self) -> None:
        from apm_cli.install.plan import PlanEntry

        entry = PlanEntry(dep_key="k", action="update")
        assert entry.has_changes is True

    def test_has_changes_true_for_add(self) -> None:
        from apm_cli.install.plan import PlanEntry

        entry = PlanEntry(dep_key="k", action="add")
        assert entry.has_changes is True

    def test_has_changes_true_for_remove(self) -> None:
        from apm_cli.install.plan import PlanEntry

        entry = PlanEntry(dep_key="k", action="remove")
        assert entry.has_changes is True

    def test_has_changes_false_for_unchanged(self) -> None:
        from apm_cli.install.plan import PlanEntry

        entry = PlanEntry(dep_key="k", action="unchanged")
        assert entry.has_changes is False

    def test_short_old_commit_truncates(self) -> None:
        from apm_cli.install.plan import PlanEntry

        entry = PlanEntry(dep_key="k", action="update", old_resolved_commit="abcdef1234567890")
        assert entry.short_old_commit == "abcdef1"

    def test_short_new_commit_truncates(self) -> None:
        from apm_cli.install.plan import PlanEntry

        entry = PlanEntry(dep_key="k", action="update", new_resolved_commit="fedcba0987654321")
        assert entry.short_new_commit == "fedcba0"

    def test_short_commit_with_none(self) -> None:
        from apm_cli.install.plan import PlanEntry

        entry = PlanEntry(dep_key="k", action="add")
        assert entry.short_old_commit == "-"
        assert entry.short_new_commit == "-"


class TestUpdatePlanDataclass:
    """Test UpdatePlan frozen dataclass properties."""

    def test_has_changes_empty(self) -> None:
        from apm_cli.install.plan import UpdatePlan

        plan = UpdatePlan()
        assert plan.has_changes is False

    def test_has_changes_with_unchanged_only(self) -> None:
        from apm_cli.install.plan import PlanEntry, UpdatePlan

        plan = UpdatePlan(entries=(PlanEntry(dep_key="k", action="unchanged"),))
        assert plan.has_changes is False

    def test_has_changes_with_update(self) -> None:
        from apm_cli.install.plan import PlanEntry, UpdatePlan

        plan = UpdatePlan(entries=(PlanEntry(dep_key="k", action="update"),))
        assert plan.has_changes is True

    def test_changed_entries_filters(self) -> None:
        from apm_cli.install.plan import PlanEntry, UpdatePlan

        plan = UpdatePlan(
            entries=(
                PlanEntry(dep_key="a", action="update"),
                PlanEntry(dep_key="b", action="unchanged"),
                PlanEntry(dep_key="c", action="add"),
            )
        )
        changed = plan.changed_entries
        assert len(changed) == 2
        assert changed[0].dep_key == "a"
        assert changed[1].dep_key == "c"

    def test_summary_counts(self) -> None:
        from apm_cli.install.plan import PlanEntry, UpdatePlan

        plan = UpdatePlan(
            entries=(
                PlanEntry(dep_key="a", action="update"),
                PlanEntry(dep_key="b", action="update"),
                PlanEntry(dep_key="c", action="add"),
                PlanEntry(dep_key="d", action="remove"),
                PlanEntry(dep_key="e", action="unchanged"),
            )
        )
        counts = plan.summary_counts
        assert counts["update"] == 2
        assert counts["add"] == 1
        assert counts["remove"] == 1
        assert counts["unchanged"] == 1


class TestBuildUpdatePlan:
    """Test build_update_plan with fake deps and lockfiles."""

    def test_all_new_deps(self) -> None:
        from apm_cli.install.plan import build_update_plan

        deps = [
            _FakeDepRef(repo_url="https://github.com/a/b", reference="main"),
            _FakeDepRef(repo_url="https://github.com/c/d", reference="v1"),
        ]
        plan = build_update_plan(None, deps)
        assert len(plan.entries) == 2
        assert all(e.action == "add" for e in plan.entries)

    def test_unchanged_deps(self) -> None:
        from apm_cli.install.plan import build_update_plan

        locked = _FakeLockedDep(
            repo_url="https://github.com/a/b",
            resolved_ref="main",
            resolved_commit="abc1234",
        )
        lockfile = _FakeLockFile({"https://github.com/a/b": locked})
        dep = _FakeDepRef(
            repo_url="https://github.com/a/b",
            resolved_reference=_FakeResolvedRef(ref_name="main", resolved_commit="abc1234"),
        )
        plan = build_update_plan(lockfile, [dep])
        assert len(plan.entries) == 1
        assert plan.entries[0].action == "unchanged"

    def test_updated_deps(self) -> None:
        from apm_cli.install.plan import build_update_plan

        locked = _FakeLockedDep(
            repo_url="https://github.com/a/b",
            resolved_ref="main",
            resolved_commit="abc1234",
            deployed_files=[".github/copilot-instructions.md"],
        )
        lockfile = _FakeLockFile({"https://github.com/a/b": locked})
        dep = _FakeDepRef(
            repo_url="https://github.com/a/b",
            resolved_reference=_FakeResolvedRef(ref_name="main", resolved_commit="def5678"),
        )
        plan = build_update_plan(lockfile, [dep])
        assert len(plan.entries) == 1
        assert plan.entries[0].action == "update"
        assert plan.entries[0].deployed_files == (".github/copilot-instructions.md",)

    def test_removed_deps(self) -> None:
        from apm_cli.install.plan import build_update_plan

        locked = _FakeLockedDep(
            repo_url="https://github.com/a/b",
            resolved_ref="main",
            resolved_commit="abc1234",
        )
        lockfile = _FakeLockFile({"https://github.com/a/b": locked})
        plan = build_update_plan(lockfile, [])
        assert len(plan.entries) == 1
        assert plan.entries[0].action == "remove"

    def test_mixed_plan(self) -> None:
        from apm_cli.install.plan import build_update_plan

        locked_a = _FakeLockedDep(
            repo_url="https://github.com/a/b",
            resolved_ref="main",
            resolved_commit="abc1234",
        )
        locked_b = _FakeLockedDep(
            repo_url="https://github.com/c/d",
            resolved_ref="v1",
            resolved_commit="xyz9999",
        )
        lockfile = _FakeLockFile(
            {
                "https://github.com/a/b": locked_a,
                "https://github.com/c/d": locked_b,
            }
        )
        deps = [
            _FakeDepRef(
                repo_url="https://github.com/a/b",
                resolved_reference=_FakeResolvedRef(ref_name="main", resolved_commit="abc1234"),
            ),
            _FakeDepRef(
                repo_url="https://github.com/e/f",
                reference="main",
            ),
        ]
        plan = build_update_plan(lockfile, deps)
        actions = {e.action for e in plan.entries}
        assert "unchanged" in actions
        assert "add" in actions
        assert "remove" in actions

    def test_local_dep_key(self) -> None:
        from apm_cli.install.plan import _dep_ref_key

        dep = _FakeDepRef(is_local=True, local_path="./local-pkg")
        assert _dep_ref_key(dep) == "./local-pkg"

    def test_virtual_dep_key(self) -> None:
        from apm_cli.install.plan import _dep_ref_key

        dep = _FakeDepRef(
            repo_url="https://github.com/a/b",
            is_virtual=True,
            virtual_path="packages/skill-a",
        )
        assert _dep_ref_key(dep) == "https://github.com/a/b/packages/skill-a"

    def test_non_default_host_dep_key(self) -> None:
        from apm_cli.install.plan import _dep_ref_key

        dep = _FakeDepRef(repo_url="org/r", host="ghe.example.com")
        assert _dep_ref_key(dep) == "ghe.example.com/org/r"

    def test_self_key_excluded(self) -> None:
        from apm_cli.install.plan import build_update_plan

        locked_self = _FakeLockedDep(repo_url=".")
        locked_dep = _FakeLockedDep(
            repo_url="https://github.com/a/b",
            resolved_ref="main",
            resolved_commit="abc1234",
        )
        lockfile = _FakeLockFile(
            {
                ".": locked_self,
                "https://github.com/a/b": locked_dep,
            }
        )
        plan = build_update_plan(lockfile, [])
        assert len(plan.entries) == 1
        assert plan.entries[0].action == "remove"

    def test_extract_new_ref_and_commit_no_resolved(self) -> None:
        from apm_cli.install.plan import _extract_new_ref_and_commit

        dep = _FakeDepRef(reference="v2.0.0")
        ref, commit = _extract_new_ref_and_commit(dep)
        assert ref == "v2.0.0"
        assert commit is None

    def test_extract_new_ref_and_commit_with_resolved(self) -> None:
        from apm_cli.install.plan import _extract_new_ref_and_commit

        dep = _FakeDepRef(
            resolved_reference=_FakeResolvedRef(ref_name="v2.0.0", resolved_commit="aaa1111")
        )
        ref, commit = _extract_new_ref_and_commit(dep)
        assert ref == "v2.0.0"
        assert commit == "aaa1111"


class TestRenderPlanText:
    """Test render_plan_text ASCII output."""

    def test_empty_plan_returns_empty(self) -> None:
        from apm_cli.install.plan import UpdatePlan, render_plan_text

        plan = UpdatePlan()
        assert render_plan_text(plan) == ""

    def test_add_entry_rendering(self) -> None:
        from apm_cli.install.plan import PlanEntry, UpdatePlan, render_plan_text

        plan = UpdatePlan(
            entries=(
                PlanEntry(
                    dep_key="k",
                    action="add",
                    display_name="owner/repo",
                    new_resolved_ref="main",
                    new_resolved_commit="abc1234567890",
                ),
            )
        )
        text = render_plan_text(plan)
        assert "owner/repo" in text
        assert "abc1234" in text
        assert "new" in text

    def test_remove_entry_rendering(self) -> None:
        from apm_cli.install.plan import PlanEntry, UpdatePlan, render_plan_text

        plan = UpdatePlan(
            entries=(
                PlanEntry(
                    dep_key="k",
                    action="remove",
                    display_name="owner/old-pkg",
                    old_resolved_ref="v1.0",
                    old_resolved_commit="dead000beef000",
                ),
            )
        )
        text = render_plan_text(plan)
        assert "owner/old-pkg" in text
        assert "removed" in text

    def test_update_entry_rendering(self) -> None:
        from apm_cli.install.plan import PlanEntry, UpdatePlan, render_plan_text

        plan = UpdatePlan(
            entries=(
                PlanEntry(
                    dep_key="k",
                    action="update",
                    display_name="owner/pkg",
                    old_resolved_ref="main",
                    old_resolved_commit="aaa1111222233334",
                    new_resolved_ref="main",
                    new_resolved_commit="bbb2222333344445",
                    deployed_files=("file1.md", "file2.md", "file3.md", "file4.md"),
                ),
            )
        )
        text = render_plan_text(plan)
        assert "owner/pkg" in text
        assert "aaa1111" in text
        assert "bbb2222" in text
        assert "+1 more" in text

    def test_verbose_shows_unchanged(self) -> None:
        from apm_cli.install.plan import PlanEntry, UpdatePlan, render_plan_text

        plan = UpdatePlan(
            entries=(
                PlanEntry(
                    dep_key="k",
                    action="unchanged",
                    display_name="owner/stable",
                    old_resolved_ref="main",
                    old_resolved_commit="aaa1111222233334",
                    new_resolved_ref="main",
                    new_resolved_commit="aaa1111222233334",
                ),
            )
        )
        text_normal = render_plan_text(plan)
        text_verbose = render_plan_text(plan, verbose=True)
        assert text_normal == ""
        assert "owner/stable" in text_verbose
        assert "unchanged" in text_verbose

    def test_summary_line(self) -> None:
        from apm_cli.install.plan import PlanEntry, UpdatePlan, render_plan_text

        plan = UpdatePlan(
            entries=(
                PlanEntry(dep_key="a", action="update", display_name="x"),
                PlanEntry(dep_key="b", action="add", display_name="y"),
            )
        )
        text = render_plan_text(plan)
        assert "1 updated" in text
        assert "1 added" in text


class TestLockfileSatisfiesManifest:
    """Test lockfile_satisfies_manifest structural check."""

    def test_satisfied(self) -> None:
        from apm_cli.install.plan import lockfile_satisfies_manifest

        locked = _FakeLockedDep(repo_url="https://github.com/a/b")
        lockfile = _FakeLockFile({"https://github.com/a/b": locked})
        dep = _FakeDepRef(repo_url="https://github.com/a/b")
        satisfied, reasons = lockfile_satisfies_manifest(lockfile, [dep])
        assert satisfied is True
        assert reasons == []

    def test_unsatisfied(self) -> None:
        from apm_cli.install.plan import lockfile_satisfies_manifest

        lockfile = _FakeLockFile({})
        dep = _FakeDepRef(repo_url="https://github.com/a/b")
        satisfied, reasons = lockfile_satisfies_manifest(lockfile, [dep])
        assert satisfied is False
        assert len(reasons) == 1
        assert "missing" in reasons[0]

    def test_local_deps_skipped(self) -> None:
        from apm_cli.install.plan import lockfile_satisfies_manifest

        lockfile = _FakeLockFile({})
        dep = _FakeDepRef(is_local=True, local_path="./local")
        satisfied, _reasons = lockfile_satisfies_manifest(lockfile, [dep])
        assert satisfied is True


class TestDisplayName:
    """Test _display_name helper."""

    def test_with_locked_dep(self) -> None:
        from apm_cli.install.plan import _display_name

        locked = _FakeLockedDep(repo_url="https://github.com/a/b")
        assert _display_name("key", locked) == "https://github.com/a/b"

    def test_with_virtual_path(self) -> None:
        from apm_cli.install.plan import _display_name

        locked = _FakeLockedDep(repo_url="https://github.com/a/b", virtual_path="packages/s1")
        assert "packages/s1" in _display_name("key", locked)

    def test_fallback_to_key(self) -> None:
        from apm_cli.install.plan import _display_name

        assert _display_name("my-key", None) == "my-key"


# ===================================================================
# marketplace/yml_editor.py -- round-trip YAML editing (0% coverage)
# ===================================================================


def _write_marketplace_yml(path: Path, content: str) -> Path:
    """Write a marketplace.yml to path and return its Path."""
    yml_path = path / "marketplace.yml"
    yml_path.write_text(content, encoding="utf-8")
    return yml_path


_VALID_MARKETPLACE = """\
name: test-marketplace
description: A test marketplace
version: "1.0.0"
owner:
  name: test-org
packages:
  - name: existing-pkg
    source: owner/existing-pkg
    version: "1.0.0"
"""

_VALID_APM_YML_WITH_MARKETPLACE = """\
name: my-project
version: 1.0.0
description: A test project
marketplace:
  owner:
    name: test-org
  packages:
    - name: existing-pkg
      source: owner/existing-pkg
      version: "1.0.0"
"""


class TestYmlEditorHelpers:
    """Test yml_editor internal helpers."""

    def test_rt_yaml_returns_instance(self) -> None:
        from apm_cli.marketplace.yml_editor import _rt_yaml

        yml = _rt_yaml()
        assert yml.preserve_quotes is True

    def test_load_rt(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import _load_rt

        yml_path = _write_marketplace_yml(tmp_path, _VALID_MARKETPLACE)
        data, text = _load_rt(yml_path)
        assert data["name"] == "test-marketplace"
        assert "packages" in text

    def test_dump_rt_roundtrip(self) -> None:
        from apm_cli.marketplace.yml_editor import _dump_rt, _rt_yaml

        data = _rt_yaml().load("owner: test\nname: hello\n")
        text = _dump_rt(data)
        assert "owner: test" in text
        assert "name: hello" in text

    def test_is_apm_yml_with_marketplace(self) -> None:
        from apm_cli.marketplace.yml_editor import _is_apm_yml_with_marketplace

        assert _is_apm_yml_with_marketplace({"marketplace": {"owner": "x"}}) is True
        assert _is_apm_yml_with_marketplace({"owner": "x"}) is False
        assert _is_apm_yml_with_marketplace({"marketplace": "scalar"}) is False
        assert _is_apm_yml_with_marketplace("not-a-dict") is False

    def test_get_marketplace_container_apm_yml(self) -> None:
        from apm_cli.marketplace.yml_editor import _get_marketplace_container

        data = {"marketplace": {"owner": "x", "packages": []}}
        assert _get_marketplace_container(data) is data["marketplace"]

    def test_get_marketplace_container_legacy(self) -> None:
        from apm_cli.marketplace.yml_editor import _get_marketplace_container

        data = {"owner": "x", "packages": []}
        assert _get_marketplace_container(data) is data

    def test_find_entry_index_found(self) -> None:
        from apm_cli.marketplace.yml_editor import _find_entry_index

        packages = [{"name": "alpha"}, {"name": "Beta"}]
        assert _find_entry_index(packages, "alpha") == 0
        assert _find_entry_index(packages, "BETA") == 1

    def test_find_entry_index_not_found(self) -> None:
        from apm_cli.marketplace.yml_editor import (
            MarketplaceYmlError,
            _find_entry_index,
        )

        with pytest.raises(MarketplaceYmlError, match=r"not found"):
            _find_entry_index([{"name": "alpha"}], "missing")

    def test_validate_source_valid(self) -> None:
        from apm_cli.marketplace.yml_editor import _validate_source

        _validate_source("owner/repo")

    def test_validate_source_local_path(self) -> None:
        from apm_cli.marketplace.yml_editor import _validate_source

        _validate_source("./local-dir")

    def test_validate_source_invalid(self) -> None:
        from apm_cli.marketplace.yml_editor import (
            MarketplaceYmlError,
            _validate_source,
        )

        with pytest.raises(MarketplaceYmlError):
            _validate_source("no-slash")

    def test_validate_subdir_traversal(self) -> None:
        from apm_cli.marketplace.yml_editor import (
            MarketplaceYmlError,
            _validate_subdir,
        )

        with pytest.raises(MarketplaceYmlError):
            _validate_subdir("../../etc")


class TestAddPluginEntry:
    """Test add_plugin_entry public API."""

    def test_add_with_version(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import add_plugin_entry

        yml_path = _write_marketplace_yml(tmp_path, _VALID_MARKETPLACE)
        name = add_plugin_entry(yml_path, source="owner/new-pkg", version="2.0.0")
        assert name == "new-pkg"
        content = yml_path.read_text()
        assert "new-pkg" in content
        assert "2.0.0" in content

    def test_add_with_ref(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import add_plugin_entry

        yml_path = _write_marketplace_yml(tmp_path, _VALID_MARKETPLACE)
        name = add_plugin_entry(yml_path, source="owner/ref-pkg", name="custom-name", ref="main")
        assert name == "custom-name"

    def test_add_with_all_fields(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import add_plugin_entry

        yml_path = _write_marketplace_yml(tmp_path, _VALID_MARKETPLACE)
        add_plugin_entry(
            yml_path,
            source="owner/full-pkg",
            version="1.0.0",
            subdir="packages/sub",
            tag_pattern="v{version}",
            tags=["ai", "tools"],
            include_prerelease=True,
        )
        content = yml_path.read_text()
        assert "full-pkg" in content
        assert "tag_pattern" in content

    def test_add_duplicate_raises(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import (
            MarketplaceYmlError,
            add_plugin_entry,
        )

        yml_path = _write_marketplace_yml(tmp_path, _VALID_MARKETPLACE)
        with pytest.raises(MarketplaceYmlError, match=r"already exists"):
            add_plugin_entry(yml_path, source="owner/existing-pkg", version="1.0.0")

    def test_add_version_and_ref_raises(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import (
            MarketplaceYmlError,
            add_plugin_entry,
        )

        yml_path = _write_marketplace_yml(tmp_path, _VALID_MARKETPLACE)
        with pytest.raises(MarketplaceYmlError, match=r"Cannot specify both"):
            add_plugin_entry(yml_path, source="owner/bad", version="1.0", ref="main")

    def test_add_no_version_no_ref_raises(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import (
            MarketplaceYmlError,
            add_plugin_entry,
        )

        yml_path = _write_marketplace_yml(tmp_path, _VALID_MARKETPLACE)
        with pytest.raises(MarketplaceYmlError, match=r"At least one"):
            add_plugin_entry(yml_path, source="owner/bad")

    def test_add_to_apm_yml_marketplace_block(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import add_plugin_entry

        yml_path = tmp_path / "apm.yml"
        yml_path.write_text(_VALID_APM_YML_WITH_MARKETPLACE, encoding="utf-8")
        name = add_plugin_entry(yml_path, source="owner/apm-pkg", version="1.0.0")
        assert name == "apm-pkg"

    def test_add_creates_packages_if_missing(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import add_plugin_entry

        yml_path = _write_marketplace_yml(
            tmp_path,
            "name: test-mp\ndescription: Test\nversion: '1.0.0'\nowner:\n  name: test-org\n",
        )
        name = add_plugin_entry(yml_path, source="owner/first", version="1.0.0")
        assert name == "first"


class TestUpdatePluginEntry:
    """Test update_plugin_entry public API."""

    def test_update_version(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import update_plugin_entry

        yml_path = _write_marketplace_yml(tmp_path, _VALID_MARKETPLACE)
        update_plugin_entry(yml_path, "existing-pkg", version="2.0.0")
        content = yml_path.read_text()
        assert "2.0.0" in content

    def test_update_to_ref(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import update_plugin_entry

        yml_path = _write_marketplace_yml(tmp_path, _VALID_MARKETPLACE)
        update_plugin_entry(yml_path, "existing-pkg", ref="develop")
        content = yml_path.read_text()
        assert "develop" in content
        # version key should be removed when ref is set
        assert "ref: develop" in content

    def test_update_both_version_and_ref_raises(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import (
            MarketplaceYmlError,
            update_plugin_entry,
        )

        yml_path = _write_marketplace_yml(tmp_path, _VALID_MARKETPLACE)
        with pytest.raises(MarketplaceYmlError, match=r"Cannot specify both"):
            update_plugin_entry(yml_path, "existing-pkg", version="2.0", ref="main")

    def test_update_not_found_raises(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import (
            MarketplaceYmlError,
            update_plugin_entry,
        )

        yml_path = _write_marketplace_yml(tmp_path, _VALID_MARKETPLACE)
        with pytest.raises(MarketplaceYmlError, match=r"not found"):
            update_plugin_entry(yml_path, "missing-pkg", version="1.0")

    def test_update_subdir_and_tag_pattern(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import update_plugin_entry

        yml_path = _write_marketplace_yml(tmp_path, _VALID_MARKETPLACE)
        update_plugin_entry(
            yml_path,
            "existing-pkg",
            subdir="packages/sub",
            tag_pattern="v{version}",
            include_prerelease=True,
            tags=["new-tag"],
        )
        content = yml_path.read_text()
        assert "subdir" in content
        assert "tag_pattern" in content

    def test_update_no_packages_raises(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import (
            MarketplaceYmlError,
            update_plugin_entry,
        )

        yml_path = _write_marketplace_yml(
            tmp_path,
            "name: test-mp\ndescription: Test\nversion: '1.0.0'\nowner:\n  name: test-org\n",
        )
        with pytest.raises(MarketplaceYmlError, match=r"not found"):
            update_plugin_entry(yml_path, "anything", version="1.0")


class TestRemovePluginEntry:
    """Test remove_plugin_entry public API."""

    def test_remove_existing(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import remove_plugin_entry

        yml_path = _write_marketplace_yml(tmp_path, _VALID_MARKETPLACE)
        remove_plugin_entry(yml_path, "existing-pkg")
        content = yml_path.read_text()
        assert "existing-pkg" not in content

    def test_remove_case_insensitive(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import remove_plugin_entry

        yml_path = _write_marketplace_yml(tmp_path, _VALID_MARKETPLACE)
        remove_plugin_entry(yml_path, "EXISTING-PKG")
        content = yml_path.read_text()
        assert "existing-pkg" not in content

    def test_remove_not_found_raises(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import (
            MarketplaceYmlError,
            remove_plugin_entry,
        )

        yml_path = _write_marketplace_yml(tmp_path, _VALID_MARKETPLACE)
        with pytest.raises(MarketplaceYmlError, match=r"not found"):
            remove_plugin_entry(yml_path, "missing-pkg")

    def test_remove_no_packages_raises(self, tmp_path: Path) -> None:
        from apm_cli.marketplace.yml_editor import (
            MarketplaceYmlError,
            remove_plugin_entry,
        )

        yml_path = _write_marketplace_yml(
            tmp_path,
            "name: test-mp\ndescription: Test\nversion: '1.0.0'\nowner:\n  name: test-org\n",
        )
        with pytest.raises(MarketplaceYmlError, match=r"not found"):
            remove_plugin_entry(yml_path, "anything")


# ===================================================================
# bundle/unpacker.py -- tar extraction and lockfile verification
# ===================================================================


def _create_bundle_tarball(
    tmp_path: Path,
    *,
    files: dict[str, str] | None = None,
    lockfile_content: str | None = None,
) -> Path:
    """Build a minimal .tar.gz bundle for testing."""
    bundle_dir = tmp_path / "bundle-source"
    inner_dir = bundle_dir / "my-bundle"
    inner_dir.mkdir(parents=True)

    if lockfile_content is None:
        lockfile_content = (
            "version: 1\n"
            "dependencies:\n"
            "  - name: test-pkg\n"
            "    repo_url: https://github.com/a/b\n"
            "    resolved_ref: main\n"
            "    resolved_commit: abc1234\n"
            "    deployed_files:\n"
            "      - test-file.md\n"
        )
    (inner_dir / "apm.lock.yaml").write_text(lockfile_content, encoding="utf-8")

    if files:
        for fname, content in files.items():
            fpath = inner_dir / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")

    tar_path = tmp_path / "test-bundle.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(inner_dir, arcname="my-bundle")

    return tar_path


class TestUnpackResult:
    """Test UnpackResult dataclass."""

    def test_defaults(self) -> None:
        from apm_cli.bundle.unpacker import UnpackResult

        result = UnpackResult(extracted_dir=Path("/tmp/test"))
        assert result.files == []
        assert result.verified is False
        assert result.skipped_count == 0
        assert result.security_warnings == 0
        assert result.security_critical == 0
        assert result.pack_meta == {}


class TestUnpackBundle:
    """Test unpack_bundle function."""

    def test_unpack_from_directory(self, tmp_path: Path) -> None:
        from apm_cli.bundle.unpacker import unpack_bundle

        bundle_dir = tmp_path / "my-bundle"
        bundle_dir.mkdir()
        lockfile = (
            "version: 1\n"
            "dependencies:\n"
            "  - name: pkg\n"
            "    repo_url: https://github.com/a/b\n"
            "    resolved_ref: main\n"
            "    resolved_commit: abc1234\n"
            "    deployed_files:\n"
            "      - hello.md\n"
        )
        (bundle_dir / "apm.lock.yaml").write_text(lockfile, encoding="utf-8")
        (bundle_dir / "hello.md").write_text("# Hello\n", encoding="utf-8")

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        result = unpack_bundle(bundle_dir, output_dir=output_dir, skip_verify=True)
        assert result.extracted_dir == bundle_dir

    def test_unpack_from_tarball(self, tmp_path: Path) -> None:
        from apm_cli.bundle.unpacker import unpack_bundle

        tar_path = _create_bundle_tarball(
            tmp_path,
            files={"test-file.md": "# Test\n"},
        )
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        result = unpack_bundle(tar_path, output_dir=output_dir, skip_verify=True)
        assert result.extracted_dir.exists()

    def test_unpack_dry_run(self, tmp_path: Path) -> None:
        from apm_cli.bundle.unpacker import unpack_bundle

        tar_path = _create_bundle_tarball(
            tmp_path,
            files={"test-file.md": "# Test\n"},
        )
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        result = unpack_bundle(tar_path, output_dir=output_dir, skip_verify=True, dry_run=True)
        assert result.extracted_dir.exists()

    def test_unpack_missing_bundle_raises(self, tmp_path: Path) -> None:
        from apm_cli.bundle.unpacker import unpack_bundle

        with pytest.raises(FileNotFoundError):
            unpack_bundle(tmp_path / "nonexistent.tar.gz")

    def test_unpack_with_path_traversal_tarball(self, tmp_path: Path) -> None:
        """Tar entries with absolute paths are rejected."""
        from apm_cli.bundle.unpacker import unpack_bundle

        tar_path = tmp_path / "evil.tar.gz"
        inner_dir = tmp_path / "evil-source"
        inner_dir.mkdir()
        (inner_dir / "safe.txt").write_text("safe", encoding="utf-8")

        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(inner_dir / "safe.txt", arcname="/etc/passwd")

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        with pytest.raises((ValueError, FileNotFoundError)):
            unpack_bundle(tar_path, output_dir=output_dir)

    def test_unpack_with_symlink_tarball(self, tmp_path: Path) -> None:
        """Tar entries with symlinks are rejected."""
        from apm_cli.bundle.unpacker import unpack_bundle

        tar_path = tmp_path / "symlink.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            info = tarfile.TarInfo(name="link.txt")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        with pytest.raises(ValueError, match=r"Symlinks and hard links are not supported"):
            unpack_bundle(tar_path, output_dir=output_dir)

    def test_unpack_legacy_lockfile_name(self, tmp_path: Path) -> None:
        """Bundle with legacy apm.lock is supported."""
        from apm_cli.bundle.unpacker import unpack_bundle

        bundle_dir = tmp_path / "legacy-bundle"
        bundle_dir.mkdir()
        lockfile = (
            "version: 1\n"
            "dependencies:\n"
            "  - name: pkg\n"
            "    repo_url: https://github.com/a/b\n"
            "    resolved_ref: main\n"
            "    resolved_commit: abc1234\n"
            "    deployed_files:\n"
            "      - hello.md\n"
        )
        (bundle_dir / "apm.lock").write_text(lockfile, encoding="utf-8")
        (bundle_dir / "hello.md").write_text("# Hello\n", encoding="utf-8")

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        result = unpack_bundle(bundle_dir, output_dir=output_dir, skip_verify=True)
        assert result.extracted_dir == bundle_dir
