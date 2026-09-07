"""Source-aware snapshots preserve exact matching and legacy name-set semantics."""

from unittest.mock import patch

import pytest

from apm_cli.models.dependency import DependencyReference
from apm_cli.policy.matcher import DependencyPolicyIndex, dependency_policy_name_matches

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "query",
    [
        "CONTOSO/TOOLS/skills/Review",
        "contoso/tools/skills/review",
        "gitlab.com/Contoso/Tools",
        "gitlab.com/contoso/tools",
        "./Packages/Tools",
        "./packages/tools",
        "unknown/package",
    ],
)
def test_index_matches_source_aware_scan_without_reprojecting(query: str) -> None:
    deps = [
        DependencyReference.parse("Contoso/Tools/skills/Review"),
        DependencyReference.parse("gitlab.com/Contoso/Tools"),
        DependencyReference.parse("./Packages/Tools"),
    ]
    expected = next(
        (
            dep.get_canonical_dependency_string().split("#", 1)[0]
            for dep in deps
            if dependency_policy_name_matches(dep, query)
        ),
        None,
    )
    index = DependencyPolicyIndex.from_dependencies(iter(deps))
    with patch.object(
        DependencyReference, "get_canonical_dependency_string", side_effect=AssertionError
    ):
        assert index.find_name(query) == expected


@pytest.mark.parametrize("reverse", [False, True])
def test_index_preserves_first_match_across_case_prefix_buckets(reverse: bool) -> None:
    """Registry identity can collide with a case-sensitive host; first still wins."""
    deps = [
        DependencyReference(repo_url="Contoso/Tools", host="gitlab.com"),
        DependencyReference(repo_url="Contoso/Tools", host="gitlab.com", source="registry"),
    ]
    if reverse:
        deps.reverse()
    query = "Contoso/Tools"
    assert all(dependency_policy_name_matches(dep, query) for dep in deps)
    index = DependencyPolicyIndex.from_dependencies(deps)
    assert index.find_name(query) == deps[0].get_canonical_dependency_string()


def test_supplied_name_snapshot_is_authoritative_byte_exact_and_independent() -> None:
    names = {"Contoso/Tools"}
    index = DependencyPolicyIndex.from_names(names)
    names.clear()
    assert index.find_name("Contoso/Tools") == "Contoso/Tools"
    assert index.find_name("contoso/tools") is None
    assert DependencyPolicyIndex.from_names(set()).find_name("Contoso/Tools") is None
