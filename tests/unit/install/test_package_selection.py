"""Pure selected-package identity normalization and raw-selector fallback."""

import pytest

from apm_cli.install.package_selection import selected_dependency_identity, selected_dependency_keys
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
