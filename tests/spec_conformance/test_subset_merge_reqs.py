"""Per-edge subset merging conformance (OpenAPM req-rs-018, Section 7.4.1)."""

from __future__ import annotations

import itertools

import pytest
from jsonschema import Draft202012Validator

from apm_cli.models.dependency.subsets import SkillTargetSelection, merge_edge_pairings
from tests.spec_conformance._helpers import load_schema

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


@pytest.mark.req("req-rs-018")
def test_proposed_lock_schema_types_the_pairing_fields_only() -> None:
    """The 0.1.60 resource adds target_subset and subset_pairings to the 0.1.46 contract."""
    previous = load_schema("lockfile-v0.1.46.schema.json")
    proposed = load_schema("lockfile-v0.1.60.schema.json")
    Draft202012Validator.check_schema(proposed)
    assert proposed["$id"] != previous["$id"]
    entry = proposed["$defs"]["entry"]["properties"]
    assert entry.pop("target_subset") == entry["skill_subset"]
    entry.pop("subset_pairings")
    proposed["$id"], proposed["title"] = previous["$id"], previous["title"]
    assert proposed == previous


@pytest.mark.req("req-rs-018")
@pytest.mark.parametrize(
    ("records", "valid"),
    [
        (merge_edge_pairings(DIVERGENT_DIAMOND).to_lock_records(), True),
        (merge_edge_pairings([(None, ["claude"]), (["alpha"], None)]).to_lock_records(), True),
        ([{"skills": ["alpha"], "targets": ["claude"]}], False),
        ([{"skills": ["alpha"], "x-note": "v"}, {"targets": ["claude"]}], False),
        ([{"skills": []}, {"targets": ["claude"]}], False),
    ],
)
def test_proposed_lock_schema_matches_canonical_writer_output(records, valid) -> None:
    """Writer output validates; single products, extension keys and empty axes do not."""
    validator = Draft202012Validator(load_schema("lockfile-v0.1.60.schema.json"))
    lock = {
        "lockfile_version": "2",
        "dependencies": [{"repo_url": "owner/bundle", "subset_pairings": records}],
    }
    assert (list(validator.iter_errors(lock)) == []) is valid


def _locked_bundle(**fields):
    from apm_cli.deps.lockfile import LockedDependency

    return LockedDependency(repo_url="owner/bundle", **fields)


@pytest.mark.req("req-rs-018")
def test_lock_entry_records_and_replays_the_exact_pairing() -> None:
    """Clause 4: the lock codec writes subset_pairings and replays it to deploy."""
    from apm_cli.deps.lockfile import LockedDependency
    from apm_cli.models.dependency.reference import DependencyReference

    pairing = merge_edge_pairings(DIVERGENT_DIAMOND)
    ref = DependencyReference(
        repo_url="owner/bundle",
        host="github.com",
        skill_subset=list(pairing.skill_subset),
        target_subset=list(pairing.target_subset),
        subset_pairings=pairing,
    )
    entry = LockedDependency.from_dependency_ref(ref, "a" * 40, 1, None).to_dict()
    assert entry["subset_pairings"] == pairing.to_lock_records()
    assert LockedDependency.from_dict(entry).to_dependency_ref().subset_pairings == pairing


@pytest.mark.req("req-rs-018")
def test_frozen_rejects_changed_or_invalid_selections_before_deploy() -> None:
    """Clauses 5 and 6: frozen names the dependency for a changed or invalid record."""
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.service import frozen_selection_changes
    from apm_cli.models.dependency.reference import DependencyReference

    pairing = merge_edge_pairings(DIVERGENT_DIAMOND)
    ref = DependencyReference(
        repo_url="owner/bundle",
        host="github.com",
        skill_subset=list(pairing.skill_subset),
        target_subset=list(pairing.target_subset),
        subset_pairings=pairing,
    )
    flat = _locked_bundle(skill_subset=["alpha", "gamma"], target_subset=["claude", "copilot"])
    key = flat.get_unique_key()
    assert ref.get_unique_key() == key

    product_lock = LockFile(dependencies={key: flat})
    assert "selection for owner/bundle changed" in frozen_selection_changes([ref], product_lock)[0]

    flat.subset_pairings = [{"skills": ["alpha"], "targets": ["claude"]}]
    assert "invalid" in frozen_selection_changes([ref], product_lock)[0]

    flat.subset_pairings = pairing.to_lock_records()
    assert frozen_selection_changes([ref], product_lock) == []


@pytest.mark.req("req-rs-018")
def test_frozen_replay_reads_a_recorded_vscode_target_as_copilot() -> None:
    """0.1.61: a lock that recorded the deprecated `vscode` alias is not a selection change."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.install.service import frozen_selection_changes
    from apm_cli.models.dependency.reference import DependencyReference

    ref = DependencyReference(
        repo_url="owner/bundle", host="github.com", target_subset=["claude", "copilot"]
    )
    legacy = LockedDependency.from_dict(
        {"repo_url": "owner/bundle", "target_subset": ["claude", "vscode"]}
    )
    assert legacy.target_subset == ["claude", "copilot"]
    lock = LockFile(dependencies={legacy.get_unique_key(): legacy})
    assert frozen_selection_changes([ref], lock) == []
