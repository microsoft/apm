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

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.phases.lockfile import LockfileBuilder


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
        # current-target URI row replaced
        assert "copilot-app-db://workflows/new-id" in dep.deployed_files
        assert "copilot-app-db://workflows/old-id" not in dep.deployed_files
        # other-target file rows preserved
        assert ".agents/skills/demo/SKILL.md" in dep.deployed_files

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
        file_roots, uri_schemes = install_governance(targets)
        assert ".github" in file_roots
        assert ".agents" in file_roots
        assert uri_schemes == set()

    def test_copilot_app_target_uses_uri_scheme(self, tmp_path):
        from apm_cli.install.manifest_reconcile import install_governance

        _file_roots, uri_schemes = install_governance([_target("copilot-app")])
        assert any(s.startswith("copilot-app-db://") for s in uri_schemes)


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
