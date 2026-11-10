"""Unit tests for :mod:`apm_cli.deps.lock_parent_placement` (#3510)."""

from __future__ import annotations

from pathlib import Path

from apm_cli.deps.lock_parent_placement import (
    installed_manifest_children,
    place_legacy_lock_parents,
)
from apm_cli.deps.lockfile import LockedDependency


def _virtual(path: str, depth: int, resolved_by: str | None = None) -> LockedDependency:
    return LockedDependency(
        repo_url="acme/mono",
        virtual_path=path,
        is_virtual=True,
        depth=depth,
        resolved_by=resolved_by,
    )


def _plain(repo_url: str, depth: int, resolved_by: str | None = None) -> LockedDependency:
    return LockedDependency(repo_url=repo_url, depth=depth, resolved_by=resolved_by)


_ROOT = _virtual("presets/root", 1)
_LEFT = _virtual("presets/left", 2, "acme/mono")
_RIGHT = _virtual("presets/right", 2, "acme/mono")
_LEFT_CHILD = _plain("acme/left-child", 3, "acme/mono")
_RIGHT_CHILD = _plain("acme/right-child", 3, "acme/mono")


def _declared(mapping: dict[str, frozenset[str] | None]):
    return lambda dep: mapping[dep.get_unique_key()]


def test_single_candidate_places_without_manifest_evidence() -> None:
    parent = _plain("acme/solo", 1)
    child = _plain("acme/leaf", 2, "acme/solo")

    placement = place_legacy_lock_parents([parent, child])

    assert placement.parents == {"acme/leaf": parent}
    assert placement.unresolved == ()


def test_sibling_candidates_are_narrowed_by_declaring_manifest() -> None:
    reader = _declared(
        {
            "acme/mono/presets/left": frozenset({"acme/left-child"}),
            "acme/mono/presets/right": frozenset({"acme/right-child"}),
        }
    )

    placement = place_legacy_lock_parents([_ROOT, _LEFT, _RIGHT, _LEFT_CHILD, _RIGHT_CHILD], reader)

    assert placement.parents["acme/mono/presets/left"] is _ROOT
    assert placement.parents["acme/mono/presets/right"] is _ROOT
    assert placement.parents["acme/left-child"] is _LEFT
    assert placement.parents["acme/right-child"] is _RIGHT
    assert placement.unresolved == ()


def test_sibling_candidates_without_reader_stay_unresolved() -> None:
    placement = place_legacy_lock_parents([_ROOT, _LEFT, _RIGHT, _LEFT_CHILD])

    assert placement.unresolved == (_LEFT_CHILD,)


def test_several_declaring_siblings_stay_unresolved() -> None:
    both = frozenset({"acme/left-child"})
    reader = _declared({"acme/mono/presets/left": both, "acme/mono/presets/right": both})

    placement = place_legacy_lock_parents([_ROOT, _LEFT, _RIGHT, _LEFT_CHILD], reader)

    assert "acme/left-child" not in placement.parents
    assert placement.unresolved == (_LEFT_CHILD,)


def test_missing_manifest_evidence_on_any_sibling_stays_unresolved() -> None:
    reader = _declared(
        {"acme/mono/presets/left": frozenset({"acme/left-child"}), "acme/mono/presets/right": None}
    )

    placement = place_legacy_lock_parents([_ROOT, _LEFT, _RIGHT, _LEFT_CHILD], reader)

    assert placement.unresolved == (_LEFT_CHILD,)


def test_installed_manifest_children_reads_and_classifies(tmp_path: Path) -> None:
    modules = tmp_path / "apm_modules"
    left = modules / "acme/mono/presets/left"
    left.mkdir(parents=True)
    (left / "apm.yml").write_text(
        "name: left\nversion: 1.0.0\ndependencies:\n  apm:\n    - Acme/Left-Child\n",
        encoding="utf-8",
    )
    skill_only = modules / "acme/mono/presets/right"
    skill_only.mkdir(parents=True)
    (skill_only / "SKILL.md").write_text("# right\n", encoding="utf-8")

    reader = installed_manifest_children(modules)

    assert reader(_LEFT) == frozenset({"acme/left-child"})
    assert reader(_RIGHT) == frozenset()
    assert reader(_ROOT) is None
