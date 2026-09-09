"""Shared loader for the OpenAPM requirements manifest.

Single point of truth for path resolution and manifest parsing.
Reused by conftest.py, orphan_check.py, and gen_statement.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_DIR = REPO_ROOT / "docs" / "src" / "content" / "docs" / "specs"
_SELECTED_MINOR = "v0.2"
SPEC_PATH = SPEC_DIR / f"openapm-{_SELECTED_MINOR}.md"
# Schemas and the requirements manifest are served as static assets so
# the schema $id URLs resolve on the published site. They live under
# docs/public/specs/, not under the Starlight content collection.
PUBLIC_SPEC_DIR = REPO_ROOT / "docs" / "public" / "specs"
MANIFEST_PATH = PUBLIC_SPEC_DIR / "manifests" / f"openapm-{_SELECTED_MINOR}.requirements.yml"
SCHEMA_PATH = PUBLIC_SPEC_DIR / "schemas" / "requirements-v0.1.schema.json"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "spec-conformance"
COVERAGE_ENV = "APM_CONFORMANCE_COVERAGE_PATH"
Coverage = dict[str, list[dict[str, str]]]

ALLOWED_KEYWORDS = ("MUST", "MUST NOT", "SHOULD", "SHOULD NOT", "MAY")
ALLOWED_CLASSES = ("producer", "consumer", "registry", "governance")


@dataclass(frozen=True)
class Assessment:
    """The one active exact revision, with its validated input fingerprints."""

    version: str
    spec_path: Path
    manifest_path: Path
    spec_sha256: str
    manifest_sha256: str

    @property
    def citation(self) -> str:
        return f"https://microsoft.github.io/apm/spec/{self.version}"

    @property
    def coverage_path(self) -> Path:
        return REPO_ROOT / "build" / f"conformance-coverage-{self.version}.json"

    def stamp(self) -> dict[str, str]:
        return {
            "spec_version": self.version,
            "spec_sha256": self.spec_sha256,
            "manifest_sha256": self.manifest_sha256,
            "inventory_kind": "static-test-bindings",
        }


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


def selected_assessment() -> Assessment:
    """Validate metadata against the artifact, without a second version authority."""
    raw = load_manifest_raw()
    Draft202012Validator(load_schema()).validate(raw)
    version = raw["spec_version"]
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Selected assessment requires an exact specification revision")
    if version.rsplit(".", 1)[0] != _SELECTED_MINOR:
        raise ValueError("Selected manifest revision disagrees with its minor artifact")
    text = SPEC_PATH.read_text(encoding="utf-8")
    parts = text.split("---\n", 2)
    if len(parts) != 3 or parts[0]:
        raise ValueError("Selected specification has no frontmatter identity")
    metadata = yaml.safe_load(parts[1])
    if not isinstance(metadata, dict) or metadata.get("title") != f"OpenAPM {version}":
        raise ValueError("Selected specification identity disagrees with manifest metadata")
    if metadata.get("slug") != f"specs/openapm-{version.replace('.', '')}":
        raise ValueError("Selected specification needs an exact-revision content route")
    return Assessment(
        version=version,
        spec_path=SPEC_PATH,
        manifest_path=MANIFEST_PATH,
        spec_sha256=hashlib.sha256(SPEC_PATH.read_bytes()).hexdigest(),
        manifest_sha256=hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest(),
    )


def coverage_document(coverage: Coverage) -> dict[str, Any]:
    """Stamp static bindings, not runtime outcomes, with the selected inputs."""
    return {**selected_assessment().stamp(), "requirements": coverage}


def coverage_output_path() -> Path:
    """Honor the fresh collector's isolated output, or use a version-qualified map."""
    requested = os.environ.get(COVERAGE_ENV)
    return Path(requested) if requested else selected_assessment().coverage_path


def load_coverage(path: Path) -> Coverage:
    """Reject stale, unversioned, mismatched, or malformed binding inventories."""
    with path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or any(
        document.get(key) != value for key, value in selected_assessment().stamp().items()
    ):
        raise ValueError("Coverage identity or input fingerprints do not match the assessment")
    coverage = document.get("requirements")
    if not isinstance(coverage, dict) or any(
        not isinstance(req_id, str)
        or not isinstance(rows, list)
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("test_nodeid"), str)
            or row.get("status") not in {"active", "skipped", "xfail"}
            for row in rows
        )
        for req_id, rows in coverage.items()
    ):
        raise ValueError("Malformed conformance binding inventory")
    return coverage


def collect_coverage() -> Coverage:
    """Always collect the full active suite; never reuse a previous map on failure."""
    assessment = selected_assessment()
    assessment.coverage_path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix="conformance-", dir=assessment.coverage_path.parent
    ) as temporary:
        output = Path(temporary) / assessment.coverage_path.name
        env = {**os.environ, COVERAGE_ENV: str(output)}
        env.pop("PYTEST_ADDOPTS", None)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/spec_conformance",
                "--collect-only",
                "-q",
                "-o",
                "addopts=",
                "-p",
                "no:randomly",
                "--no-header",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Full spec collection failed ({result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}"
            )
        if not output.is_file():
            raise RuntimeError("Full spec collection produced no binding inventory")
        return load_coverage(output)


def load_requirements() -> list[Requirement]:
    selected_assessment()
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


if __name__ == "__main__":
    selection = selected_assessment()
    print(selection.spec_path.relative_to(REPO_ROOT).as_posix())
    print(selection.manifest_path.relative_to(REPO_ROOT).as_posix())
