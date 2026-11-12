"""Explicit negotiation between normative and working manifest contracts."""

from __future__ import annotations

from enum import Enum

OPENAPM_V01_SCHEMA_URI = "https://microsoft.github.io/apm/specs/schemas/manifest-v0.1.schema.json"
OPENAPM_V0141_SCHEMA_URI = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-v0.1.41.schema.json"
)
OPENAPM_V0148_SCHEMA_URI = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-v0.1.48.schema.json"
)
# Candidate working-draft contract for source replacements (issue #3180).
# Unpublished until doc/schema/spec/release deliverables; do not claim ratified.
SOURCE_REPLACEMENTS_V1_SCHEMA_URI = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
)


class ManifestContract(str, Enum):
    """Manifest contracts understood by this loader."""

    WORKING_DRAFT = "working-draft"
    OPENAPM_V01 = "openapm-v0.1"
    SOURCE_REPLACEMENTS_V1 = "source-replacements-v1"


class UnsupportedManifestContractError(ValueError):
    """Raised when ``$schema`` names a contract this client cannot load."""


def negotiate_manifest_contract(data: dict) -> ManifestContract:
    """Select a loader contract from the standard ``$schema`` identity."""
    schema_uri = data.get("$schema")
    if schema_uri is None:
        return ManifestContract.WORKING_DRAFT
    if schema_uri in (OPENAPM_V01_SCHEMA_URI, OPENAPM_V0141_SCHEMA_URI, OPENAPM_V0148_SCHEMA_URI):
        return ManifestContract.OPENAPM_V01
    if schema_uri == SOURCE_REPLACEMENTS_V1_SCHEMA_URI:
        return ManifestContract.SOURCE_REPLACEMENTS_V1
    raise UnsupportedManifestContractError(
        f"Unsupported apm.yml $schema contract: {schema_uri!r}.\n"
        "Use a supported explicit contract:\n"
        f"  {OPENAPM_V01_SCHEMA_URI}\n"
        f"  {OPENAPM_V0141_SCHEMA_URI}\n"
        f"  {OPENAPM_V0148_SCHEMA_URI}\n"
        f"  {SOURCE_REPLACEMENTS_V1_SCHEMA_URI}"
    )
