"""Shared helpers for the spec-conformance test modules.

Keep test bodies short and uniform. The harness is built so that any
requirement marked @pytest.mark.req but not yet exercisable lands in
the CONFORMANCE statement as `status=skipped` with the verbatim
waiver text from `waive(...)`.

Real-assertion helpers (parse_yaml_fixture, validate_schema,
load_oracle) are imported by the cluster modules. The honesty
contract: a test that has no assertion MUST call `waive("...")`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from tests.spec_conformance._manifest import FIXTURE_ROOT, SPEC_DIR


def waive(reason: str) -> None:
    """Skip a conformance test with a recorded waiver.

    Use the prefix `waiver:` so the conformance generator can extract
    structured rationales for the public statement.
    """
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
    path = SPEC_DIR / "schemas" / name
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def validate_against(schema_name: str, doc: Any) -> None:
    schema = load_schema(schema_name)
    Draft202012Validator(schema).validate(doc)


def fixture_path(*parts: str) -> Path:
    return FIXTURE_ROOT.joinpath(*parts)
