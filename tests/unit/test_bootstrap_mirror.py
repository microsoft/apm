"""Unit tests for enterprise bootstrap mirror helper functions."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from apm_cli.bootstrap_mirror import append_url_path, get_env_url


def test_get_env_url_strips_quotes_whitespace_and_trailing_slash() -> None:
    """Mirror URL env values are normalized before callers use them."""
    with patch.dict(os.environ, {"APM_TEST_URL": "  'https://mirror.corp.example/path/'  "}):
        assert get_env_url("APM_TEST_URL") == "https://mirror.corp.example/path"


@pytest.mark.parametrize("value", ["", "   "])
def test_get_env_url_returns_none_for_empty_values(value: str) -> None:
    """Unset or blank mirror URL env values are treated as absent."""
    with patch.dict(os.environ, {"APM_TEST_URL": value}):
        assert get_env_url("APM_TEST_URL") is None


def test_append_url_path_joins_base_and_strips_slashes() -> None:
    """URL path joining avoids duplicate slashes across base and parts."""
    assert (
        append_url_path("https://mirror.corp.example/root/", "/v1.2.3/", "asset.zip")
        == "https://mirror.corp.example/root/v1.2.3/asset.zip"
    )


@pytest.mark.parametrize("part", [".", "..", "nested/../asset.zip", "./asset.zip"])
def test_append_url_path_rejects_dot_segments(part: str) -> None:
    """Mirror URL path parts reject dot segments before joining."""
    with pytest.raises(ValueError, match="dot segments"):
        append_url_path("https://mirror.corp.example/root", part)
