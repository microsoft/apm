"""Regression tests for marketplace metadata enrichment outcomes (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.marketplace.builder import (
    MetadataEnrichmentOutcome,
    MetadataEnrichmentResult,
)

pytestmark = pytest.mark.unit


def test_metadata_outcome_mapping_uses_the_precomputed_package_index() -> None:
    """Mapper lookups do not rescan every preserved metadata outcome."""
    result = MetadataEnrichmentResult(
        (
            MetadataEnrichmentOutcome("first", "fetched", (("version", "1.0.0"),)),
            MetadataEnrichmentOutcome("second", "empty"),
        )
    )

    assert dict(result) == {"first": {"version": "1.0.0"}}
    assert result["first"] == {"version": "1.0.0"}


def test_metadata_outcomes_reject_unknown_statuses() -> None:
    """Unknown future states cannot silently become certifiable."""
    with pytest.raises(ValueError, match="unknown metadata enrichment status"):
        MetadataEnrichmentOutcome("remote-tool", "future")  # type: ignore[arg-type]
