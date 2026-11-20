"""Pure selected-package identity normalization and raw-selector fallback."""

from types import SimpleNamespace

import pytest

from apm_cli.install.package_selection import (
    selected_dependency_identity,
    selected_dependency_keys,
    selected_subtree_dependencies,
    unlocked_local_dependencies,
)
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("error", [ValueError, TypeError])
def test_selected_keys_preserve_failed_selector_without_losing_valid_keys(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    original_parse = DependencyReference.parse

    def parse(spec: str) -> DependencyReference:
        if spec == "INVALID_REF_NO_SLASH":
            raise error("invalid selector")
        return original_parse(spec)

    monkeypatch.setattr(DependencyReference, "parse", parse)
    assert selected_dependency_keys(["org/pkg#main", "INVALID_REF_NO_SLASH"]) == {
        "org/pkg",
        "INVALID_REF_NO_SLASH",
    }


@pytest.mark.parametrize("ref", ["main", "release/hooks", "v1.2.3"])
def test_selected_keys_and_identity_preserve_interpreted_versioned_paths(ref: str) -> None:
    dependency = DependencyReference.parse_from_dict(
        {"git": "https://github.com/acme/hooks", "path": "plugins/hook-1.2.3", "ref": ref}
    )
    selector = dependency.to_canonical()
    assert selected_dependency_keys([selector, "other/package#main"], [dependency]) == {
        dependency.get_unique_key(),
        "other/package",
    }
    assert selected_dependency_identity(selector, [dependency]) == dependency.get_identity()


@pytest.mark.parametrize(
    ("locked_keys", "expected"),
    [((), ["./local", "./grandchild"]), (("./local",), [])],
    ids=["unlocked-local", "locked-local"],
)
def test_unlocked_local_dependencies_complete_a_partial_install(
    locked_keys: tuple[str, ...], expected: list[str]
) -> None:
    """A partial install also locks direct local deps the lock lacks (#4198)."""
    root = DependencyReference.parse("owner/root")
    local = DependencyReference.parse_from_dict({"path": "./local"})
    grandchild = DependencyReference.parse_from_dict({"path": "./grandchild"})
    nested = DependencyReference.parse_from_dict({"path": "./nested"})
    grandchild_node = SimpleNamespace(dependency_ref=grandchild, children=[], depth=2)
    nodes = [
        SimpleNamespace(dependency_ref=root, children=[], depth=1),
        SimpleNamespace(dependency_ref=local, children=[grandchild_node], depth=1),
        grandchild_node,
        SimpleNamespace(dependency_ref=nested, children=[], depth=2),
    ]
    tree = SimpleNamespace(nodes=dict(enumerate(nodes)))
    refs = [root, local, grandchild, nested]
    assert selected_subtree_dependencies(["owner/root"], tree, refs) == [root]
    extra = unlocked_local_dependencies(tree, refs, locked_keys)
    assert [dependency.get_unique_key() for dependency in extra] == expected
