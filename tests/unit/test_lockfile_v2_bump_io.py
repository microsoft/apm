"""Tests for the lockfile v1->v2 opportunistic bump (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import (
    LockFile,
)

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-read-only-lockfile-path")]


@pytest.mark.parametrize(
    ("fixture_name", "expected_size", "expected_sha256"),
    [
        (
            "v1-git-only.yml",
            889,
            "75b7d3b5d6f310e481131f18209103c3f4559933bef70ec5fd087b60af3cc26e",
        ),
        (
            "v2-with-registry.yml",
            1236,
            "3f3d9f509c0a07971aa597d223dcdd1afb48df62025fc3f9ebb14b61621ad056",
        ),
    ],
)
def test_pre_v3_lockfile_canonical_bytes_remain_stable(
    fixture_name: str, expected_size: int, expected_sha256: str
) -> None:
    fixture = Path("tests/fixtures/spec-conformance/lockfile") / fixture_name
    rendered = LockFile.from_yaml(fixture.read_text(encoding="utf-8")).to_yaml().encode("utf-8")

    assert len(rendered) == expected_size
    assert hashlib.sha256(rendered).hexdigest() == expected_sha256


# ───────────────────────────────────────────────────────────────────────────
# Schema bump (invariant §2.1.4)
# ───────────────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────────────
# Field round-trip
# ───────────────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────────────
# v1 lockfile compatibility (§2.1.4 read invariant)
# ───────────────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────────────
# from_dependency_ref + RegistryResolution
# ───────────────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────────────
# to_dependency_ref restores source field
# ───────────────────────────────────────────────────────────────────────────


# ---------------------------------------------------------------------------
# Issue #1888 (d): no v2 bump for name-carrying deps
# ---------------------------------------------------------------------------
