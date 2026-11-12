"""Unit tests for traversal alias rejection (alias-poisoning fix)."""

from pathlib import Path

import pytest

from apm_cli.models.dependency import DependencyReference
from apm_cli.models.dependency.object_fields import parse_alias_override
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within


class TestParseAliasOverrideTraversal:
    """Reject aliases that navigate outside apm_modules."""

    def test_single_dot_rejected(self):
        with pytest.raises(ValueError, match="reserved directory names"):
            parse_alias_override(".")

    def test_double_dot_rejected(self):
        with pytest.raises(ValueError, match="reserved directory names"):
            parse_alias_override("..")

    @pytest.mark.parametrize("alias", [".", ".."])
    @pytest.mark.parametrize(
        "prefix", ["ssh://git@github.com/org/pkg.git@", "git@github.com:org/pkg.git@"]
    )
    def test_ssh_alias_uses_shared_validation(self, prefix, alias):
        with pytest.raises(ValueError, match="reserved directory names"):
            DependencyReference.parse(prefix + alias)

    def test_traversal_segment_rejected(self):
        with pytest.raises(
            ValueError,
            match="Aliases can only contain letters, numbers, dots, underscores, and hyphens",
        ):
            parse_alias_override("foo/../bar")

    def test_leading_dot_slash_rejected(self):
        with pytest.raises(
            ValueError,
            match="Aliases can only contain letters, numbers, dots, underscores, and hyphens",
        ):
            parse_alias_override("./safe-name")

    def test_trailing_dot_dot_rejected(self):
        with pytest.raises(
            ValueError,
            match="Aliases can only contain letters, numbers, dots, underscores, and hyphens",
        ):
            parse_alias_override("pkg/..")

    def test_encoded_dot_dot_rejected(self):
        with pytest.raises(
            ValueError,
            match="Aliases can only contain letters, numbers, dots, underscores, and hyphens",
        ):
            parse_alias_override("%2e%2e")

    def test_double_encoded_dot_dot_rejected(self):
        with pytest.raises(
            ValueError,
            match="Aliases can only contain letters, numbers, dots, underscores, and hyphens",
        ):
            parse_alias_override("%252e%252e")


class TestParseAliasOverrideSafe:
    """Safe aliases must still be accepted."""

    def test_simple_name(self):
        assert parse_alias_override("my-skill") == "my-skill"

    def test_name_with_dots(self):
        assert parse_alias_override("my-skill.v2") == "my-skill.v2"

    @pytest.mark.parametrize("alias", [".safe", "safe.", "foo..bar", "my-skill.v2"])
    def test_safe_dotted_alias_preserves_spelling(self, alias):
        assert parse_alias_override(alias) == alias
        ref = DependencyReference.parse("ssh://git@github.com/org/pkg.git@" + alias)
        assert ref.alias == alias

    def test_deep_dotted_name(self):
        assert parse_alias_override("deep.nested.name") == "deep.nested.name"

    def test_underscore_and_hyphen(self):
        assert parse_alias_override("skill_v2-beta") == "skill_v2-beta"

    def test_none_returns_none(self):
        assert parse_alias_override(None) is None

    def test_empty_string_rejected(self):
        with pytest.raises(ValueError):
            parse_alias_override("")

    def test_whitespace_only_rejected(self):
        with pytest.raises(ValueError):
            parse_alias_override("   ")

    def test_invalid_chars_rejected(self):
        with pytest.raises(ValueError):
            parse_alias_override("bad alias!")


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
