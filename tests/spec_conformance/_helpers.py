"""Shared helpers for the spec-conformance test modules.

Two assertion shapes carry the weight:

1. **Schema introspection** -- validate fixtures against the shipped
   JSON Schemas (positive + negative).
2. **Spec-text grep** -- assert that a verbatim substring is present
   in the spec body. This is the silent-deletion detector: if the
   spec authoring panel removes or rewords a normative clause, the
   matching test breaks at PR time.

There is also `waive(...)`. Use it ONLY when the requirement is
genuinely beyond v0.1 active testability and the rationale is
written down. Every waiver appears in CONFORMANCE.md as debt.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from tests.spec_conformance._manifest import FIXTURE_ROOT, SPEC_DIR, SPEC_PATH


def waive(reason: str) -> None:
    pytest.skip(f"waiver: {reason}")


def load_yaml_fixture(*parts: str) -> Any:
    path = FIXTURE_ROOT.joinpath(*parts)
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_json_fixture(*parts: str) -> Any:
    path = FIXTURE_ROOT.joinpath(*parts)
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_schema(name: str) -> dict[str, Any]:
    # Schemas live in docs/public/specs/schemas/ so they ship at their
    # declared $id on the published Starlight site.
    path = SPEC_DIR.parent.parent.parent.parent / "public" / "specs" / "schemas" / name
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def validate_against(schema_name: str, doc: Any) -> None:
    schema = load_schema(schema_name)
    Draft202012Validator(schema).validate(doc)


def fixture_path(*parts: str) -> Path:
    return FIXTURE_ROOT.joinpath(*parts)


_SPEC_TEXT_CACHE: str | None = None


def spec_text() -> str:
    global _SPEC_TEXT_CACHE
    if _SPEC_TEXT_CACHE is None:
        _SPEC_TEXT_CACHE = SPEC_PATH.read_text(encoding="utf-8")
    return _SPEC_TEXT_CACHE


def spec_contains(needle: str) -> bool:
    return needle in spec_text()


def assert_spec_contains(*needles: str) -> None:
    """Assert that every needle appears verbatim in the spec body.

    Drift-detection contract: the spec authoring panel cannot remove
    or silently reword these phrases without breaking the test. The
    test does NOT pin the surrounding language -- only the substring
    -- so editorial changes that preserve the normative invariant are
    allowed.
    """
    missing = [n for n in needles if not spec_contains(n)]
    assert not missing, (
        f"Spec body MUST contain the verbatim phrase(s): {missing}. "
        "If the spec was intentionally reworded, update the assertion "
        "to the new minimal phrasing and add a note explaining the "
        "drift."
    )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
