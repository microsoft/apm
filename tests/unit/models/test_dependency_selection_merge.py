"""Algebra of the dependency selection-merge owner (#3735)."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apm_cli.models.dependency.subsets import (
    EdgeSelection,
    SelectionMergeError,
    merge_edge_selections,
    union_selection_names,
)

pytestmark = pytest.mark.unit

_names = st.one_of(
    st.none(),
    st.lists(st.sampled_from(["alpha", "beta", "delta", "gamma", "x/y"]), min_size=1).map(
        lambda names: tuple(sorted(set(names)))
    ),
)
_edges = st.builds(
    EdgeSelection,
    parent_chain=st.tuples(st.sampled_from(["org/a", "org/b"])),
    skill_subset=_names,
    target_subset=_names,
)
_edge_lists = st.lists(_edges, min_size=1, max_size=6)
PROPERTY_PROFILE = settings(
    max_examples=50,
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _fields(edges: list[EdgeSelection]) -> tuple:
    merged = merge_edge_selections(edges)
    return merged.skill_subset, merged.target_subset


def _collapse(edges: list[EdgeSelection]) -> EdgeSelection:
    merged = merge_edge_selections(edges)
    return EdgeSelection((), merged.skill_subset, merged.target_subset)


@PROPERTY_PROFILE
@given(_edge_lists.flatmap(lambda edges: st.tuples(st.just(edges), st.permutations(edges))))
def test_merge_is_order_independent(pair: tuple) -> None:
    edges, shuffled = pair
    assert _fields(list(shuffled)) == _fields(edges)


@PROPERTY_PROFILE
@given(_edge_lists, _edge_lists, _edge_lists)
def test_merge_is_associative(a: list, b: list, c: list) -> None:
    left = _fields([_collapse([_collapse(a), *b]), *c])
    right = _fields([*a, _collapse([*b, *c])])
    assert left == right == _fields([*a, *b, *c])


@PROPERTY_PROFILE
@given(_edge_lists)
def test_merge_is_idempotent(edges: list[EdgeSelection]) -> None:
    assert _fields(edges + edges) == _fields(edges) == _fields([_collapse(edges)])


@PROPERTY_PROFILE
@given(_edge_lists)
def test_unrestricted_edge_absorbs(edges: list[EdgeSelection]) -> None:
    merged = merge_edge_selections([*edges, EdgeSelection(("org/z",), None, None)])
    assert merged.skill_subset is None
    assert merged.target_subset is None


@PROPERTY_PROFILE
@given(_edge_lists)
def test_merged_names_are_sorted_union(edges: list[EdgeSelection]) -> None:
    merged = merge_edge_selections(edges)
    if all(edge.skill_subset is not None for edge in edges):
        expected = sorted({name for edge in edges for name in edge.skill_subset})
        assert list(merged.skill_subset) == expected
    assert merged.contributors == tuple(edges)


def test_unsupported_mode_fails_clearly() -> None:
    edge = EdgeSelection(("org/a",), ("alpha",), None)
    object.__setattr__(edge, "mode", "exclude")
    with pytest.raises(SelectionMergeError, match="exclude"):
        merge_edge_selections([edge, EdgeSelection((), ("beta",), None)])


def test_explicitly_empty_selection_never_widens() -> None:
    empty = EdgeSelection(("org/a",), (), None)
    assert merge_edge_selections([empty, empty]).skill_subset == ()
    assert merge_edge_selections([empty, EdgeSelection((), ("beta",), None)]).skill_subset == (
        "beta",
    )


def test_union_names_ignores_unrestricted_inputs() -> None:
    assert union_selection_names(None, ("b",), ("a", "b")) == ("a", "b")
    assert union_selection_names(None, ()) is None


def test_alias_winner_and_divergence() -> None:
    merged = merge_edge_selections(
        [
            EdgeSelection(("org/a",), None, None, alias="bun-a"),
            EdgeSelection(("org/b",), None, None),
            EdgeSelection(("org/c",), None, None, alias="bun-b"),
        ]
    )
    assert merged.alias == "bun-a"
    assert merged.divergent_aliases == ("bun-b",)


def test_allow_insecure_requires_every_edge() -> None:
    allowed = EdgeSelection((), None, None, allow_insecure=True)
    denied = EdgeSelection(("org/a",), None, None)
    assert merge_edge_selections([allowed]).allow_insecure is True
    assert merge_edge_selections([allowed, denied]).allow_insecure is False
