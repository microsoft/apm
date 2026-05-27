"""Tests for ``apm_cli.policy._constraint_pinning.classify_unbounded_reason``.

Table-driven: every classification rule has at least one positive case.
"""

from __future__ import annotations

import pytest

from apm_cli.models.apm_package import DependencyReference
from apm_cli.policy._constraint_pinning import (
    UnboundedReason,
    classify_unbounded_reason,
    humanize_reason,
    is_pinned_constraint,
)


def _dep(
    spec: str | None,
    *,
    source: str | None = None,
    registry_name: str | None = None,
    is_local: bool = False,
) -> DependencyReference:
    """Build a ``DependencyReference`` with the given ref/source.

    Local deps bypass parser (``DependencyReference.parse`` rejects
    local paths via ``./`` shorthand differently); construct directly.
    """
    if is_local:
        return DependencyReference(
            repo_url="_local/sample",
            is_local=True,
            local_path="./packages/sample",
        )
    if source == "registry":
        dep = DependencyReference(repo_url="acme/lib", reference=spec)
        dep.source = "registry"
        dep.registry_name = registry_name or "default"
        return dep
    # Git-source (the legacy default).
    if spec is None:
        return DependencyReference.parse("acme/lib")
    return DependencyReference.parse(f"acme/lib#{spec}")


# ---------------------------------------------------------------------------
# Unbounded cases
# ---------------------------------------------------------------------------


def test_classify_no_ref_returns_no_ref():
    assert classify_unbounded_reason(_dep(None)) is UnboundedReason.NO_REF


def test_classify_empty_ref_returns_no_ref():
    dep = DependencyReference.parse("acme/lib")
    dep.reference = ""
    assert classify_unbounded_reason(dep) is UnboundedReason.NO_REF


def test_classify_whitespace_ref_returns_no_ref():
    dep = DependencyReference.parse("acme/lib")
    dep.reference = "   "
    assert classify_unbounded_reason(dep) is UnboundedReason.NO_REF


def test_classify_bare_branch_main_returns_bare_branch():
    assert classify_unbounded_reason(_dep("main")) is UnboundedReason.BARE_BRANCH


def test_classify_bare_branch_develop_returns_bare_branch():
    assert classify_unbounded_reason(_dep("develop")) is UnboundedReason.BARE_BRANCH


def test_classify_feature_branch_returns_bare_branch():
    assert classify_unbounded_reason(_dep("feature/foo")) is UnboundedReason.BARE_BRANCH


def test_classify_wildcard_star_returns_wildcard():
    assert classify_unbounded_reason(_dep("*")) is UnboundedReason.WILDCARD


def test_classify_wildcard_x_lowercase_returns_wildcard():
    assert classify_unbounded_reason(_dep("x")) is UnboundedReason.WILDCARD


def test_classify_wildcard_x_uppercase_returns_wildcard():
    assert classify_unbounded_reason(_dep("X")) is UnboundedReason.WILDCARD


def test_classify_open_upper_returns_open_upper():
    assert classify_unbounded_reason(_dep(">=1.0.0")) is UnboundedReason.OPEN_UPPER


def test_classify_greater_than_only_returns_gt_only():
    assert classify_unbounded_reason(_dep(">1.0.0")) is UnboundedReason.GREATER_THAN_ONLY


def test_classify_open_upper_in_multi_component():
    # ``>=1.0.0 >=2.0.0`` has no upper bound either.
    assert classify_unbounded_reason(_dep(">=1.0.0 >=2.0.0")) is UnboundedReason.OPEN_UPPER


# ---------------------------------------------------------------------------
# Pinned cases (return None)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        "^1.2.3",
        "^0.0.1",
        "~1.2.3",
        "1.2.3",
        "0.0.1",
        ">=1.0.0 <2.0.0",
        ">=1.0.0 <=1.9.9",
        "1.2.x",
        "1.2.X",
        "v1.2.3",
        "v1.0.0-rc.1",
        "v1.2.3+build.42",
    ],
)
def test_classify_pinned_specs_return_none(spec):
    assert classify_unbounded_reason(_dep(spec)) is None


# Regression trap for the ``=1.2.3`` alternate exact-version form
# (npm- and cargo-style "explicit equality"). Before the fix shipped
# alongside this test, ``_constraint_pinning.py`` mis-classified these
# as ``BARE_BRANCH`` because ``is_semver_range`` rejected the leading
# ``=`` operator, and ``require_pinned_constraint: true`` would block
# the install with a confusing branch-name diagnostic.
@pytest.mark.parametrize(
    "spec",
    [
        "=1.2.3",
        "=0.0.1",
        "=1.2.3-beta.1",
        "=1.2.3+build.42",
    ],
)
def test_equals_prefix_exact_version_classified_as_pinned(spec):
    assert classify_unbounded_reason(_dep(spec)) is None
    assert is_pinned_constraint(_dep(spec)) is True


def test_equals_prefix_exact_version_pinned_on_registry_source():
    # Same contract for registry-routed deps: ``=1.2.3`` is a pin, not
    # an unbounded constraint.
    assert classify_unbounded_reason(_dep("=1.2.3", source="registry")) is None


def test_double_equals_prefix_rejected_as_bare_branch():
    # APM follows the npm/cargo semver grammar where ``=`` is the
    # explicit-equality operator. The pip-style ``==`` form is NOT
    # part of node-semver and is intentionally not recognised; it
    # falls through to ``BARE_BRANCH`` so that a user who wrote
    # ``==1.2.3`` gets a violation that points them at the supported
    # syntax instead of silently accepting the wrong dialect.
    assert classify_unbounded_reason(_dep("==1.2.3")) is UnboundedReason.BARE_BRANCH


def test_classify_caret_range_returns_pinned_none():
    assert classify_unbounded_reason(_dep("^1.2.3")) is None


def test_classify_tilde_range_returns_pinned_none():
    assert classify_unbounded_reason(_dep("~1.2.3")) is None


def test_classify_bounded_range_returns_pinned_none():
    assert classify_unbounded_reason(_dep(">=1.0.0 <2.0.0")) is None


def test_classify_literal_tag_v_prefixed_returns_pinned_none():
    assert classify_unbounded_reason(_dep("v1.5.3")) is None


def test_classify_sha_returns_pinned_none():
    sha = "a" * 40
    assert classify_unbounded_reason(_dep(sha)) is None


def test_classify_local_dep_returns_pinned_none():
    assert classify_unbounded_reason(_dep(None, is_local=True)) is None


def test_classify_registry_dep_with_exact_version_returns_pinned_none():
    assert classify_unbounded_reason(_dep("1.2.3", source="registry")) is None


def test_classify_registry_dep_with_caret_returns_pinned_none():
    assert classify_unbounded_reason(_dep("^1.2.0", source="registry")) is None


def test_classify_registry_dep_with_open_range_returns_open_upper():
    assert (
        classify_unbounded_reason(_dep(">=1.0.0", source="registry")) is UnboundedReason.OPEN_UPPER
    )


def test_classify_registry_dep_missing_ref_returns_no_ref():
    dep = _dep("1.0.0", source="registry")
    dep.reference = None
    assert classify_unbounded_reason(dep) is UnboundedReason.NO_REF


# ---------------------------------------------------------------------------
# Future-uniformity tests (cross-source semver semantics)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=False,
    reason=(
        "awaits #1488 (git-source semver routing). The shape-based "
        "classifier already accepts ``^1.2.0`` regardless of source, so "
        "this test xpasses today on every supported semver shape. The "
        "marker stays in place so that once #1488 lands and introduces "
        "a normalised ``source='git-semver'`` value, a CI failure here "
        "alerts us to revisit the classification path end-to-end."
    ),
)
def test_classify_git_semver_dep_returns_pinned_none():
    # Pseudo: when #1488 lands, ``acme/lib#^1.2.0`` will be tagged
    # source="git-semver" or the resolver will normalize the ref. Either
    # way, the classifier needs to see "^1.2.0" as a pinned constraint.
    dep = DependencyReference.parse("acme/lib#^1.2.0")
    dep.source = "git-semver"  # hypothetical post-#1488 marker
    assert classify_unbounded_reason(dep) is None
    # Sentinel that will start failing once the source-discriminator
    # actually exists post-#1488, signalling time to remove the xfail.
    raise AssertionError(
        "remove xfail decorator once #1488 lands and 'git-semver' source "
        "discriminator is wired in the resolver"
    )


def test_classify_marketplace_dep_with_caret_returns_pinned_none():
    """Until PR #1422 lands, marketplace deps surface as git-source.

    Pre-#1422, marketplace entries are routed through the git
    resolver and carry ``source=None`` / ``"git"``. The shape-based
    classifier still recognises ``^1.2.0`` as pinned because the
    constraint string is identical regardless of source.

    Post-#1422 (when ``source="marketplace"`` is set), the same
    classification path applies via the semver-range probe.  This
    test pins the invariant: shape decides, not source.
    """
    dep = DependencyReference.parse("acme/skills#^1.2.0")
    assert classify_unbounded_reason(dep) is None
    # Also exercise the post-#1422 marker (treated as git for now).
    dep.source = "marketplace"
    assert classify_unbounded_reason(dep) is None


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------


def test_is_pinned_constraint_true_for_caret():
    assert is_pinned_constraint(_dep("^1.2.3")) is True


def test_is_pinned_constraint_false_for_bare_branch():
    assert is_pinned_constraint(_dep("main")) is False


@pytest.mark.parametrize(
    "reason,must_contain",
    [
        (UnboundedReason.NO_REF, "no ref"),
        (UnboundedReason.BARE_BRANCH, "bare branch"),
        (UnboundedReason.WILDCARD, "wildcard"),
        (UnboundedReason.OPEN_UPPER, "unbounded upper"),
        (UnboundedReason.GREATER_THAN_ONLY, "no upper bound"),
    ],
)
def test_humanize_reason_is_actionable_ascii(reason, must_contain):
    dep = _dep("main")
    msg = humanize_reason(reason, dep)
    assert must_contain in msg
    # ASCII-only invariant.
    assert msg.encode("ascii", errors="strict")
