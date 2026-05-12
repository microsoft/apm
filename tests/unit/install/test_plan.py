"""Unit tests for ``apm_cli.install.plan``.

Covers ``build_update_plan``, ``render_plan_text``, and
``lockfile_satisfies_manifest``.  The plan module is pure -- no I/O,
no network -- so all tests use in-memory fixtures.

Issue: https://github.com/microsoft/apm/issues/1203
"""

from __future__ import annotations

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.plan import (
    PlanEntry,
    UpdatePlan,
    build_update_plan,
    lockfile_satisfies_manifest,
    render_plan_text,
)
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference


def _new_lockfile() -> LockFile:
    return LockFile(
        lockfile_version="1",
        generated_at="2025-01-01T00:00:00+00:00",
        apm_version="0.0.0-test",
    )


def _locked(
    repo_url: str, ref: str, commit: str, files: list[str] | None = None
) -> LockedDependency:
    return LockedDependency(
        repo_url=repo_url,
        resolved_ref=ref,
        resolved_commit=commit,
        depth=1,
        deployed_files=files or [],
    )


def _resolved_dep(repo_url: str, ref: str, commit: str | None) -> DependencyReference:
    """Construct a DependencyReference with resolved_reference populated."""
    dep = DependencyReference(repo_url=repo_url, reference=ref)
    dep.resolved_reference = ResolvedReference(
        original_ref=ref,
        ref_type=GitReferenceType.BRANCH,
        ref_name=ref,
        resolved_commit=commit,
    )
    return dep


# -----------------------------------------------------------------------------
# build_update_plan
# -----------------------------------------------------------------------------


class TestBuildUpdatePlan:
    def test_unchanged_dep_when_ref_and_commit_match(self):
        lock = _new_lockfile()
        lock.add_dependency(_locked("https://github.com/o/r", "main", "a" * 40))
        deps = [_resolved_dep("https://github.com/o/r", "main", "a" * 40)]

        plan = build_update_plan(lock, deps)

        assert plan.has_changes is False
        assert len(plan.entries) == 1
        assert plan.entries[0].action == "unchanged"

    def test_update_when_commit_advances(self):
        lock = _new_lockfile()
        lock.add_dependency(
            _locked("https://github.com/o/r", "main", "a" * 40, [".github/skills/x/SKILL.md"])
        )
        deps = [_resolved_dep("https://github.com/o/r", "main", "b" * 40)]

        plan = build_update_plan(lock, deps)

        assert plan.has_changes is True
        entry = plan.entries[0]
        assert entry.action == "update"
        assert entry.old_resolved_commit == "a" * 40
        assert entry.new_resolved_commit == "b" * 40
        assert entry.deployed_files == (".github/skills/x/SKILL.md",)

    def test_add_when_dep_not_in_lockfile(self):
        lock = _new_lockfile()
        deps = [_resolved_dep("https://github.com/new/r", "main", "c" * 40)]

        plan = build_update_plan(lock, deps)

        assert plan.has_changes is True
        assert plan.entries[0].action == "add"
        assert plan.entries[0].new_resolved_commit == "c" * 40

    def test_remove_when_locked_but_not_in_resolved(self):
        lock = _new_lockfile()
        lock.add_dependency(_locked("https://github.com/old/r", "main", "d" * 40))

        plan = build_update_plan(lock, [])

        assert plan.has_changes is True
        assert plan.entries[0].action == "remove"
        assert plan.entries[0].old_resolved_commit == "d" * 40

    def test_summary_counts_aggregate_correctly(self):
        lock = _new_lockfile()
        lock.add_dependency(_locked("https://github.com/u/r", "main", "a" * 40))
        lock.add_dependency(_locked("https://github.com/r/r", "main", "b" * 40))
        deps = [
            _resolved_dep("https://github.com/u/r", "main", "z" * 40),  # update
            _resolved_dep("https://github.com/n/r", "main", "n" * 40),  # add
        ]

        plan = build_update_plan(lock, deps)

        counts = plan.summary_counts
        assert counts["update"] == 1
        assert counts["add"] == 1
        assert counts["remove"] == 1

    def test_no_lockfile_returns_all_adds(self):
        deps = [
            _resolved_dep("https://github.com/a/r", "main", "1" * 40),
            _resolved_dep("https://github.com/b/r", "main", "2" * 40),
        ]

        plan = build_update_plan(None, deps)

        assert all(e.action == "add" for e in plan.entries)
        assert plan.has_changes is True

    def test_self_entry_ignored(self):
        """The lockfile self-entry must not appear as a remove."""
        lock = _new_lockfile()
        lock.local_deployed_files = [".github/instructions/x.md"]

        plan = build_update_plan(lock, [])

        assert plan.entries == ()


# -----------------------------------------------------------------------------
# render_plan_text
# -----------------------------------------------------------------------------


class TestRenderPlanText:
    def test_empty_plan_returns_empty_string(self):
        assert render_plan_text(UpdatePlan(entries=())) == ""

    def test_unchanged_only_returns_empty_when_not_verbose(self):
        plan = UpdatePlan(
            entries=(
                PlanEntry(
                    dep_key="o/r",
                    action="unchanged",
                    display_name="o/r",
                    old_resolved_ref="main",
                    new_resolved_ref="main",
                    old_resolved_commit="a" * 40,
                    new_resolved_commit="a" * 40,
                ),
            )
        )

        assert render_plan_text(plan) == ""

    def test_update_entry_includes_ref_transition_and_files(self):
        plan = UpdatePlan(
            entries=(
                PlanEntry(
                    dep_key="o/r",
                    action="update",
                    display_name="o/r",
                    old_resolved_ref="main",
                    new_resolved_ref="main",
                    old_resolved_commit="a" * 40,
                    new_resolved_commit="b" * 40,
                    deployed_files=(".github/skills/x/SKILL.md",),
                ),
            )
        )

        text = render_plan_text(plan)

        assert "[~]" in text  # update symbol
        assert "o/r" in text
        assert "aaaaaaa" in text  # short old commit
        assert "bbbbbbb" in text  # short new commit
        assert "SKILL.md" in text
        assert "1 updated" in text  # summary line

    def test_only_ascii_in_rendered_output(self):
        """Encoding rule: printable ASCII only (Windows cp1252 safe)."""
        plan = UpdatePlan(
            entries=(
                PlanEntry(
                    dep_key="o/r",
                    action="add",
                    display_name="o/r",
                    new_resolved_ref="main",
                    new_resolved_commit="c" * 40,
                ),
            )
        )

        text = render_plan_text(plan)

        for ch in text:
            assert ord(ch) <= 0x7E or ch in ("\n", "\r"), f"Non-ASCII char: {ch!r}"

    def test_verbose_includes_unchanged_count(self):
        plan = UpdatePlan(
            entries=(
                PlanEntry(
                    dep_key="o/r",
                    action="unchanged",
                    display_name="o/r",
                    old_resolved_ref="main",
                    new_resolved_ref="main",
                    old_resolved_commit="a" * 40,
                    new_resolved_commit="a" * 40,
                ),
                PlanEntry(
                    dep_key="o/s",
                    action="update",
                    display_name="o/s",
                    old_resolved_commit="b" * 40,
                    new_resolved_commit="c" * 40,
                ),
            )
        )

        text = render_plan_text(plan, verbose=True)

        assert "[=]" in text
        assert "1 unchanged" in text


# -----------------------------------------------------------------------------
# lockfile_satisfies_manifest
# -----------------------------------------------------------------------------


class TestLockfileSatisfiesManifest:
    def test_satisfied_when_all_manifest_deps_locked(self):
        lock = _new_lockfile()
        lock.add_dependency(_locked("https://github.com/o/r", "main", "a" * 40))
        manifest = [_resolved_dep("https://github.com/o/r", "main", None)]

        ok, reasons = lockfile_satisfies_manifest(lock, manifest)

        assert ok is True
        assert reasons == []

    def test_unsatisfied_when_manifest_dep_missing_from_lock(self):
        lock = _new_lockfile()
        manifest = [_resolved_dep("https://github.com/missing/r", "main", None)]

        ok, reasons = lockfile_satisfies_manifest(lock, manifest)

        assert ok is False
        assert len(reasons) == 1
        assert "missing" in reasons[0]

    def test_local_deps_skipped(self):
        """Local file deps have no remote ref, so they're skipped."""
        lock = _new_lockfile()
        local = DependencyReference(repo_url="local", local_path="./vendor/pkg")
        local.is_local = True

        ok, reasons = lockfile_satisfies_manifest(lock, [local])

        assert ok is True
        assert reasons == []

    def test_orphan_lockfile_entries_do_not_fail_check(self):
        """Lock has extra entries not in manifest -- structural check passes."""
        lock = _new_lockfile()
        lock.add_dependency(_locked("https://github.com/o/r", "main", "a" * 40))
        lock.add_dependency(_locked("https://github.com/orphan/r", "main", "b" * 40))
        manifest = [_resolved_dep("https://github.com/o/r", "main", None)]

        ok, reasons = lockfile_satisfies_manifest(lock, manifest)

        assert ok is True
        assert reasons == []
