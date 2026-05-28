"""Shared loader for the OpenAPM requirements manifest.

Single point of truth for path resolution and manifest parsing.
Reused by conftest.py, orphan_check.py, and gen_statement.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_DIR = REPO_ROOT / "docs" / "src" / "content" / "docs" / "specs"
SPEC_PATH = SPEC_DIR / "openapm-v0.1.md"
# Schemas and the requirements manifest are served as static assets so
# the schema $id URLs resolve on the published site. They live under
# docs/public/specs/, not under the Starlight content collection.
PUBLIC_SPEC_DIR = REPO_ROOT / "docs" / "public" / "specs"
MANIFEST_PATH = PUBLIC_SPEC_DIR / "manifests" / "openapm-v0.1.requirements.yml"
SCHEMA_PATH = PUBLIC_SPEC_DIR / "schemas" / "requirements-v0.1.schema.json"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "spec-conformance"
COVERAGE_PATH = REPO_ROOT / "build" / "conformance-coverage.json"

ALLOWED_KEYWORDS = ("MUST", "MUST NOT", "SHOULD", "SHOULD NOT", "MAY")
ALLOWED_CLASSES = ("producer", "consumer", "registry", "governance")


@dataclass(frozen=True)
class Requirement:
    id: str
    keyword: str
    section: str
    conformance_class: str
    fixture: str | None = None
    oracle: str | None = None
    round_trip_exempt_fields: tuple[str, ...] = ()
    notes: str | None = None


def load_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as f:
        schema = json.load(f)
    Draft202012Validator.check_schema(schema)
    return schema


def load_manifest_raw() -> dict[str, Any]:
    with MANIFEST_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_requirements() -> list[Requirement]:
    schema = load_schema()
    raw = load_manifest_raw()
    Draft202012Validator(schema).validate(raw)
    reqs: list[Requirement] = []
    for entry in raw["requirements"]:
        reqs.append(
            Requirement(
                id=entry["id"],
                keyword=entry["keyword"],
                section=entry["section"],
                conformance_class=entry["conformance_class"],
                fixture=entry.get("fixture"),
                oracle=entry.get("oracle"),
                round_trip_exempt_fields=tuple(entry.get("round_trip_exempt_fields", [])),
                notes=entry.get("notes"),
            )
        )
    return reqs


def requirements_by_id() -> dict[str, Requirement]:
    return {r.id: r for r in load_requirements()}
