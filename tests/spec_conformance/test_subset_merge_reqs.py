"""Per-edge subset merging conformance (OpenAPM req-rs-018, Section 7.4.1)."""

from __future__ import annotations

import itertools

import pytest

from apm_cli.models.dependency.subsets import SkillTargetSelection, merge_edge_pairings

pytestmark = pytest.mark.unit

# Section 7.4.1 informative example: a -> bundle [alpha]/[claude], b -> bundle [gamma]/[copilot].
DIVERGENT_DIAMOND = [(["alpha"], ["claude"]), (["gamma"], ["copilot"])]


def _deploys(selection: SkillTargetSelection, skill: str, target: str) -> bool:
    if not selection.selects_target(target):
        return False
    skills = selection.skills_for_target(target)
    return skills is None or skill in skills


@pytest.mark.req("req-rs-018")
def test_exact_pairing_never_crosses_edges() -> None:
    """Clause 1: a skill reaches a target only when one edge pairs them."""
    selection = merge_edge_pairings(DIVERGENT_DIAMOND)
    assert _deploys(selection, "alpha", "claude")
    assert _deploys(selection, "gamma", "copilot")
    assert not _deploys(selection, "alpha", "copilot")
    assert not _deploys(selection, "gamma", "claude")


@pytest.mark.req("req-rs-018")
@pytest.mark.parametrize(
    ("edges", "target", "expected"),
    [
        ([(None, ["claude"]), (["alpha"], ["claude"])], "claude", None),
        ([(["alpha"], None), (["gamma"], ["copilot"])], "cursor", ("alpha",)),
        ([(["alpha"], None), (["gamma"], ["copilot"])], "copilot", ("alpha", "gamma")),
    ],
)
def test_all_absorbs_per_edge_and_axis(edges, target, expected) -> None:
    """Clause 2: an omitted axis selects all for that edge; narrower edges cannot reduce it."""
    assert merge_edge_pairings(edges).skills_for_target(target) == expected


@pytest.mark.req("req-rs-018")
def test_selection_and_lock_record_are_order_independent() -> None:
    """Clause 3: every declaration order yields the same selection and record."""
    edges = [*DIVERGENT_DIAMOND, (["beta"], ["claude", "cursor"]), (None, ["cursor"])]
    outcomes = {
        repr(merge_edge_pairings(order).to_lock_records())
        for order in itertools.permutations(edges)
    }
    assert len(outcomes) == 1


@pytest.mark.req("req-rs-018")
def test_lock_record_matches_section_7_4_1_example() -> None:
    """Clause 4 and the canonical form: unions plus subset_pairings when over-selecting."""
    selection = merge_edge_pairings(DIVERGENT_DIAMOND)
    assert selection.skill_subset == ("alpha", "gamma")
    assert selection.target_subset == ("claude", "copilot")
    assert selection.to_lock_records() == [
        {"skills": ["alpha"], "targets": ["claude"]},
        {"skills": ["gamma"], "targets": ["copilot"]},
    ]


@pytest.mark.req("req-rs-018")
def test_exact_product_omits_subset_pairings() -> None:
    """Clause 4: writers omit subset_pairings when the flat product is exact."""
    selection = merge_edge_pairings([(["alpha"], ["claude"]), (["gamma"], ["claude"])])
    assert selection.to_lock_records() is None


@pytest.mark.req("req-rs-018")
@pytest.mark.parametrize(
    "records",
    [
        [{"skills": ["alpha"], "targets": ["claude"]}],
        [{"skills": ["alpha"], "targets": ["claude"]}, {"skills": ["delta"]}],
        [{"skills": []}],
        [{"harness": ["claude"]}],
    ],
)
def test_inconsistent_or_malformed_pairings_are_invalid_lock_state(records) -> None:
    """Clause 5: malformed records, or records disagreeing with the unions, are invalid."""
    with pytest.raises(ValueError):
        SkillTargetSelection.from_lock_fields(["alpha", "gamma"], ["claude", "copilot"], records)


@pytest.mark.req("req-rs-018")
def test_entry_without_pairings_denotes_the_flat_product() -> None:
    """Clause 5: an entry without subset_pairings is the product of its flat fields."""
    selection = SkillTargetSelection.from_lock_fields(["alpha", "gamma"], ["claude", "copilot"])
    assert _deploys(selection, "alpha", "copilot")
    assert selection != merge_edge_pairings(DIVERGENT_DIAMOND)
