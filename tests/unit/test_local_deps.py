"""Unit tests for local filesystem path dependency support."""

from pathlib import Path  # noqa: F401
from unittest.mock import Mock  # noqa: F401

import pytest
import yaml

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import APMPackage, DependencyReference

# ===========================================================================
# DependencyReference.is_local_path()
# ===========================================================================


class TestIsLocalPath:
    """Test local path detection logic."""

    def test_relative_dot_slash(self):
        assert DependencyReference.is_local_path("./my-package") is True

    def test_relative_dot_dot_slash(self):
        assert DependencyReference.is_local_path("../sibling-pkg") is True

    def test_absolute_unix(self):
        assert DependencyReference.is_local_path("/home/user/my-pkg") is True

    def test_home_tilde(self):
        assert DependencyReference.is_local_path("~/repos/my-pkg") is True

    def test_windows_relative(self):
        assert DependencyReference.is_local_path(".\\packages\\my-pkg") is True

    def test_windows_parent(self):
        assert DependencyReference.is_local_path("..\\sibling-pkg") is True

    def test_windows_home(self):
        assert DependencyReference.is_local_path("~\\repos\\my-pkg") is True

    def test_windows_absolute_backslash(self):
        assert DependencyReference.is_local_path("C:\\Users\\runner\\my-pkg") is True

    def test_windows_absolute_forward_slash(self):
        assert DependencyReference.is_local_path("D:/repos/my-pkg") is True

    def test_windows_absolute_uppercase(self):
        assert DependencyReference.is_local_path("Z:\\some\\path") is True

    def test_windows_absolute_lowercase(self):
        assert DependencyReference.is_local_path("c:\\users\\me\\pkg") is True

    def test_remote_shorthand_not_local(self):
        assert DependencyReference.is_local_path("owner/repo") is False

    def test_https_url_not_local(self):
        assert DependencyReference.is_local_path("https://github.com/owner/repo") is False

    def test_ssh_url_not_local(self):
        assert DependencyReference.is_local_path("git@github.com:owner/repo.git") is False

    def test_protocol_relative_not_local(self):
        """Protocol-relative URLs (//...) must NOT be treated as local paths."""
        assert DependencyReference.is_local_path("//evil.com/owner/repo") is False

    def test_bare_name_not_local(self):
        assert DependencyReference.is_local_path("my-package") is False

    def test_whitespace_trimmed(self):
        assert DependencyReference.is_local_path("  ./my-pkg  ") is True

    def test_empty_string_not_local(self):
        assert DependencyReference.is_local_path("") is False


# ===========================================================================
# DependencyReference.parse() with local paths
# ===========================================================================


class TestParseLocalPath:
    """Test parsing local filesystem paths into DependencyReference."""

    def test_relative_path(self):
        dep = DependencyReference.parse("./packages/my-skills")
        assert dep.is_local is True
        assert dep.local_path == "./packages/my-skills"
        assert dep.repo_url == "_local/my-skills"

    def test_relative_parent_path(self):
        dep = DependencyReference.parse("../sibling-package")
        assert dep.is_local is True
        assert dep.local_path == "../sibling-package"
        assert dep.repo_url == "_local/sibling-package"

    def test_absolute_path(self):
        dep = DependencyReference.parse("/home/user/repos/my-package")
        assert dep.is_local is True
        assert dep.local_path == "/home/user/repos/my-package"
        assert dep.repo_url == "_local/my-package"

    def test_home_path(self):
        dep = DependencyReference.parse("~/repos/my-ai-pkg")
        assert dep.is_local is True
        assert dep.local_path == "~/repos/my-ai-pkg"
        assert dep.repo_url == "_local/my-ai-pkg"

    def test_deeply_nested_relative(self):
        dep = DependencyReference.parse("./a/b/c/d/my-deep-pkg")
        assert dep.is_local is True
        assert dep.local_path == "./a/b/c/d/my-deep-pkg"
        assert dep.repo_url == "_local/my-deep-pkg"

    def test_no_reference_for_local(self):
        """Local paths should not have reference, alias, or virtual_path."""
        dep = DependencyReference.parse("./my-pkg")
        assert dep.reference is None
        assert dep.alias is None
        assert dep.virtual_path is None
        assert dep.is_virtual is False

    def test_remote_dep_not_local(self):
        """Regular remote deps should remain unaffected."""
        dep = DependencyReference.parse("microsoft/apm-sample-package")
        assert dep.is_local is False
        assert dep.local_path is None

    def test_bare_dot_dot_slash_rejected(self):
        """Path '../' has name '..' which could escape _local/ — must be rejected."""
        with pytest.raises(ValueError, match="does not resolve to a named directory"):
            DependencyReference.parse("../")

    def test_bare_dot_slash_rejected(self):
        """Path './' has empty name — must be rejected."""
        with pytest.raises(ValueError, match="does not resolve to a named directory"):
            DependencyReference.parse("./")

    def test_bare_root_rejected(self):
        """Path '/' has empty name — must be rejected."""
        with pytest.raises(ValueError, match="does not resolve to a named directory"):
            DependencyReference.parse("/")

    def test_dot_dot_without_slash_rejected(self):
        """Path '..' is not detected as a local path (no trailing '/')."""
        # '..' doesn't start with '../' so is_local_path returns False.
        # It falls through to regular parsing which also rejects it.
        with pytest.raises(ValueError):
            DependencyReference.parse("..")


# ===========================================================================
# DependencyReference methods for local deps
# ===========================================================================


class TestLocalDepMethods:
    """Test DependencyReference methods with local dependencies."""

    def test_to_canonical_returns_local_path(self):
        dep = DependencyReference.parse("./packages/my-skills")
        assert dep.to_canonical() == "./packages/my-skills"

    def test_get_identity_returns_local_path(self):
        dep = DependencyReference.parse("./packages/my-skills")
        assert dep.get_identity() == "./packages/my-skills"

    def test_get_unique_key_returns_local_path(self):
        dep = DependencyReference.parse("./packages/my-skills")
        assert dep.get_unique_key() == "./packages/my-skills"

    def test_get_install_path(self, tmp_path):
        dep = DependencyReference.parse("./packages/my-skills")
        install_path = dep.get_install_path(tmp_path / "apm_modules")
        assert install_path == tmp_path / "apm_modules" / "_local" / "my-skills"

    def test_get_display_name_returns_path(self):
        dep = DependencyReference.parse("./packages/my-skills")
        assert dep.get_display_name() == "./packages/my-skills"

    def test_str_returns_path(self):
        dep = DependencyReference.parse("./my-pkg")
        assert str(dep) == "./my-pkg"

    def test_install_path_no_conflict_with_remote(self, tmp_path):
        """Local and remote packages with same name should not conflict."""
        local_dep = DependencyReference.parse("./skills")
        remote_dep = DependencyReference.parse("owner/skills")
        apm_modules = tmp_path / "apm_modules"
        assert local_dep.get_install_path(apm_modules) != remote_dep.get_install_path(apm_modules)


# ===========================================================================
# LockedDependency with local source
# ===========================================================================


class TestLockedDependencyLocal:
    """Test LockedDependency serialization for local path dependencies."""

    def test_from_dependency_ref_local(self):
        dep_ref = DependencyReference.parse("./packages/my-skills")
        locked = LockedDependency.from_dependency_ref(dep_ref, None, 1, None)
        assert locked.source == "local"
        assert locked.local_path == "./packages/my-skills"
        assert locked.resolved_commit is None

    def test_from_dependency_ref_remote(self):
        dep_ref = DependencyReference.parse("owner/repo")
        locked = LockedDependency.from_dependency_ref(dep_ref, "abc123", 1, None)
        assert locked.source is None
        assert locked.local_path is None
        assert locked.resolved_commit == "abc123"

    def test_to_dict_includes_source(self):
        locked = LockedDependency(
            repo_url="_local/my-skills",
            source="local",
            local_path="./packages/my-skills",
        )
        d = locked.to_dict()
        assert d["source"] == "local"
        assert d["local_path"] == "./packages/my-skills"

    def test_to_dict_excludes_source_for_remote(self):
        locked = LockedDependency(repo_url="owner/repo", resolved_commit="abc123")
        d = locked.to_dict()
        assert "source" not in d
        assert "local_path" not in d

    def test_from_dict_with_source(self):
        data = {
            "repo_url": "_local/my-skills",
            "source": "local",
            "local_path": "./packages/my-skills",
        }
        locked = LockedDependency.from_dict(data)
        assert locked.source == "local"
        assert locked.local_path == "./packages/my-skills"

    def test_round_trip(self, tmp_path):
        """Write and read back a lockfile with local dependencies."""
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(
                repo_url="_local/my-skills",
                source="local",
                local_path="./packages/my-skills",
                deployed_files=[".github/instructions/my-skill.instructions.md"],
            )
        )
        lock.add_dependency(
            LockedDependency(
                repo_url="owner/remote-pkg",
                resolved_commit="abc123",
                deployed_files=[".github/instructions/remote.instructions.md"],
            )
        )

        lock_path = tmp_path / "apm.lock"
        lock.write(lock_path)
        loaded = LockFile.read(lock_path)

        assert loaded.has_dependency("./packages/my-skills")
        assert loaded.has_dependency("owner/remote-pkg")

        local_dep = loaded.get_dependency("./packages/my-skills")
        assert local_dep.source == "local"
        assert local_dep.local_path == "./packages/my-skills"

        remote_dep = loaded.get_dependency("owner/remote-pkg")
        assert remote_dep.source is None
        assert remote_dep.resolved_commit == "abc123"

    def test_get_unique_key_local(self):
        locked = LockedDependency(
            repo_url="_local/my-skills",
            source="local",
            local_path="./packages/my-skills",
        )
        assert locked.get_unique_key() == "./packages/my-skills"

    def test_get_unique_key_remote(self):
        locked = LockedDependency(repo_url="owner/repo")
        assert locked.get_unique_key() == "owner/repo"


# ===========================================================================
# APMPackage.from_apm_yml with local deps
# ===========================================================================


class TestAPMPackageLocalDeps:
    """Test APMPackage loading with local path dependencies in apm.yml."""

    def test_apm_yml_with_local_string_dep(self, tmp_path):
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": ["./packages/my-skills"],
                    },
                }
            )
        )
        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        assert deps[0].is_local is True
        assert deps[0].local_path == "./packages/my-skills"

    def test_apm_yml_with_local_dict_dep(self, tmp_path):
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": [{"path": "./packages/my-skills"}],
                    },
                }
            )
        )
        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 1
        assert deps[0].is_local is True
        assert deps[0].local_path == "./packages/my-skills"

    def test_mixed_local_and_remote_deps(self, tmp_path):
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": [
                            "microsoft/apm-sample-package",
                            "./packages/my-local-skills",
                            "/absolute/path/to/pkg",
                        ],
                    },
                }
            )
        )
        pkg = APMPackage.from_apm_yml(apm_yml)
        deps = pkg.get_apm_dependencies()
        assert len(deps) == 3
        assert deps[0].is_local is False
        assert deps[1].is_local is True
        assert deps[1].local_path == "./packages/my-local-skills"
        assert deps[2].is_local is True
        assert deps[2].local_path == "/absolute/path/to/pkg"

    def test_invalid_dict_path_rejected(self, tmp_path):
        """Dict-form paths that don't look like filesystem paths should be rejected."""
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": [{"path": "not-a-local-path"}],
                    },
                }
            )
        )
        with pytest.raises(ValueError, match="local filesystem path"):
            APMPackage.from_apm_yml(apm_yml)


# ===========================================================================
# Pack guard: reject local deps
# ===========================================================================


class TestPackGuardLocalDeps:
    """Test that packing rejects packages with local dependencies."""

    def test_pack_rejects_local_deps(self, tmp_path):
        from apm_cli.bundle.packer import pack_bundle

        # Set up project with local dep
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": ["./packages/my-local-pkg"],
                    },
                }
            )
        )

        # Create a lockfile so pack_bundle doesn't fail on missing lockfile
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(
                repo_url="_local/my-local-pkg",
                source="local",
                local_path="./packages/my-local-pkg",
            )
        )
        lock.write(tmp_path / "apm.lock")

        with pytest.raises(ValueError, match="local path dependency"):
            pack_bundle(tmp_path, tmp_path / "dist")

    def test_pack_allows_remote_deps(self, tmp_path):
        from apm_cli.bundle.packer import pack_bundle

        # Set up project with only remote dep
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {
                        "apm": ["owner/repo"],
                    },
                }
            )
        )

        # Create an empty lockfile
        lock = LockFile()
        lock.write(tmp_path / "apm.lock")

        # Should not raise (may fail for other reasons like missing files, that's OK)
        # We just check it gets past the guard
        try:
            pack_bundle(tmp_path, tmp_path / "dist")
        except ValueError as e:
            assert "local path dependency" not in str(e)


# ===========================================================================
# Copy local package helper
# ===========================================================================


class TestCopyLocalPackage:
    """Test the _copy_local_package helper from the install module."""

    def test_copy_local_package_with_apm_yml(self, tmp_path):
        from apm_cli.commands.install import _copy_local_package

        # Create a local package
        local_pkg = tmp_path / "my-local-pkg"
        local_pkg.mkdir()
        (local_pkg / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "my-local-pkg",
                    "version": "1.0.0",
                }
            )
        )
        instr_dir = local_pkg / ".apm" / "instructions"
        instr_dir.mkdir(parents=True)
        (instr_dir / "test.instructions.md").write_text("# Test")

        # Create dep ref and install path
        dep_ref = DependencyReference.parse(f"./{local_pkg.name}")
        dep_ref.local_path = str(local_pkg)  # Use absolute path for test
        install_path = tmp_path / "apm_modules" / "_local" / "my-local-pkg"

        result = _copy_local_package(dep_ref, install_path, tmp_path)
        assert result is not None
        assert result.exists()
        assert (result / "apm.yml").exists()
        assert (result / ".apm" / "instructions" / "test.instructions.md").exists()

    def test_copy_local_package_with_skill_md(self, tmp_path):
        from apm_cli.commands.install import _copy_local_package

        # Create a Claude Skill package (SKILL.md but no apm.yml)
        local_pkg = tmp_path / "my-skill"
        local_pkg.mkdir()
        (local_pkg / "SKILL.md").write_text("# My Skill")

        dep_ref = DependencyReference.parse(f"./{local_pkg.name}")
        dep_ref.local_path = str(local_pkg)
        install_path = tmp_path / "apm_modules" / "_local" / "my-skill"

        result = _copy_local_package(dep_ref, install_path, tmp_path)
        assert result is not None
        assert (result / "SKILL.md").exists()

    def test_copy_local_package_missing_path(self, tmp_path):
        from apm_cli.commands.install import _copy_local_package

        dep_ref = DependencyReference.parse("./nonexistent-pkg")
        install_path = tmp_path / "apm_modules" / "_local" / "nonexistent-pkg"

        result = _copy_local_package(dep_ref, install_path, tmp_path)
        assert result is None

    def test_copy_local_package_no_manifest(self, tmp_path):
        from apm_cli.commands.install import _copy_local_package

        # Create a directory without apm.yml or SKILL.md
        local_pkg = tmp_path / "no-manifest"
        local_pkg.mkdir()
        (local_pkg / "README.md").write_text("# No manifest")

        dep_ref = DependencyReference.parse(f"./{local_pkg.name}")
        dep_ref.local_path = str(local_pkg)
        install_path = tmp_path / "apm_modules" / "_local" / "no-manifest"

        result = _copy_local_package(dep_ref, install_path, tmp_path)
        assert result is None

    def test_copy_replaces_existing(self, tmp_path):
        from apm_cli.commands.install import _copy_local_package

        # Create a local package
        local_pkg = tmp_path / "my-pkg"
        local_pkg.mkdir()
        (local_pkg / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "my-pkg",
                    "version": "1.0.0",
                }
            )
        )
        (local_pkg / "data.txt").write_text("original")

        dep_ref = DependencyReference.parse(f"./{local_pkg.name}")
        dep_ref.local_path = str(local_pkg)
        install_path = tmp_path / "apm_modules" / "_local" / "my-pkg"

        # First copy
        _copy_local_package(dep_ref, install_path, tmp_path)
        assert (install_path / "data.txt").read_text() == "original"

        # Modify source
        (local_pkg / "data.txt").write_text("updated")

        # Second copy should overwrite
        _copy_local_package(dep_ref, install_path, tmp_path)
        assert (install_path / "data.txt").read_text() == "updated"

    def test_copy_preserves_symlinks_without_following(self, tmp_path):
        """Symlinks in local packages should be preserved, not followed."""
        from apm_cli.commands.install import _copy_local_package

        # Create a secret file outside the package
        secret_dir = tmp_path / "secret"
        secret_dir.mkdir()
        (secret_dir / "credentials.txt").write_text("TOP_SECRET")

        # Create a local package with a symlink pointing outside
        local_pkg = tmp_path / "evil-pkg"
        local_pkg.mkdir()
        (local_pkg / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "evil-pkg",
                    "version": "1.0.0",
                }
            )
        )
        (local_pkg / "escape").symlink_to(secret_dir)

        dep_ref = DependencyReference.parse(f"./{local_pkg.name}")
        dep_ref.local_path = str(local_pkg)
        install_path = tmp_path / "apm_modules" / "_local" / "evil-pkg"

        result = _copy_local_package(dep_ref, install_path, tmp_path)
        assert result is not None

        # The symlink should be preserved as a symlink, NOT followed
        link = install_path / "escape"
        assert link.is_symlink(), "Symlink was followed instead of preserved"
