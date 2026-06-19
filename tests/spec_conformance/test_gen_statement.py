"""gen_statement determinism + ASCII self-test."""

from __future__ import annotations

import subprocess
import sys

from tests.spec_conformance._manifest import REPO_ROOT
from tests.spec_conformance.gen_statement import (
    CONFORMANCE_JSON,
    CONFORMANCE_MD,
    GENERATOR,
    SPEC_VERSION,
)


def _run_gen() -> None:
    res = subprocess.run(
        [sys.executable, "-m", "tests.spec_conformance.gen_statement"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr


def test_gen_statement_is_deterministic():
    _run_gen()
    first_json = CONFORMANCE_JSON.read_bytes()
    first_md = CONFORMANCE_MD.read_bytes()
    _run_gen()
    second_json = CONFORMANCE_JSON.read_bytes()
    second_md = CONFORMANCE_MD.read_bytes()
    assert first_json == second_json, "CONFORMANCE.json is not deterministic"
    assert first_md == second_md, "CONFORMANCE.md is not deterministic"


def test_gen_statement_emits_ascii_only():
    _run_gen()
    json_bytes = CONFORMANCE_JSON.read_bytes()
    md_bytes = CONFORMANCE_MD.read_bytes()
    for label, data in (("JSON", json_bytes), ("MD", md_bytes)):
        for i, b in enumerate(data):
            assert b in (0x09, 0x0A) or 0x20 <= b <= 0x7E, (
                f"non-ASCII byte 0x{b:02x} at offset {i} in {label}"
            )


def test_gen_statement_md_advertises_spec_version_and_generator():
    _run_gen()
    md = CONFORMANCE_MD.read_text(encoding="ascii")
    assert SPEC_VERSION in md, f"missing '{SPEC_VERSION}' in CONFORMANCE.md"
    assert GENERATOR in md, f"missing '{GENERATOR}' in CONFORMANCE.md"


def test_gen_statement_md_contains_honesty_phrase():
    """req-cf-002 binding: the literal honesty disclaimer MUST appear."""
    _run_gen()
    md = CONFORMANCE_MD.read_text(encoding="ascii")
    assert "NO automated CI detector" in md, (
        "CONFORMANCE.md MUST carry the literal phrase 'NO automated CI detector' (honesty contract)"
    )
