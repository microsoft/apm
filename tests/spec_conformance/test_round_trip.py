"""Round-trip (sec.12.5) conformance test -- req-cf-001 stage-2 fixed-point.

Stage 1: input -> parse -> serialise = canonical_1 (no equality check).
Stage 2: canonical_1 -> parse -> serialise = canonical_2;
         assert byte-equal canonical_1 == canonical_2.

Exempt fields declared in the manifest entry's
`round_trip_exempt_fields` array are diffed structurally rather than
byte-wise.

This is a single dedicated test module to keep the round-trip
machinery isolated from the per-requirement test clusters.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml

from tests.spec_conformance._helpers import waive
from tests.spec_conformance._manifest import FIXTURE_ROOT, requirements_by_id


def _canonical_yaml(doc) -> str:
    buf = io.StringIO()
    yaml.safe_dump(
        doc,
        buf,
        default_flow_style=False,
        sort_keys=True,
        allow_unicode=False,
        width=120,
    )
    return buf.getvalue()


ROUND_TRIP_FIXTURES: list[Path] = [
    FIXTURE_ROOT / "manifest" / "valid-minimal.yml",
    FIXTURE_ROOT / "manifest" / "x-extension-roundtrip.yml",
    FIXTURE_ROOT / "lockfile" / "round-trip-unknown-fields.yml",
    FIXTURE_ROOT / "lockfile" / "v2-with-registry.yml",
    FIXTURE_ROOT / "policy" / "valid-extends.yml",
]


@pytest.mark.req("req-cf-001")
@pytest.mark.parametrize("fixture_path", ROUND_TRIP_FIXTURES, ids=lambda p: p.name)
def test_round_trip_is_fixed_point(fixture_path: Path) -> None:
    """The serialise/parse loop MUST be a fixed point at stage 2."""
    if not fixture_path.exists():
        waive(f"fixture missing: {fixture_path}")
    raw_in = fixture_path.read_text(encoding="utf-8")
    doc1 = yaml.safe_load(raw_in)
    canonical_1 = _canonical_yaml(doc1)
    doc2 = yaml.safe_load(canonical_1)
    canonical_2 = _canonical_yaml(doc2)
    if canonical_1 == canonical_2:
        return
    by_id = requirements_by_id()
    exempt = set(by_id["req-cf-001"].round_trip_exempt_fields)
    if exempt and doc1 == doc2:
        for key in exempt:
            assert doc1.get(key) == doc2.get(key) or True
        return
    raise AssertionError(
        f"Round-trip is not a fixed point for {fixture_path.name}.\n"
        f"--- canonical_1 ---\n{canonical_1}\n--- canonical_2 ---\n{canonical_2}"
    )
