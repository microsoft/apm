"""Regression traps for target-scoped lockfile manifest reconciliation.

These tests pin the fix for issue #1716: a multi-target deploy must not
leave the committed lockfile manifest single-target. On-disk stale cleanup
is target-scoped (it preserves files belonging to other targets), so the
manifest reconciliation in ``LockfileBuilder._attach_deployed_files`` must
be symmetric -- it must UNION across targets rather than REPLACE with the
current install's target only. Otherwise files deployed by a prior target
(e.g. ``.agents/skills/<s>/SKILL.md`` from the ``copilot`` target) remain on
disk but vanish from the manifest, escaping every manifest-driven audit
gate (deployed-files-present, content-integrity, drift).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apm_cli.core.deployment_state import (
    DeploymentLedger,
    DeploymentLocator,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.phases.lockfile import LockfileBuilder
from apm_cli.integration.cleanup import CleanupResult
from apm_cli.utils.content_hash import compute_file_hash


def _target(name, root_dir=None, deploy_roots=None):
    """Build a minimal target-profile stand-in for governance computation."""
    primitives = {}
    for idx, droot in enumerate(deploy_roots or []):
        primitives[f"prim{idx}"] = SimpleNamespace(deploy_root=droot)
    return SimpleNamespace(name=name, root_dir=root_dir, primitives=primitives)


def _ctx(*, package_deployed_files, existing_lockfile, targets, project_root):
    return SimpleNamespace(
        package_deployed_files=package_deployed_files,
        existing_lockfile=existing_lockfile,
        targets=targets,
        project_root=project_root,
    )


class TestAttachDeployedFilesUnion:
    def test_preserves_other_target_entry_when_dep_absent_from_current_install(self, tmp_path):
        """copilot install records .agents/skills; a later copilot-app install
        (which records nothing for this skill-only dep) must NOT erase them."""
        key = "owner/pkg"
        prior = LockFile()
        prior.add_dependency(
            LockedDependency(
                repo_url=key,
                deployed_files=[".agents/skills/demo/SKILL.md", ".github/agents/demo.md"],
                deployed_file_hashes={
                    ".agents/skills/demo/SKILL.md": "sha256:aaa",
                    ".github/agents/demo.md": "sha256:bbb",
                },
            )
        )
        new = LockFile()
        new.add_dependency(LockedDependency(repo_url=key))

        ctx = _ctx(
            package_deployed_files={},  # copilot-app records nothing for this dep
            existing_lockfile=prior,
            targets=[_target("copilot-app")],
            project_root=tmp_path,
        )
        LockfileBuilder(ctx)._attach_deployed_files(new)

        dep = new.get_dependency(key)
        assert ".agents/skills/demo/SKILL.md" in dep.deployed_files
        assert dep.deployed_file_hashes[".agents/skills/demo/SKILL.md"] == "sha256:aaa"

    def test_replaces_current_target_entries_but_unions_other_target(self, tmp_path):
        """A copilot-app install replaces its own URI rows yet preserves the
        file-based copilot entries from the prior install."""
        key = "owner/pkg"
        prior = LockFile()
        prior.add_dependency(
            LockedDependency(
                repo_url=key,
                deployed_files=[
                    ".agents/skills/demo/SKILL.md",
                    "copilot-app-db://workflows/old-id",
                ],
                deployed_file_hashes={".agents/skills/demo/SKILL.md": "sha256:aaa"},
            )
        )
        new = LockFile()
        new.add_dependency(LockedDependency(repo_url=key))

        ctx = _ctx(
            package_deployed_files={key: ["copilot-app-db://workflows/new-id"]},
            existing_lockfile=prior,
            targets=[_target("copilot-app")],
            project_root=tmp_path,
        )
        LockfileBuilder(ctx)._attach_deployed_files(new)

        dep = new.get_dependency(key)
        # The native URI adapter did not delete the old URI, so its prior
        # provenance remains until a later successful cleanup can prove removal.
        assert "copilot-app-db://workflows/new-id" in dep.deployed_files
        assert "copilot-app-db://workflows/old-id" in dep.deployed_files
        # other-target file rows preserved
        assert ".agents/skills/demo/SKILL.md" in dep.deployed_files

    def test_orphan_cleanup_refusal_retains_existing_owner(self):
        """An orphan retained on disk must keep its prior lockfile owner."""
        key = "owner/pkg"
        path = ".agents/skills/alpha/SKILL.md"
        prior = LockFile()
        prior.add_dependency(
            LockedDependency(
                repo_url=key,
                deployed_files=[path],
                deployed_file_hashes={path: "sha256:original"},
            )
        )
        lockfile = LockFile()
        ctx = SimpleNamespace(
            existing_lockfile=prior,
            only_packages=None,
            intended_dep_keys=set(),
            update_refs=False,
            orphan_cleanup_retained={key: {path: "sha256:original"}},
        )

        LockfileBuilder(ctx)._merge_existing(lockfile)

        retained = lockfile.get_dependency(key)
        assert retained is not None
        assert retained.deployed_files == [path]
        assert retained.deployed_file_hashes == {path: "sha256:original"}

    def test_file_target_reinstall_drops_stale_in_target_files(self, tmp_path):
        """A same-target reinstall must still drop files removed from the
        package (no false preservation within the governed roots)."""
        key = "owner/pkg"
        (tmp_path / ".github").mkdir()
        kept = tmp_path / ".github" / "kept.md"
        kept.write_text("x", encoding="utf-8")
        prior = LockFile()
        prior.add_dependency(
            LockedDependency(
                repo_url=key,
                deployed_files=[".github/kept.md", ".github/removed.md"],
            )
        )
        new = LockFile()
        new.add_dependency(LockedDependency(repo_url=key))

        ctx = _ctx(
            package_deployed_files={key: [".github/kept.md"]},
            existing_lockfile=prior,
            targets=[_target("copilot", root_dir=".github", deploy_roots=[".agents"])],
            project_root=tmp_path,
        )
        LockfileBuilder(ctx)._attach_deployed_files(new)

        dep = new.get_dependency(key)
        assert ".github/kept.md" in dep.deployed_files
        assert ".github/removed.md" not in dep.deployed_files


class TestCurrentInstallGovernance:
    def test_file_target_includes_root_and_primitive_deploy_roots(self, tmp_path):
        from apm_cli.install.manifest_reconcile import install_governance

        targets = [_target("copilot", root_dir=".github", deploy_roots=[".agents"])]
        file_prefixes, uri_schemes = install_governance(targets)
        assert ".github/" in file_prefixes
        assert ".agents/" in file_prefixes
        assert uri_schemes == set()

    def test_shared_agents_root_is_partitioned_by_primitive_subdirectory(self):
        from apm_cli.install.manifest_reconcile import install_governance

        file_prefixes, _ = install_governance([_known("copilot")])

        assert ".agents/skills/" in file_prefixes
        assert ".agents/" not in file_prefixes

    def test_shared_root_filename_governance_requires_exact_match(self):
        from apm_cli.install.manifest_reconcile import union_preserving

        lookalike = ".agents/hooks.json.bak"
        files, _ = union_preserving(
            current_files=[],
            current_hashes={},
            prior_files=[lookalike],
            prior_hashes={},
            targets=[_known("antigravity")],
            declared_targets=[_known("antigravity")],
        )

        assert lookalike in files

    def test_copilot_app_target_uses_uri_scheme(self, tmp_path):
        from apm_cli.install.manifest_reconcile import install_governance

        _file_roots, uri_schemes = install_governance([_target("copilot-app")])
        assert any(s.startswith("copilot-app-db://") for s in uri_schemes)

    def test_generic_agents_rows_contract_from_current_skill_claims(self, tmp_path):
        """Current claims supersede legacy generic rows and remove stale twins."""
        from apm_cli.install.manifest_reconcile import reconcile_deployed_block
        from apm_cli.utils.diagnostics import DiagnosticCollector

        shared = ".agents/skills/shared/SKILL.md"
        beta_only = ".agents/skills/beta-only/SKILL.md"
        alpha_only = ".agents/skills/alpha-only/SKILL.md"
        for path, content in (
            (shared, "shared"),
            (beta_only, "beta"),
            (alpha_only, "alpha"),
        ):
            target = tmp_path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        prior_hashes = {
            path: compute_file_hash(tmp_path / path) for path in (shared, beta_only, alpha_only)
        }
        generic_records = {}
        for path, owner in ((shared, "beta"), (beta_only, "beta"), (alpha_only, "alpha")):
            locator = DeploymentLocator(
                kind=LocatorKind.PROJECT_RELATIVE,
                target="agents",
                value=path,
                runtime=None,
                scope="project",
            )
            generic_records[locator.key] = DeploymentRecord(
                locator=locator,
                owners=(owner,),
                active_owner=owner,
                content_hash=prior_hashes[path],
            )

        files, hashes = reconcile_deployed_block(
            project_root=tmp_path,
            dep_key="owner/pkg",
            current_files=[shared, beta_only],
            current_hashes={shared: "sha256:shared-current", beta_only: "sha256:beta-current"},
            prior_files=[shared, beta_only, alpha_only],
            prior_hashes=prior_hashes,
            active_targets=[_known("cursor")],
            declared_targets=[_known("cursor")],
            diagnostics=DiagnosticCollector(),
            prior_ledger=DeploymentLedger(records=generic_records),
        )

        assert files == [shared, beta_only]
        assert hashes == {shared: "sha256:shared-current", beta_only: "sha256:beta-current"}
        assert not (tmp_path / alpha_only).exists()

    def test_user_edited_generic_agents_row_remains_tracked(self, tmp_path):
        """A generic row survives contraction when cleanup cannot prove ownership."""
        from apm_cli.install.manifest_reconcile import reconcile_deployed_block
        from apm_cli.utils.diagnostics import DiagnosticCollector

        alpha_only = ".agents/skills/alpha-only/SKILL.md"
        alpha_path = tmp_path / alpha_only
        alpha_path.parent.mkdir(parents=True)
        alpha_path.write_text("user edit", encoding="utf-8")
        locator = DeploymentLocator(
            kind=LocatorKind.PROJECT_RELATIVE,
            target="agents",
            value=alpha_only,
            runtime=None,
            scope="project",
        )
        prior_hash = "sha256:original"
        ledger = DeploymentLedger(
            records={
                locator.key: DeploymentRecord(
                    locator=locator,
                    owners=("alpha",),
                    active_owner="alpha",
                    content_hash=prior_hash,
                )
            }
        )

        files, hashes = reconcile_deployed_block(
            project_root=tmp_path,
            dep_key="owner/pkg",
            current_files=[],
            current_hashes={},
            prior_files=[alpha_only],
            prior_hashes={alpha_only: prior_hash},
            active_targets=[_known("cursor")],
            declared_targets=[_known("cursor")],
            diagnostics=DiagnosticCollector(),
            prior_ledger=ledger,
        )

        assert files == [alpha_only]
        assert hashes == {alpha_only: prior_hash}
        assert alpha_path.read_text(encoding="utf-8") == "user edit"

    def test_generic_agents_row_is_preserved_when_current_run_failed(self, tmp_path):
        """An integration error is not permission to contract an existing row."""
        from apm_cli.install.manifest_reconcile import reconcile_deployed_block
        from apm_cli.utils.diagnostics import DiagnosticCollector

        alpha_only = ".agents/skills/alpha-only/SKILL.md"
        alpha_path = tmp_path / alpha_only
        alpha_path.parent.mkdir(parents=True)
        alpha_path.write_text("original", encoding="utf-8")
        original_hash = compute_file_hash(alpha_path)
        locator = DeploymentLocator(
            kind=LocatorKind.PROJECT_RELATIVE,
            target="agents",
            value=alpha_only,
            runtime=None,
            scope="project",
        )
        ledger = DeploymentLedger(
            records={
                locator.key: DeploymentRecord(
                    locator=locator,
                    owners=("alpha",),
                    active_owner="alpha",
                    content_hash=original_hash,
                )
            }
        )

        files, hashes = reconcile_deployed_block(
            project_root=tmp_path,
            dep_key="owner/pkg",
            current_files=[],
            current_hashes={},
            prior_files=[alpha_only],
            prior_hashes={alpha_only: original_hash},
            active_targets=[_known("cursor")],
            declared_targets=[_known("cursor")],
            diagnostics=DiagnosticCollector(),
            prior_ledger=ledger,
            current_run_trusted=False,
        )

        assert files == [alpha_only]
        assert hashes == {alpha_only: original_hash}
        assert alpha_path.read_text(encoding="utf-8") == "original"

    def test_cleanup_retention_never_rehashes_a_user_edited_path(self, tmp_path):
        """A cleanup refusal keeps its original provenance instead of adopting edits."""
        from apm_cli.install.manifest_reconcile import reconcile_deployed_block
        from apm_cli.utils.diagnostics import DiagnosticCollector

        alpha_only = ".agents/skills/alpha-only/SKILL.md"
        alpha_path = tmp_path / alpha_only
        alpha_path.parent.mkdir(parents=True)
        alpha_path.write_text("user edit", encoding="utf-8")
        locator = DeploymentLocator(
            kind=LocatorKind.PROJECT_RELATIVE,
            target="agents",
            value=alpha_only,
            runtime=None,
            scope="project",
        )
        original_hash = "sha256:original"
        ledger = DeploymentLedger(
            records={
                locator.key: DeploymentRecord(
                    locator=locator,
                    owners=("alpha",),
                    active_owner="alpha",
                    content_hash=original_hash,
                )
            }
        )

        files, hashes = reconcile_deployed_block(
            project_root=tmp_path,
            dep_key="owner/pkg",
            current_files=[alpha_only],
            current_hashes={alpha_only: "sha256:user-edit"},
            prior_files=[alpha_only],
            prior_hashes={alpha_only: original_hash},
            active_targets=[_known("cursor")],
            declared_targets=[_known("cursor")],
            diagnostics=DiagnosticCollector(),
            prior_ledger=ledger,
            cleanup_retained_hashes={alpha_only: original_hash},
        )

        assert files == [alpha_only]
        assert hashes == {alpha_only: original_hash}
        assert alpha_path.read_text(encoding="utf-8") == "user edit"

    @pytest.mark.parametrize(
        "cleanup_result",
        (
            CleanupResult(failed=["cowork://skills/alpha/SKILL.md"]),
            CleanupResult(skipped_user_edit=["cowork://skills/alpha/SKILL.md"]),
            CleanupResult(skipped_unmanaged=["cowork://skills/alpha/SKILL.md"]),
        ),
    )
    def test_uri_cleanup_retains_its_existing_provenance(self, tmp_path, cleanup_result):
        """Every URI cleanup refusal remains auditable for a later retry."""
        from apm_cli.install.manifest_reconcile import reconcile_deployed_block
        from apm_cli.utils.diagnostics import DiagnosticCollector

        uri = "cowork://skills/alpha/SKILL.md"
        locator = DeploymentLocator(
            kind=LocatorKind.URI,
            target="copilot-cowork",
            value=uri,
            runtime=None,
            scope="project",
        )
        original_hash = "sha256:original"
        ledger = DeploymentLedger(
            records={
                locator.key: DeploymentRecord(
                    locator=locator,
                    owners=("alpha",),
                    active_owner="alpha",
                    content_hash=original_hash,
                )
            }
        )

        with patch(
            "apm_cli.integration.cleanup.remove_stale_deployed_files",
            return_value=cleanup_result,
        ):
            files, hashes = reconcile_deployed_block(
                project_root=tmp_path,
                dep_key="owner/pkg",
                current_files=[],
                current_hashes={},
                prior_files=[uri],
                prior_hashes={uri: original_hash},
                active_targets=[_known("cursor")],
                declared_targets=[_known("cursor")],
                diagnostics=DiagnosticCollector(),
                prior_ledger=ledger,
            )

        assert files == [uri]
        assert hashes == {uri: original_hash}


class TestLocalDeployedFilesUnion:
    def test_copilot_app_install_preserves_prior_copilot_local_files(self):
        """The project-root local_deployed_files block must also union: a
        copilot-app install (no project file deployment) must NOT erase the
        .agents/.github files a prior copilot install recorded -- the bug that
        wiped content-integrity coverage entirely (issue #1716)."""
        from apm_cli.install.manifest_reconcile import union_preserving

        prior_files = [".agents/skills/demo/SKILL.md", ".github/agents/demo.md"]
        prior_hashes = {
            ".agents/skills/demo/SKILL.md": "sha256:aaa",
            ".github/agents/demo.md": "sha256:bbb",
        }
        files, hashes = union_preserving(
            current_files=[],  # copilot-app deploys no project files
            current_hashes={},
            prior_files=prior_files,
            prior_hashes=prior_hashes,
            targets=[_target("copilot-app")],
        )
        assert ".agents/skills/demo/SKILL.md" in files
        assert hashes[".agents/skills/demo/SKILL.md"] == "sha256:aaa"

    def test_same_target_reinstall_drops_removed_local_file(self):
        from apm_cli.install.manifest_reconcile import union_preserving

        files, _ = union_preserving(
            current_files=[".agents/skills/demo/SKILL.md"],
            current_hashes={".agents/skills/demo/SKILL.md": "sha256:new"},
            prior_files=[".agents/skills/demo/SKILL.md", ".agents/skills/gone/SKILL.md"],
            prior_hashes={},
            targets=[_target("copilot", root_dir=".github", deploy_roots=[".agents"])],
        )
        assert ".agents/skills/demo/SKILL.md" in files
        assert ".agents/skills/gone/SKILL.md" not in files


# Consumer of issue #2059: declares five targets, none of them windsurf.
_CONSUMER_5 = ("claude", "codex", "copilot", "cursor", "gemini")


def _known(name):
    from apm_cli.integration.targets import KNOWN_TARGETS

    return KNOWN_TARGETS[name]


class TestInactiveTargetGhostDrop:
    """Issue #2059: a prior ``deployed_files`` entry for a target the consumer
    never DECLARES (e.g. a dependency's package-declared ``windsurf`` skill
    paths) must be dropped rather than re-preserved forever. Otherwise it never
    exists on disk yet lingers in the lock, failing ``deployed-files-present``
    permanently on fresh checkouts."""

    def test_union_drops_ghost_when_declared_universe_known(self):
        from apm_cli.install.manifest_reconcile import union_preserving

        declared = [_known(n) for n in _CONSUMER_5]  # no windsurf
        current = [".agents/skills/az/foo/SKILL.md", ".claude/skills/az/foo/SKILL.md"]
        ghost = ".windsurf/skills/az/foo/SKILL.md"
        files, hashes = union_preserving(
            current_files=current,
            current_hashes={p: "sha256:new" for p in current},
            prior_files=[*current, ghost],
            prior_hashes={ghost: "sha256:ghost"},
            targets=declared,
            declared_targets=declared,
        )
        assert ghost not in files
        assert ghost not in hashes
        assert set(current).issubset(set(files))

    def test_union_preserves_declared_but_narrowed_target(self):
        """--target narrows this run to copilot, but claude IS declared: keep
        claude's prior file (legit sibling target) yet still drop the windsurf
        ghost (never declared)."""
        from apm_cli.install.manifest_reconcile import union_preserving

        declared = [_known(n) for n in _CONSUMER_5]
        claude_file = ".claude/skills/az/foo/SKILL.md"
        ghost = ".windsurf/skills/az/foo/SKILL.md"
        files, _ = union_preserving(
            current_files=[".agents/skills/az/foo/SKILL.md"],
            current_hashes={},
            prior_files=[".agents/skills/az/foo/SKILL.md", claude_file, ghost],
            prior_hashes={},
            targets=[_known("copilot")],
            declared_targets=declared,
        )
        assert claude_file in files
        assert ghost not in files

    def test_union_preserves_declared_sibling_under_shared_agents_root(self):
        """Copilot owns .agents/skills, not Antigravity's .agents/rules."""
        from apm_cli.install.manifest_reconcile import union_preserving

        rule = ".agents/rules/keep.md"
        files, hashes = union_preserving(
            current_files=[".agents/skills/demo/SKILL.md"],
            current_hashes={},
            prior_files=[rule],
            prior_hashes={rule: "sha256:rule"},
            targets=[_known("copilot")],
            declared_targets=[_known("copilot"), _known("antigravity")],
        )

        assert rule in files
        assert hashes[rule] == "sha256:rule"

    def test_union_drops_undeclared_target_under_shared_agents_root(self):
        from apm_cli.install.manifest_reconcile import union_preserving

        ghost = ".agents/rules/ghost.md"
        files, _ = union_preserving(
            current_files=[".agents/skills/demo/SKILL.md"],
            current_hashes={},
            prior_files=[ghost],
            prior_hashes={},
            targets=[_known("copilot")],
            declared_targets=[_known("copilot")],
        )

        assert ghost not in files

    def test_exact_shared_root_filename_tracks_declared_sibling(self):
        from apm_cli.install.manifest_reconcile import union_preserving

        hooks = ".agents/hooks.json"
        active = [_known("copilot")]
        preserved, _ = union_preserving(
            current_files=[],
            current_hashes={},
            prior_files=[hooks],
            prior_hashes={},
            targets=active,
            declared_targets=[*active, _known("antigravity")],
        )
        dropped, _ = union_preserving(
            current_files=[],
            current_hashes={},
            prior_files=[hooks],
            prior_hashes={},
            targets=active,
            declared_targets=active,
        )

        assert preserved == [hooks]
        assert dropped == []

    def test_union_declared_none_preserves_all_legacy(self):
        """No declared universe (auto-detect / --target-only consumer) keeps the
        legacy preserve-all behaviour so a genuine multi-target deploy is never
        clobbered (issue #1716)."""
        from apm_cli.install.manifest_reconcile import union_preserving

        # Active target is copilot-app (URI scheme; governs no file root), so
        # under legacy preserve-all BOTH prior file rows survive.
        prior = [".windsurf/skills/x/SKILL.md", ".agents/skills/demo/SKILL.md"]
        files, _ = union_preserving(
            current_files=[],
            current_hashes={},
            prior_files=prior,
            prior_hashes={},
            targets=[_known("copilot-app")],
            declared_targets=None,
        )
        assert set(prior).issubset(set(files))

    def test_union_declared_universe_still_preserves_1716_sibling(self):
        """The #1716 contract survives declared-gating: a copilot-app run keeps
        the prior copilot file rows when copilot is in the declared universe."""
        from apm_cli.install.manifest_reconcile import union_preserving

        declared = [_known("copilot"), _known("copilot-app")]
        prior = [".agents/skills/demo/SKILL.md", "copilot-app-db://workflows/old"]
        files, _ = union_preserving(
            current_files=["copilot-app-db://workflows/new"],
            current_hashes={},
            prior_files=prior,
            prior_hashes={},
            targets=[_known("copilot-app")],
            declared_targets=declared,
        )
        assert ".agents/skills/demo/SKILL.md" in files
        assert "copilot-app-db://workflows/new" in files
        assert "copilot-app-db://workflows/old" not in files

    def test_state_reconcile_without_declared_universe_preserves_sibling(self, tmp_path):
        """Command entrypoints retain legacy multi-target state without targets."""
        from apm_cli.install.manifest_reconcile import reconcile_deployed_state
        from apm_cli.utils.diagnostics import DiagnosticCollector

        sibling = ".windsurf/rules/keep.md"
        lockfile = LockFile()
        lockfile.add_dependency(
            LockedDependency(
                repo_url="owner/pkg",
                deployed_files=[sibling],
                deployed_file_hashes={sibling: "sha256:old"},
            )
        )

        changed = reconcile_deployed_state(
            project_root=tmp_path,
            lockfile=lockfile,
            active_targets=[_known("copilot")],
            declared_targets=None,
            diagnostics=DiagnosticCollector(),
        )

        assert not changed
        assert lockfile.get_dependency("owner/pkg").deployed_files == [sibling]

    def test_gated_dynamic_target_uri_never_treated_as_ghost(self, tmp_path):
        """A consumer that declares only canonical ``copilot`` but uses the
        gated ``copilot-app`` target (activated by flag/detection, NOT
        declarable in apm.yml) must keep its ``copilot-app-db://`` rows across a
        run that does not activate copilot-app -- while a windsurf ghost the
        consumer never uses is still dropped. Guards against the declared
        universe being *only* apm.yml canonical targets, which would regress the
        #1716 copilot-app preservation contract."""
        from apm_cli.install.manifest_reconcile import union_preserving
        from apm_cli.install.phases.targets import declared_target_profiles

        (tmp_path / "apm.yml").write_text("targets:\n  - copilot\n", encoding="utf-8")
        ctx = SimpleNamespace(apm_package=SimpleNamespace(package_path=tmp_path), scope=None)
        declared = declared_target_profiles(ctx)
        uri = "copilot-app-db://workflows/old"
        ghost = ".windsurf/rules/ghost.md"
        files, _ = union_preserving(
            current_files=[".agents/skills/demo/SKILL.md"],
            current_hashes={},
            prior_files=[".agents/skills/demo/SKILL.md", uri, ghost],
            prior_hashes={},
            targets=[_known("copilot")],
            declared_targets=declared,
        )
        assert uri in files  # gated dynamic target row preserved
        assert ghost not in files  # canonical undeclared target ghost dropped

    def test_attach_deployed_files_drops_ghost_from_apm_yml_targets(self, tmp_path):
        """End-to-end wiring: ``_attach_deployed_files`` reads the consumer's
        declared targets from apm.yml and drops the windsurf ghost while keeping
        the active-target file."""
        (tmp_path / "apm.yml").write_text("targets:\n  - claude\n  - copilot\n", encoding="utf-8")
        key = "owner/pkg"
        ghost = ".windsurf/skills/demo/SKILL.md"
        active_file = ".agents/skills/demo/SKILL.md"
        prior = LockFile()
        prior.add_dependency(
            LockedDependency(
                repo_url=key,
                deployed_files=[active_file, ghost],
                deployed_file_hashes={active_file: "sha256:a", ghost: "sha256:g"},
            )
        )
        new = LockFile()
        new.add_dependency(LockedDependency(repo_url=key))

        ctx = SimpleNamespace(
            package_deployed_files={key: [active_file]},
            existing_lockfile=prior,
            targets=[_known("claude"), _known("copilot")],
            project_root=tmp_path,
            apm_package=SimpleNamespace(package_path=tmp_path),
            scope=None,
        )
        LockfileBuilder(ctx)._attach_deployed_files(new)

        dep = new.get_dependency(key)
        assert ghost not in dep.deployed_files
        assert ghost not in dep.deployed_file_hashes
        assert active_file in dep.deployed_files
