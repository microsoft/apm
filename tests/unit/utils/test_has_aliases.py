"""Direct unit tests for _BoundedSafeLoader._has_aliases.

Covers edge cases that the integration tests in tests/red_team/parser/
exercise only indirectly: empty mappings, scalar-only documents, a single
cross-edge alias, a back-edge (self-referential anchor / cycle), and a
tree with shared scalar leaf objects (which PyYAML reuses for duplicate
identical scalars in some configurations).
"""

from __future__ import annotations

import pytest
import yaml

from apm_cli.utils.yaml_io import _BoundedSafeLoader

_has_aliases = _BoundedSafeLoader._has_aliases

# ---------------------------------------------------------------------------
# Helpers to build composed node graphs directly without parsing
# ---------------------------------------------------------------------------
_STR = "tag:yaml.org,2002:str"
_MAP = "tag:yaml.org,2002:map"
_SEQ = "tag:yaml.org,2002:seq"


def _scalar(value: str) -> yaml.nodes.ScalarNode:
    return yaml.nodes.ScalarNode(_STR, value)


def _mapping(*pairs: tuple[yaml.nodes.Node, yaml.nodes.Node]) -> yaml.nodes.MappingNode:
    return yaml.nodes.MappingNode(_MAP, list(pairs))


def _sequence(*children: yaml.nodes.Node) -> yaml.nodes.SequenceNode:
    return yaml.nodes.SequenceNode(_SEQ, list(children))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_mapping_returns_false() -> None:
    """An empty mapping has no aliases."""
    assert _has_aliases(_mapping()) is False


def test_scalar_only_returns_false() -> None:
    """A bare scalar document cannot have aliases."""
    assert _has_aliases(_scalar("hello")) is False


def test_plain_tree_mapping_returns_false() -> None:
    """A non-trivial mapping with all distinct node objects returns False."""
    root = _mapping(
        (_scalar("a"), _scalar("1")),
        (_scalar("b"), _scalar("2")),
    )
    assert _has_aliases(root) is False


def test_sequence_of_scalars_returns_false() -> None:
    """A sequence with all distinct scalar children returns False."""
    root = _sequence(_scalar("x"), _scalar("y"), _scalar("z"))
    assert _has_aliases(root) is False


def test_single_alias_cross_edge_returns_true() -> None:
    """A node referenced from two different parents is a cross-edge alias."""
    shared = _scalar("shared_value")
    root = _mapping(
        (_scalar("key1"), shared),
        (_scalar("key2"), shared),  # same object -> alias
    )
    assert _has_aliases(root) is True


def test_alias_in_sequence_returns_true() -> None:
    """Alias detection works inside SequenceNode children."""
    shared = _scalar("v")
    root = _sequence(shared, _scalar("other"), shared)
    assert _has_aliases(root) is True


def test_alias_in_nested_mapping_returns_true() -> None:
    """Alias reachable through nested structure is still detected."""
    shared = _mapping((_scalar("k"), _scalar("v")))
    child_a = _mapping((_scalar("x"), shared))
    child_b = _mapping((_scalar("y"), shared))  # same shared object
    root = _mapping((_scalar("a"), child_a), (_scalar("b"), child_b))
    assert _has_aliases(root) is True


def test_self_referential_cycle_back_edge_returns_true() -> None:
    """A self-referential sequence (cycle) is a back-edge alias."""
    seq = _sequence(_scalar("item"))
    # Make seq refer to itself: seq.value[0] replaced by seq itself.
    seq.value[0] = seq  # type: ignore[index]
    assert _has_aliases(seq) is True


@pytest.mark.parametrize(
    "levels",
    [1, 5, 50, 1100],
    ids=["depth-1", "depth-5", "depth-50", "depth-1100-exceeds-recursion-limit"],
)
def test_deep_tree_no_aliases_returns_false(levels: int) -> None:
    """Deep pure-tree documents return False regardless of depth.

    The depth-1100 case specifically guards the iterative implementation
    against the Python default recursion limit (~1000 frames).  A recursive
    implementation would raise RecursionError here; the iterative version
    must return False cleanly.
    """
    # Build a chain: {k: {k: {k: ...}}} of the given depth.
    node: yaml.nodes.Node = _scalar("leaf")
    for _ in range(levels):
        node = _mapping((_scalar("k"), node))
    assert _has_aliases(node) is False
