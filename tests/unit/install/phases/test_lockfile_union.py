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
