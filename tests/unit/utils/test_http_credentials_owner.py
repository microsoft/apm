"""credential-origin-scope owner: path and base-path edges of origin binding (#4121)."""

from __future__ import annotations

import pytest

from apm_cli.utils.http_credentials import (
    REFUSED_OUTSIDE_BASE_PATH,
    _comparable_paths,
    origin_bound_refusal,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-credential-origin-scope")]


def test_comparable_paths_roots_empty_and_relative_paths() -> None:

    assert _comparable_paths("") == ("/", "/")
    assert _comparable_paths("a/b") == ("/a/b", "/a/b")
    # A relative path must be rooted before dot segments are removed, so
    # ``..`` can never climb above the root.
    assert _comparable_paths("a/../..") == ("/", "/")


def test_base_path_keeps_trailing_characters_other_than_slash() -> None:

    assert (
        origin_bound_refusal("https://h.example/api/v", base_url="https://h.example/apiX")
        == REFUSED_OUTSIDE_BASE_PATH
    )
    assert (
        origin_bound_refusal("https://h.example/apiX/v", base_url="https://h.example/apiX/") is None
    )
