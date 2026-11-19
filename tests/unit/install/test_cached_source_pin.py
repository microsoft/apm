"""Warm-cache installs carry the locked commit as the source pin (#3388).

A cached package vouches for a full commit only when its ``.apm-pin`` marker
agrees; short, branch, missing or inconsistent identities fall back to
``cached`` so nothing downstream records an identity the bytes cannot prove.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.install.cache_pin import is_full_source_pin
from apm_cli.install.sources import CachedDependencySource

_LOCKED_SHA = "f1b2ba49d18a3de11f87d8e6dc672dfdaadbff85"


def _source(install_path: Path, *, fetched: bool = False) -> SimpleNamespace:
    return SimpleNamespace(install_path=install_path, fetched_this_run=fetched)


@pytest.mark.parametrize(
    ("locked_commit", "pin_commit", "expected"),
    [
        pytest.param(_LOCKED_SHA, _LOCKED_SHA, _LOCKED_SHA, id="locked-sha"),
        pytest.param("f1b2ba4", "f1b2ba4", "cached", id="short-sha"),
        pytest.param("main", "main", "cached", id="branch-name"),
        pytest.param(_LOCKED_SHA, None, "cached", id="missing-pin"),
        pytest.param(_LOCKED_SHA, "a" * 40, "cached", id="inconsistent-pin"),
    ],
)
def test_warm_cache_uses_locked_source_pin_only_when_marker_agrees(
    tmp_path: Path, locked_commit: str, pin_commit: str | None, expected: str
) -> None:
    if pin_commit is not None:
        (tmp_path / ".apm-pin").write_text(
            json.dumps({"schema_version": 1, "resolved_commit": pin_commit}), encoding="utf-8"
        )
    pin = CachedDependencySource._cached_source_pin(_source(tmp_path), locked_commit)
    assert pin == expected


def test_bytes_fetched_this_run_vouch_for_their_commit_without_marker(tmp_path: Path) -> None:
    pin = CachedDependencySource._cached_source_pin(_source(tmp_path, fetched=True), _LOCKED_SHA)
    assert pin == _LOCKED_SHA


@pytest.mark.parametrize(
    ("value", "expected"),
    [("a" * 40, True), ("b" * 64, True), ("A" * 40, False), ("a" * 39, False), (None, False)],
)
def test_full_source_pin_accepts_only_full_lowercase_commit_ids(value, expected) -> None:
    assert is_full_source_pin(value) is expected
