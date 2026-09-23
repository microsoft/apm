"""Source-aware snapshots preserve exact matching and legacy name-set semantics."""

from unittest.mock import patch

import pytest

from apm_cli.models.dependency import DependencyReference
from apm_cli.models.dependency.identity import normalize_package_policy_identity
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


@pytest.mark.parametrize("count", [100, 1000])
def test_index_projection_scales_with_dependencies_not_required_lookups(count: int) -> None:
    deps = [
        DependencyReference(
            repo_url=f"Contoso/Tool{position}",
            host="github.com" if position % 2 else "gitlab.com",
        )
        for position in range(count)
    ]
    canonical = DependencyReference.get_canonical_dependency_string
    with patch.object(
        DependencyReference, "get_canonical_dependency_string", autospec=True, side_effect=canonical
    ) as project:
        index = DependencyPolicyIndex.from_dependencies(deps)
        assert project.call_count == count
        with patch(
            "apm_cli.policy.matcher.normalize_package_policy_identity",
            wraps=normalize_package_policy_identity,
        ) as normalize:
            for _ in range(20):
                assert index.find_name("CONTOSO/TOOL1") == "contoso/tool1"
            assert normalize.call_count == 40
        assert project.call_count == count


def test_policy_normalization_cache_is_bounded_and_prefix_sensitive() -> None:
    normalize_package_policy_identity.cache_clear()
    try:
        for _ in range(10):
            assert (
                normalize_package_policy_identity(
                    "Contoso/Tools/KeepCase", case_insensitive_prefix_segments=2
                )
                == "contoso/tools/KeepCase"
            )
        assert (
            normalize_package_policy_identity(
                "Contoso/Tools/KeepCase", case_insensitive_prefix_segments=0
            )
            == "Contoso/Tools/KeepCase"
        )
        info = normalize_package_policy_identity.cache_info()
        assert (info.hits, info.misses, info.maxsize) == (9, 2, 512)
        for position in range(600):
            normalize_package_policy_identity(
                f"Contoso/Tool{position}", case_insensitive_prefix_segments=2
            )
        assert normalize_package_policy_identity.cache_info().currsize == 512
    finally:
        normalize_package_policy_identity.cache_clear()
