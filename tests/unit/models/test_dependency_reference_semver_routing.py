"""Semver-routing detection on ``DependencyReference``.

The ``ref_kind`` property classifies ``reference`` values so the install
pipeline can route to the git-semver resolver only when the author wrote
a semver range. Literal tags, branch names, and SHAs MUST keep their
existing literal-routing behaviour.
"""

from __future__ import annotations

import pytest

from apm_cli.models.dependency.reference import DependencyReference


class TestStringShorthandRouting:
    """Routing from the ``owner/repo#<ref>`` shorthand."""

    def test_string_shorthand_with_caret_range_routes_to_semver(self) -> None:
        dep = DependencyReference.parse("acme/some-skills#^1.2.0")
        assert dep.reference == "^1.2.0"
        assert dep.ref_kind == "semver"

    def test_string_shorthand_with_tilde_range_routes_to_semver(self) -> None:
        dep = DependencyReference.parse("acme/some-skills#~2.1.0")
        assert dep.ref_kind == "semver"

    def test_string_shorthand_with_wildcard_routes_to_semver(self) -> None:
        dep = DependencyReference.parse("acme/some-skills#1.2.x")
        assert dep.ref_kind == "semver"

    def test_string_shorthand_with_branch_name_does_not_route_to_semver(self) -> None:
        dep = DependencyReference.parse("acme/some-skills#main")
        assert dep.reference == "main"
        assert dep.ref_kind == "literal"

    def test_string_shorthand_with_sha_does_not_route_to_semver(self) -> None:
        dep = DependencyReference.parse("acme/some-skills#abc1234")
        assert dep.ref_kind == "literal"

    def test_no_reference_returns_none(self) -> None:
        dep = DependencyReference.parse("acme/some-skills")
        assert dep.reference is None
        assert dep.ref_kind is None


class TestObjectFormRouting:
    """Routing from the ``{git: ..., ref: ...}`` object form."""

    def test_object_form_caret_range_routes_to_semver(self) -> None:
        dep = DependencyReference.parse_from_dict(
            {
                "git": "https://github.com/acme/some-skills.git",
                "ref": "^1.2.0",
            }
        )
        assert dep.reference == "^1.2.0"
        assert dep.ref_kind == "semver"

    def test_object_form_branch_name_does_not_route_to_semver(self) -> None:
        dep = DependencyReference.parse_from_dict(
            {
                "git": "https://github.com/acme/some-skills.git",
                "ref": "main",
            }
        )
        assert dep.ref_kind == "literal"


class TestLiteralTagRegressionTrap:
    """Regression trap: literal ``v1.2.3`` tag MUST NOT route to semver.

    ``v1.2.3`` is a valid git tag literal; before #1488, authors who
    wrote ``ref: v1.2.3`` got an exact-tag clone. Routing this through
    the semver resolver would be a behaviour break.
    """

    def test_literal_tag_v1_2_3_does_not_route_to_semver(self) -> None:
        dep = DependencyReference.parse("acme/some-skills#v1.2.3")
        assert dep.reference == "v1.2.3"
        # ``v1.2.3`` does NOT parse as a semver range (leading 'v' is not
        # an operator); the literal path takes it. The git-semver resolver
        # is never invoked, preserving pre-#1488 behaviour.
        assert dep.ref_kind == "literal"

    def test_bare_version_1_2_3_routes_to_semver(self) -> None:
        # The mirror case: ``1.2.3`` with no prefix parses as an exact-
        # version semver constraint, so it DOES route through the
        # resolver. The resolver's bare-version fallback pattern covers
        # the case where the remote tag is also literally ``1.2.3``.
        dep = DependencyReference.parse("acme/some-skills#1.2.3")
        assert dep.reference == "1.2.3"
        assert dep.ref_kind == "semver"

    def test_subpath_tag_with_at_v_separator_parses_as_literal_ref(self) -> None:
        dep = DependencyReference.parse("owner/repo/packages/somepackage#somepackage@v1.0.1")

        assert dep.repo_url == "owner/repo"
        assert dep.virtual_path == "packages/somepackage"
        assert dep.reference == "somepackage@v1.0.1"
        assert dep.ref_kind == "literal"

    def test_shorthand_alias_before_ref_fragment_is_still_rejected(self) -> None:
        with pytest.raises(ValueError, match="Shorthand '@alias' is not supported"):
            DependencyReference.parse("owner/repo@alias")

    def test_subpath_shorthand_alias_before_ref_fragment_is_still_rejected(self) -> None:
        with pytest.raises(ValueError, match="Shorthand '@alias' is not supported"):
            DependencyReference.parse("owner/repo/subpath@alias#main")
