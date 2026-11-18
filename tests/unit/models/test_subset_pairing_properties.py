"""Property tests for the exact per-edge skills x targets pairing owner.

``merge_edge_pairings`` in ``apm_cli.models.dependency.subsets`` is the single
owner of how several dependency edges' ``skills:`` and ``targets:`` selections
combine on one node (issue #3763, OpenAPM req-rs-018). These properties pin the
algebra: exactness against a brute-force oracle, order independence, merge
laws, and the lockfile encoding round trip.
"""

from __future__ import annotations

import pytest
from hypothesis import given, seed, settings
from hypothesis import strategies as st

from apm_cli.models.dependency.subsets import (
    SkillTargetSelection,
    SubsetPairing,
    merge_edge_pairings,
)

pytestmark = pytest.mark.unit

PROPERTY_SEED = 0x3763
PROFILE = settings(max_examples=200, deadline=None, database=None, print_blob=True)

SKILLS = ("alpha", "beta", "gamma", "delta")
TARGETS = ("claude", "copilot", "cursor")
# Names no edge can mention: they model skills/targets only "all" reaches.
UNSEEN_SKILL = "zeta"
UNSEEN_TARGET = "opencode"

axis_skills = st.one_of(st.none(), st.lists(st.sampled_from(SKILLS), min_size=1, unique=True))
axis_targets = st.one_of(st.none(), st.lists(st.sampled_from(TARGETS), min_size=1, unique=True))
edges_strategy = st.lists(st.tuples(axis_skills, axis_targets), min_size=1, max_size=6)


def _oracle(edges, skill: str, target: str) -> bool:
    """A skill reaches a target iff one edge selects both (None = all)."""
    return any(
        (skills is None or skill in skills) and (targets is None or target in targets)
        for skills, targets in edges
    )


def _deploys(selection: SkillTargetSelection, skill: str, target: str) -> bool:
    if not selection.selects_target(target):
        return False
    skills = selection.skills_for_target(target)
    return skills is None or skill in skills


@seed(PROPERTY_SEED)
@PROFILE
@given(edges=edges_strategy)
def test_pairing_is_exact_against_edge_oracle(edges) -> None:
    selection = merge_edge_pairings(edges)
    for target in (*TARGETS, UNSEEN_TARGET):
        assert selection.selects_target(target) == any(
            targets is None or target in targets for _, targets in edges
        )
        for skill in (*SKILLS, UNSEEN_SKILL):
            assert _deploys(selection, skill, target) == _oracle(edges, skill, target)


@seed(PROPERTY_SEED)
@PROFILE
@given(data=st.data(), edges=edges_strategy)
def test_pairing_is_independent_of_edge_order_and_duplication(data, edges) -> None:
    shuffled = data.draw(st.permutations(edges))
    expected = merge_edge_pairings(edges)
    assert merge_edge_pairings(shuffled) == expected
    assert merge_edge_pairings(edges + shuffled) == expected
    assert expected.to_lock_records() == merge_edge_pairings(shuffled).to_lock_records()


@seed(PROPERTY_SEED)
@PROFILE
@given(a=edges_strategy, b=edges_strategy, c=edges_strategy)
def test_merge_is_commutative_associative_and_idempotent(a, b, c) -> None:
    x, y, z = (merge_edge_pairings(edges) for edges in (a, b, c))
    assert x.merge(y) == y.merge(x)
    assert x.merge(y).merge(z) == x.merge(y.merge(z))
    assert x.merge(x) == x
    assert x.merge(y) == merge_edge_pairings(a + b)


@seed(PROPERTY_SEED)
@PROFILE
@given(edges=edges_strategy)
def test_flat_fields_are_the_per_field_union(edges) -> None:
    """The flat projections keep the 0.36.1 lockfile fields unchanged."""
    selection = merge_edge_pairings(edges)
    skills = None if any(s is None for s, _ in edges) else sorted({n for s, _ in edges for n in s})
    targets = None if any(t is None for _, t in edges) else sorted({n for _, t in edges for n in t})
    assert selection.skill_subset == (None if skills is None else tuple(skills))
    assert selection.target_subset == (None if targets is None else tuple(targets))


@seed(PROPERTY_SEED)
@PROFILE
@given(edges=edges_strategy)
def test_lock_records_round_trip_and_are_omitted_for_products(edges) -> None:
    selection = merge_edge_pairings(edges)
    records = selection.to_lock_records()
    assert (records is None) == selection.is_product
    decoded = SkillTargetSelection.from_lock_fields(
        selection.skill_subset, selection.target_subset, records
    )
    assert decoded == selection
    product = merge_edge_pairings([(selection.skill_subset, selection.target_subset)])
    assert (product == selection) == selection.is_product


def test_divergent_diamond_does_not_cross_harnesses() -> None:
    selection = merge_edge_pairings([(["alpha"], ["claude"]), (["gamma"], ["copilot"])])
    assert selection.skills_for_target("claude") == ("alpha",)
    assert selection.skills_for_target("copilot") == ("gamma",)
    assert not selection.selects_target("cursor")
    assert selection.to_lock_records() == [
        {"skills": ["alpha"], "targets": ["claude"]},
        {"skills": ["gamma"], "targets": ["copilot"]},
    ]


def test_all_absorbs_per_edge_and_per_axis() -> None:
    selection = merge_edge_pairings([(None, ["claude"]), (["alpha"], None)])
    assert selection.skills_for_target("claude") is None
    assert selection.skills_for_target("cursor") == ("alpha",)
    assert selection.to_lock_records() == [{"skills": ["alpha"]}, {"targets": ["claude"]}]


def test_empty_axis_means_all_like_lockfile_and_reference_conventions() -> None:
    assert merge_edge_pairings([([], [])]) == merge_edge_pairings([(None, None)])


def test_unselected_target_is_not_read_as_all_skills() -> None:
    selection = merge_edge_pairings([(["alpha"], ["claude"])])
    with pytest.raises(KeyError):
        selection.skills_for_target("copilot")


@pytest.mark.parametrize(
    ("records", "message"),
    [
        ([], "non-empty list"),
        ("skills", "non-empty list"),
        (["claude"], "must be a mapping"),
        ([{"skills": ["alpha"], "harness": ["claude"]}], "Unknown"),
        ([{"skills": []}], "omit it to select all"),
        ([{"targets": "claude"}], "omit it to select all"),
        ([{"skills": [""]}], "non-empty string"),
    ],
)
def test_malformed_lock_records_fail_closed(records, message) -> None:
    with pytest.raises(ValueError, match=message):
        SkillTargetSelection.from_lock_fields(["alpha"], ["claude"], records)


def test_lock_records_that_disagree_with_flat_fields_fail_closed() -> None:
    records = [{"skills": ["alpha"], "targets": ["claude"]}, {"skills": ["gamma"]}]
    with pytest.raises(ValueError, match="disagree"):
        SkillTargetSelection.from_lock_fields(["alpha"], ["claude"], records)


def test_lock_without_records_replays_the_flat_product() -> None:
    """Locks written before exact pairing decode to what those writers deployed."""
    decoded = SkillTargetSelection.from_lock_fields(["alpha", "gamma"], ["claude", "copilot"])
    assert decoded.is_product
    assert decoded.skills_for_target("copilot") == ("alpha", "gamma")


def test_non_canonical_construction_is_rejected() -> None:
    with pytest.raises(ValueError, match="canonical"):
        SkillTargetSelection(
            (SubsetPairing(("alpha",), ("copilot",)), SubsetPairing(("alpha",), ("claude",)))
        )


@pytest.mark.parametrize(
    "pairing",
    [
        SubsetPairing((), ("claude",)),
        SubsetPairing((" alpha",), ("claude",)),
        SubsetPairing(("alpha",), ("",)),
        SubsetPairing(["alpha"], ("claude",)),
    ],
)
def test_direct_construction_rejects_axes_that_change_meaning_on_replay(pairing) -> None:
    """An empty, blank or padded axis would decode differently from the lock."""
    with pytest.raises(ValueError, match="non-empty"):
        SkillTargetSelection((pairing,))


def test_explicitly_empty_edge_pairs_nothing_and_never_widens() -> None:
    """An explicitly empty axis selects nothing, unlike None (all)."""
    from apm_cli.models.dependency.subsets import EdgeSelection, merge_edge_selections

    empty = EdgeSelection(("org/a",), (), ("claude",))
    node = merge_edge_selections([empty, EdgeSelection((), ("beta",), ("copilot",))])
    assert node.skill_subset == ("beta",)
    assert not node.pairing.selects_target("claude")
    assert node.pairing.skills_for_target("copilot") == ("beta",)

    only_empty = merge_edge_selections([EdgeSelection(("org/a",), (), None)] * 2)
    assert only_empty.pairing is None
    assert only_empty.skill_subset == ()
    assert only_empty.subset_pairings is None
