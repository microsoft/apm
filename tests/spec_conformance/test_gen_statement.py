"""gen_statement determinism + ASCII self-test."""

from __future__ import annotations

import json
import subprocess
import sys

from tests.spec_conformance._manifest import REPO_ROOT
from tests.spec_conformance.gen_statement import (
    CONFORMANCE_JSON,
    CONFORMANCE_MD,
    GENERATOR,
    SPEC_VERSION,
    UNINSTALL_REACHABILITY,
    USER_SCOPE_DISCLOSURE,
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


def test_gen_statement_md_lists_repository_case_rules():
    _run_gen()
    md = CONFORMANCE_MD.read_text(encoding="ascii")
    assert "## Repository case rules" in md
    assert "GitHub Enterprise Cloud hosts ending in `.ghe.com`" in md
    assert "registry-sourced dependencies (including registry prefixes)" in md
    assert "Local paths, marketplace identities, and every other host remain case-sensitive" in md


def test_gen_statement_publishes_user_scope_disclosure():
    """req-tg-014 locations and capability contract stay generated."""
    _run_gen()
    document = json.loads(CONFORMANCE_JSON.read_text(encoding="ascii"))
    assert document["consumer_user_scope"] == USER_SCOPE_DISCLOSURE
    markdown = CONFORMANCE_MD.read_text(encoding="ascii")
    for value in USER_SCOPE_DISCLOSURE.values():
        assert value in markdown


def test_gen_statement_distinguishes_proposal_from_runtime_and_adoption() -> None:
    """The new general duty is not falsely advertised as an optional capability."""
    _run_gen()
    document = json.loads(CONFORMANCE_JSON.read_text(encoding="ascii"))
    assert document["proposed_amendments"]["uninstall-reachability"] == UNINSTALL_REACHABILITY
    assert "uninstall-reachability" not in document["optional_features"]
    assert UNINSTALL_REACHABILITY in CONFORMANCE_MD.read_text(encoding="ascii")
    requirement = next(row for row in document["requirements"] if row["id"] == "req-lk-024")
    assert requirement["test_count"] == 12
    assert any("sequential_diamond_lifecycle" in node for node in requirement["tests"])
    assert any("uncertain_manifest" in node for node in requirement["tests"])
    assert any("uncertain_path" in node for node in requirement["tests"])
