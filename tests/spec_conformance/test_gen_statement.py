"""gen_statement determinism + ASCII self-test."""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts.architecture_linter.checks.contracts_conformance_coverage import (
    _statement_findings,
)
from tests.spec_conformance import gen_statement
from tests.spec_conformance._manifest import REPO_ROOT, Requirement
from tests.spec_conformance._test_kind import BEHAVIORAL, TEXT_SHAPE
from tests.spec_conformance.gen_statement import (
    CONFORMANCE_JSON,
    CONFORMANCE_MD,
    GENERATOR,
    SPEC_VERSION,
    USER_SCOPE_DISCLOSURE,
)

pytestmark = [pytest.mark.component, pytest.mark.promise("req-cf-002", "req-lk-018")]


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


def test_gen_statement_declares_implementation_default_host():
    """req-mf-019: the implementation-default host MUST be documented."""
    _run_gen()
    md = CONFORMANCE_MD.read_text(encoding="ascii")
    assert "## Default host" in md
    assert "`GITHUB_HOST` environment variable, or `github.com`" in md


def test_gen_statement_publishes_user_scope_disclosure():
    """req-tg-014 locations and capability contract stay generated."""
    _run_gen()
    document = json.loads(CONFORMANCE_JSON.read_text(encoding="ascii"))
    assert document["consumer_user_scope"] == USER_SCOPE_DISCLOSURE
    markdown = CONFORMANCE_MD.read_text(encoding="ascii")
    for value in USER_SCOPE_DISCLOSURE.values():
        assert value in markdown


def test_text_shape_tests_are_not_counted_as_implementation_coverage():
    """A requirement whose linked tests only assert spec text or fixture shape is not tested.

    The statement reports a requirement linked only to text-shape tests (never
    running APM) as text-only, and every requirement it reports as active must
    name at least one behavioral test. The text-only case is derived from the
    statement, so a requirement gaining a behavioral test does not break it.
    """
    document = json.loads(CONFORMANCE_JSON.read_text(encoding="ascii"))
    shape_only = [
        entry
        for entry in document["requirements"]
        if entry["tests"] and not entry["behavioral_tests"]
    ]
    assert any(entry["status"] == "text-only" for entry in shape_only)
    for entry in shape_only:
        assert entry["status"] != "active", entry["id"]
    for entry in document["requirements"]:
        assert set(entry["behavioral_tests"]) <= set(entry["tests"]), entry["id"]
        if entry["status"] == "active":
            assert entry["behavioral_tests"], f"{entry['id']} is active with no behavioral test"


def _statement_provider(document: dict) -> SimpleNamespace:
    text = json.dumps(document)
    return SimpleNamespace(source_cache=SimpleNamespace(read=lambda _path: (text, None)))


def test_partial_requirement_is_reported_and_still_needs_behavioral_coverage(monkeypatch):
    """A PARTIAL_CONFORMANCE entry is reported partial with its scope, never as active."""
    requirements = [
        Requirement("req-zz-001", "SHOULD", "1.1", "consumer"),
        Requirement("req-zz-002", "MUST", "1.2", "consumer"),
        Requirement("req-zz-003", "SHOULD", "1.3", "consumer"),
    ]
    coverage = {
        "req-zz-001": [{"test_nodeid": "t.py::test_a", "status": "active", "kind": BEHAVIORAL}],
        "req-zz-002": [{"test_nodeid": "t.py::test_b", "status": "active", "kind": BEHAVIORAL}],
        "req-zz-003": [{"test_nodeid": "t.py::test_c", "status": "active", "kind": TEXT_SHAPE}],
    }
    scope = "default frozen when a lockfile exists"
    monkeypatch.setattr(gen_statement, "load_requirements", lambda: requirements)
    monkeypatch.setattr(gen_statement, "_ensure_coverage", lambda: coverage)
    monkeypatch.setattr(gen_statement, "_extract_waivers", dict)
    monkeypatch.setattr(
        gen_statement, "PARTIAL_CONFORMANCE", {"req-zz-001": scope, "req-zz-003": scope}
    )

    document = gen_statement.build_json()
    entries = {entry["id"]: entry for entry in document["requirements"]}
    assert entries["req-zz-001"]["status"] == "partial"
    assert entries["req-zz-001"]["partial"] == scope
    assert entries["req-zz-002"]["status"] == "active"
    assert "partial" not in entries["req-zz-002"]
    assert entries["req-zz-003"]["status"] == "text-only"
    assert "partial" not in entries["req-zz-003"]
    assert document["summary_by_class"]["consumer"]["partial"] == 1

    markdown = gen_statement.build_md(document)
    assert "| Class | Active | Partial | Skipped | Xfail | Text-only | Unbound |" in markdown
    assert "| Consumer | 1 | 1 | 0 | 0 | 1 | 0 |" in markdown
    assert "| consumer | partial | 1 / 1 |" in markdown
    assert f"## Partial conformance\n\n### req-zz-001\n- {scope}\n" in markdown
    assert "### req-zz-003" not in markdown

    assert _statement_findings(_statement_provider(document)) == []
    entries["req-zz-001"]["behavioral_tests"] = []
    findings = _statement_findings(_statement_provider(document))
    assert [finding.message for finding in findings] == [
        "req-zz-001 is reported partial with only text/shape tests; "
        "report it text-only or bind a behavioral test"
    ]
