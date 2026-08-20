"""Explicit negotiation between normative and working manifest contracts."""

from __future__ import annotations

from enum import Enum

OPENAPM_V01_SCHEMA_URI = "https://microsoft.github.io/apm/specs/schemas/manifest-v0.1.schema.json"


class ManifestContract(str, Enum):
    """Manifest contracts understood by this loader."""

    WORKING_DRAFT = "working-draft"
    OPENAPM_V01 = "openapm-v0.1"


class UnsupportedManifestContractError(ValueError):
    """Raised when ``$schema`` names a contract this client cannot load."""


def negotiate_manifest_contract(data: dict) -> ManifestContract:
    """Select a loader contract from the standard ``$schema`` identity."""
    schema_uri = data.get("$schema")
    if schema_uri is None:
        return ManifestContract.WORKING_DRAFT
    if schema_uri == OPENAPM_V01_SCHEMA_URI:
        return ManifestContract.OPENAPM_V01
    raise UnsupportedManifestContractError(
        f"Unsupported apm.yml $schema contract: {schema_uri!r}. "
        f"Supported explicit contract: {OPENAPM_V01_SCHEMA_URI}"
    )
