"""Unit tests for traversal alias rejection (alias-poisoning fix) (filesystem, process and CLI boundary cases)."""

from pathlib import Path

import pytest

from apm_cli.models.dependency import DependencyReference
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-url-path-containment-decoding")]


class TestInstallPhaseSymlinkEscape:
    """A symlinked alias directory cannot move the write target outside apm_modules.

    Parse-time validation (``parse_alias_override``) accepts a syntactically
    valid alias like ``safe-name`` regardless of what already exists on disk.
    The escape vector is the *filesystem*: if ``apm_modules_dir/safe-name`` is a
    symlink pointing outside ``apm_modules_dir``, then writing through the
    alias path would escape the managed tree. ``ensure_path_within`` is uniquely
    load-bearing here because it resolves symlinks before checking containment;
    both install phases delegate to the materialization owner, which invokes
    it for every aliased dependency.
    """

    def test_symlink_escape_is_blocked(self, tmp_path):
        apm_modules_dir = tmp_path / "apm_modules"
        apm_modules_dir.mkdir()
        outside = tmp_path / "escape_target"
        outside.mkdir()
        sentinel = outside / "secret.txt"
        sentinel.write_text("do not escape")

        # Parse-time validation accepts the alias "safe-name"; the escape
        # vector is the filesystem: apm_modules_dir/safe-name is a symlink
        # planted outside apm_modules_dir.
        (apm_modules_dir / "safe-name").symlink_to(outside)

        # Construct the same path the install phases build for an aliased
        # dependency: (apm_modules_dir / dep_ref.alias) == apm_modules/safe-name
        alias_path = apm_modules_dir / "safe-name"

        with pytest.raises(PathTraversalError):
            ensure_path_within(alias_path, apm_modules_dir)

        # The escape was blocked, never followed: the outside target is intact.
        assert sentinel.read_text() == "do not escape"

    @pytest.mark.windows_compat
    @pytest.mark.parametrize("target", ["root", "child", "outside"])
    def test_extended_prefix_alias_destination(self, tmp_path, monkeypatch, target):
        """Emulate Windows resolve spelling without requiring junction privileges."""
        modules = tmp_path / "apm_modules"
        alias_path = modules / "safe-name"
        destination = {
            "root": modules,
            "child": modules / "child",
            "outside": tmp_path / "outside",
        }[target]
        original_resolve = Path.resolve

        def resolve_with_prefix(path, *args, **kwargs):
            if path == alias_path:
                return Path("\\\\?\\" + str(destination))
            if path == modules:
                return Path("\\\\?\\" + str(modules))
            return original_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve_with_prefix)
        dependency = DependencyReference(repo_url="org/pkg", alias="safe-name")
        if target == "child":
            assert dependency.get_install_path(modules) == alias_path
        else:
            with pytest.raises(PathTraversalError):
                dependency.get_install_path(modules)
