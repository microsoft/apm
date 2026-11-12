"""Declared-constraint and package-qualified tag coverage for #3224.

Two things changed in ``require_pinned_constraint``:

* Literal tags in the layouts the git-semver resolver selects from
  (``{name}-v{version}``, ``{name}--v{version}``) count as pinned when
  ``{name}`` is the dependency's own package name.
* The check can be given a snapshot of the declared dependencies when
  ``dep.reference`` has already been rewritten to the selected tag, so a
  range is judged as the range the author wrote and a bare branch stays a
  violation even after resolution replaced it with a tag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.install.helpers import ref_reuse
from apm_cli.models.apm_package import APMPackage, DependencyReference
from apm_cli.models.dependency.replacement import project_manifest_dependencies
from apm_cli.policy._constraint_pinning import (
    UnboundedReason,
    classify_unbounded_reason,
    collect_declared_refs,
)
from apm_cli.policy.policy_checks import _check_pinned_constraints, run_policy_checks
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy

_REPLACE_SCHEMA = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
)


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


def test_tag_name_comes_from_the_resolver_owner(monkeypatch):
    # The policy has no package-name rule of its own: it asks the
    # resolver's owner (ref_reuse.git_semver_package_name), so the tags it
    # recognises are exactly the tags the resolver could select.
    dep = _dep("acme/mono/packages/pkg-a#pkg-a-v1.1.0")
    assert classify_unbounded_reason(dep) is None
    monkeypatch.setattr(ref_reuse, "git_semver_package_name", lambda _dep: "pkg-b")
    assert classify_unbounded_reason(dep) is UnboundedReason.BARE_BRANCH
    assert classify_unbounded_reason(_dep("acme/mono/packages/pkg-a#pkg-b-v1.1.0")) is None


# ---------------------------------------------------------------------------
# The declared snapshot beats the rewritten reference
# ---------------------------------------------------------------------------


def _policy() -> DependencyPolicy:
    return DependencyPolicy(require_pinned_constraint=True)


def _resolved(spec: str, selected_tag: str) -> DependencyReference:
    """A dep whose ``reference`` the resolve phase already replaced."""
    dep = _dep(spec)
    dep.reference = selected_tag
    return dep


def _declared_then_resolved(spec: str, selected_tag: str):
    """Snapshot the declaration, then rewrite the reference as resolve does."""
    dep = _dep(spec)
    declared = collect_declared_refs([dep])
    dep.reference = selected_tag
    return dep, declared


def _check(dep: DependencyReference, declared):
    return _check_pinned_constraints([dep], _policy(), {dep.get_unique_key()}, declared)


def test_snapshot_is_not_rewritten_with_the_reference():
    dep, declared = _declared_then_resolved("acme/mono/packages/pkg-a#^1.0.0", "pkg-a-v1.1.0")
    assert dep.reference == "pkg-a-v1.1.0"
    assert [d.reference for d in declared[dep.get_unique_key()]] == ["^1.0.0"]


def test_check_passes_resolved_range_when_declared_refs_are_supplied():
    dep, declared = _declared_then_resolved("acme/mono/packages/pkg-a#^1.0.0", "pkg-a-v1.1.0")
    result = _check(dep, declared)
    assert result.passed, result.details


def test_check_still_fails_declared_branch_after_resolution():
    dep, declared = _declared_then_resolved("acme/mono/packages/pkg-a#main", "pkg-a-v1.1.0")
    result = _check(dep, declared)
    assert not result.passed
    assert any("bare branch 'main'" in line for line in result.details), result.details
    assert not any("pkg-a-v1.1.0" in line for line in result.details), result.details


def test_selected_sha_does_not_launder_an_open_upper_range():
    dep, declared = _declared_then_resolved("acme/widget#>=1.0.0", "a" * 40)
    assert classify_unbounded_reason(dep) is None  # the SHA alone would pass
    result = _check(dep, declared)
    assert not result.passed
    assert any("unbounded upper" in line for line in result.details), result.details


def test_declared_missing_ref_stays_no_ref_after_a_tag_was_selected():
    dep, declared = _declared_then_resolved("acme/widget", "widget-v1.2.0")
    assert classify_unbounded_reason(dep) is None  # the selected tag alone would pass
    result = _check(dep, declared)
    assert not result.passed
    assert any("no ref" in line for line in result.details), result.details


def test_without_declared_ref_the_reference_is_classified():
    assert classify_unbounded_reason(_dep("acme/widget#main")) is UnboundedReason.BARE_BRANCH


def test_check_without_declared_refs_keeps_the_old_contract():
    # Callers that hold the manifest view (preflight, audit) pass nothing
    # and the check reads ``dep.reference`` as before.
    assert _check_pinned_constraints([_dep("acme/widget#^1.0.0")], _policy()).passed
    assert not _check_pinned_constraints([_dep("acme/widget#main")], _policy()).passed


def test_declared_refs_only_apply_to_their_own_key():
    good, declared = _declared_then_resolved("acme/mono/packages/pkg-a#^1.0.0", "pkg-a-v1.1.0")
    other = _dep("acme/widget#main")
    result = _check_pinned_constraints(
        [good, other],
        _policy(),
        {good.get_unique_key(), other.get_unique_key()},
        declared_refs=declared,
    )
    assert not result.passed
    assert len(result.details) == 1 and "acme/widget" in result.details[0]


# ---------------------------------------------------------------------------
# Several declarations of one package (Copilot review on #3381)
# ---------------------------------------------------------------------------


def test_collect_declared_refs_keeps_every_declaration_per_key():
    deps = [_dep("acme/widget#main"), _dep("acme/widget#v1.2.3"), _dep("acme/other")]
    declared = collect_declared_refs(deps)
    assert {key: tuple(d.reference for d in snap) for key, snap in declared.items()} == {
        deps[0].get_unique_key(): ("main", "v1.2.3"),
        deps[2].get_unique_key(): (None,),
    }


@pytest.mark.parametrize(
    ("unbounded", "bounded", "hint"),
    [
        ("acme/widget#main", "acme/widget#v1.2.3", "bare branch 'main'"),
        ("acme/widget#v1.2.3", "acme/widget#main", "bare branch 'main'"),
        ("acme/widget", "acme/widget#^1.0.0", "no ref"),
        ("acme/widget#>=1.0.0", "acme/widget#^1.0.0", "unbounded upper"),
    ],
)
def test_bounded_declaration_does_not_cover_an_unbounded_one(unbounded, bounded, hint):
    # e.g. ``dependencies: [acme/widget#main]`` and
    # ``devDependencies: [acme/widget#v1.2.3]`` share one unique key; the
    # resolved set holds one dep whose reference is a selected tag.
    declared_deps = [_dep(unbounded), _dep(bounded)]
    resolved = _resolved(bounded, "v1.2.3")
    result = _check_pinned_constraints(
        [resolved],
        _policy(),
        {resolved.get_unique_key()},
        declared_refs=collect_declared_refs(declared_deps),
    )
    assert not result.passed
    assert len(result.details) == 1 and hint in result.details[0], result.details


def test_repeated_identical_violation_is_reported_once():
    deps = [_dep("acme/widget#main"), _dep("acme/widget#main")]
    result = _check_pinned_constraints(
        deps,
        _policy(),
        {deps[0].get_unique_key()},
        declared_refs=collect_declared_refs(deps),
    )
    assert result.details == ["acme/widget: bare branch 'main' tracks a moving tip"]


def test_each_declaration_keeps_its_own_package_name():
    # GitHub keys fold repository casing; tag matching does not. Each
    # declaration is judged against its own name (Copilot review on #3652).
    upper, lower = _dep("acme/Widget#Widget-v1.2.3"), _dep("acme/widget#widget-v1.2.3")
    assert upper.get_unique_key() == lower.get_unique_key()
    resolved = _resolved("acme/widget#widget-v1.2.3", "widget-v1.2.3")
    result = _check_pinned_constraints(
        [resolved],
        _policy(),
        {resolved.get_unique_key()},
        declared_refs=collect_declared_refs([upper, lower]),
    )
    assert result.passed, result.details


# ---------------------------------------------------------------------------
# replace: rules are judged by the selected source (Copilot review on #3652)
# ---------------------------------------------------------------------------


def _manifest(tmp_path: Path, fork_ref: str) -> APMPackage:
    (tmp_path / "apm.yml").write_text(
        f"$schema: {_REPLACE_SCHEMA}\n"
        "name: consumer\n"
        "version: 1.0.0\n"
        "dependencies:\n"
        "  apm:\n"
        "    - acme/widget#^1.0.0\n"
        "replace:\n"
        "  - for: {git: https://github.com/acme/widget}\n"
        f"    with: {{git: https://github.com/acme/fork, ref: {fork_ref}}}\n",
        encoding="utf-8",
    )
    return APMPackage.from_apm_yml(tmp_path / "apm.yml")


@pytest.mark.parametrize(("fork_ref", "passed"), [("main", False), ("v2.0.0", True)])
def test_replacement_is_judged_by_its_selected_ref(tmp_path, fork_ref, passed):
    # A bounded original is provenance, not a constraint on the replacement
    # (manage-dependencies.md), so it must not cover an unbounded fork ref.
    selected = project_manifest_dependencies(_manifest(tmp_path, fork_ref))
    result = _check_pinned_constraints(
        selected,
        _policy(),
        {dep.get_unique_key() for dep in selected},
        declared_refs=collect_declared_refs(selected),
    )
    assert result.passed is passed, result.details


@pytest.mark.parametrize(("fork_ref", "passed"), [("main", False), ("v2.0.0", True)])
def test_audit_judges_a_replacement_by_its_selected_ref(tmp_path, fork_ref, passed):
    # ``apm audit`` must agree with the install gate on a replaced dep.
    _manifest(tmp_path, fork_ref)
    policy = ApmPolicy(dependencies=DependencyPolicy(require_pinned_constraint=True))
    result = run_policy_checks(tmp_path, policy, fail_fast=False)
    (check,) = [c for c in result.checks if c.name == "dependency-pinned-constraint"]
    assert check.passed is passed, check.details
