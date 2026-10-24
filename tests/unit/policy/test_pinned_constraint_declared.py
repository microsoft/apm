"""Declared-constraint and package-qualified tag coverage for #3224.

Two things changed in ``require_pinned_constraint``:

* Literal tags in the layouts the git-semver resolver selects from
  (``{name}-v{version}``, ``{name}--v{version}``) count as pinned when
  ``{name}`` is the dependency's own package name.
* The check can be told the manifest-declared ref when ``dep.reference``
  has already been rewritten to the selected tag, so a range is judged as
  the range the author wrote and a bare branch stays a violation even
  after resolution replaced it with a tag.
"""

from __future__ import annotations

import pytest

from apm_cli.install.helpers.ref_reuse import _git_semver_package_name
from apm_cli.models.apm_package import DependencyReference
from apm_cli.policy._constraint_pinning import (
    UnboundedReason,
    classify_unbounded_reason,
    humanize_reason,
    package_tag_name,
)
from apm_cli.policy.policy_checks import _check_pinned_constraints
from apm_cli.policy.schema import DependencyPolicy


def _dep(spec: str) -> DependencyReference:
    return DependencyReference.parse(spec)


# ---------------------------------------------------------------------------
# Package-qualified literal tags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        "acme/mono/packages/pkg-a#pkg-a-v1.1.0",
        "acme/mono/packages/pkg-a#pkg-a--v1.1.0",
        "acme/mono/packages/pkg-a#pkg-a-v1.1.0-rc.1",
        "acme/mono/packages/pkg-a#pkg-a-v1.1.0+build.5",
        "acme/widget#widget-v1.2.0",
        "acme/widget#widget--v1.2.0",
        "https://github.com/acme/widget.git#widget-v1.2.0",
    ],
)
def test_package_qualified_literal_tag_for_own_name_is_pinned(spec):
    assert classify_unbounded_reason(_dep(spec)) is None


@pytest.mark.parametrize(
    "spec",
    [
        # another package's tag on a subdirectory dep
        "acme/mono/packages/pkg-a#pkg-b-v1.1.0",
        # the repository's tag layout on a subdirectory dep
        "acme/mono/packages/pkg-a#mono-v1.1.0",
        # a subdirectory package's tag on the repository dep
        "acme/mono#pkg-a-v1.1.0",
        # version-looking suffix that is not a full semver
        "acme/mono/packages/pkg-a#pkg-a-v1.1",
        "acme/mono/packages/pkg-a#pkg-a-v1",
        # arbitrary prefix
        "acme/mono/packages/pkg-a#release-pkg-a-v1.1.0",
        # layouts the resolver does not select from
        "acme/mono/packages/pkg-a#pkg-a_v1.1.0",
        "acme/mono/packages/pkg-a#pkg-a@1.1.0",
    ],
)
def test_other_names_and_near_misses_stay_bare_branch(spec):
    assert classify_unbounded_reason(_dep(spec)) is UnboundedReason.BARE_BRANCH


@pytest.mark.parametrize(
    "spec",
    [
        "acme/mono/packages/pkg-a#^1.0.0",
        "acme/mono#^1.0.0",
        "acme/widget#widget-v1.2.0",
        "https://github.com/acme/widget.git#widget-v1.2.0",
    ],
)
def test_package_tag_name_matches_the_resolver(spec):
    dep = _dep(spec)
    assert package_tag_name(dep) == _git_semver_package_name(dep)


def test_package_tag_name_handles_missing_fields():
    class Bare:
        pass

    assert package_tag_name(Bare()) is None


# ---------------------------------------------------------------------------
# Declared ref beats the rewritten reference
# ---------------------------------------------------------------------------


def _resolved(spec: str, selected_tag: str) -> DependencyReference:
    """A dep whose ``reference`` the resolve phase already replaced."""
    dep = _dep(spec)
    dep.reference = selected_tag
    return dep


def test_declared_range_is_judged_not_the_selected_tag():
    dep = _resolved("acme/mono/packages/pkg-a#^1.0.0", "pkg-a-v1.1.0")
    assert dep.reference == "pkg-a-v1.1.0"
    assert classify_unbounded_reason(dep, declared_ref="^1.0.0") is None


def test_selected_tag_does_not_launder_a_bare_branch():
    dep = _resolved("acme/mono/packages/pkg-a#main", "pkg-a-v1.1.0")
    assert classify_unbounded_reason(dep, declared_ref="main") is UnboundedReason.BARE_BRANCH


def test_selected_sha_does_not_launder_an_open_upper_range():
    dep = _resolved("acme/widget#>=1.0.0", "a" * 40)
    assert classify_unbounded_reason(dep) is None  # the SHA alone would pass
    assert classify_unbounded_reason(dep, declared_ref=">=1.0.0") is UnboundedReason.OPEN_UPPER


def test_declared_ref_none_falls_back_to_reference():
    dep = _dep("acme/widget#main")
    assert classify_unbounded_reason(dep, declared_ref=None) is UnboundedReason.BARE_BRANCH


def test_humanize_names_the_declared_ref():
    dep = _resolved("acme/widget#main", "widget-v1.2.0")
    hint = humanize_reason(UnboundedReason.BARE_BRANCH, dep, ref="main")
    assert "'main'" in hint
    assert "widget-v1.2.0" not in hint


# ---------------------------------------------------------------------------
# The check itself, with and without declared refs
# ---------------------------------------------------------------------------


def _policy() -> DependencyPolicy:
    return DependencyPolicy(require_pinned_constraint=True)


def test_check_passes_resolved_range_when_declared_refs_are_supplied():
    dep = _resolved("acme/mono/packages/pkg-a#^1.0.0", "pkg-a-v1.1.0")
    result = _check_pinned_constraints(
        [dep],
        _policy(),
        {dep.get_unique_key()},
        declared_refs={dep.get_unique_key(): "^1.0.0"},
    )
    assert result.passed, result.details


def test_check_still_fails_declared_branch_after_resolution():
    dep = _resolved("acme/mono/packages/pkg-a#main", "pkg-a-v1.1.0")
    result = _check_pinned_constraints(
        [dep],
        _policy(),
        {dep.get_unique_key()},
        declared_refs={dep.get_unique_key(): "main"},
    )
    assert not result.passed
    assert any("bare branch 'main'" in line for line in result.details), result.details


def test_check_without_declared_refs_keeps_the_old_contract():
    # Callers that hold the manifest view (preflight, audit) pass nothing
    # and the check reads ``dep.reference`` as before.
    assert _check_pinned_constraints([_dep("acme/widget#^1.0.0")], _policy()).passed
    assert not _check_pinned_constraints([_dep("acme/widget#main")], _policy()).passed


def test_declared_refs_only_apply_to_their_own_key():
    good = _resolved("acme/mono/packages/pkg-a#^1.0.0", "pkg-a-v1.1.0")
    other = _dep("acme/widget#main")
    result = _check_pinned_constraints(
        [good, other],
        _policy(),
        {good.get_unique_key(), other.get_unique_key()},
        declared_refs={good.get_unique_key(): "^1.0.0"},
    )
    assert not result.passed
    assert len(result.details) == 1 and "acme/widget" in result.details[0]
