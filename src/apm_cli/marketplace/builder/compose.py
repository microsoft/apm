"""MarketplaceBuilder -- load, resolve, compose, and write marketplace.json.

This module implements the full build pipeline:

1. **Load** -- parse ``marketplace.yml`` via ``yml_schema.load_marketplace_yml``.
2. **Resolve** -- for every package entry, call ``git ls-remote`` (via
   ``RefResolver``) and determine the concrete tag + SHA.
3. **Compose** -- produce an Anthropic-compliant ``marketplace.json`` dict
   with all APM-only fields stripped.
4. **Write** -- atomically write the JSON to disk (or skip on dry-run)
   and produce a ``BuildReport`` with diff statistics.

Hard rule: the output ``marketplace.json`` conforms byte-for-byte to
Anthropic's schema.  No APM-specific keys, no extensions, no renamed
fields.  ``packages`` in yml becomes ``plugins`` in json.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...utils.path_security import ensure_path_within
from .._io import atomic_write
from ..diagnostics import BuildDiagnostic
from ..output_profiles import (
    CODEX_MARKETPLACE_OUTPUT,
    DEFAULT_MARKETPLACE_OUTPUT,
    MarketplaceOutputProfile,
)
from .class_ import BuildReport, MarketplaceOutputReport, ResolvedPackage

logger = logging.getLogger(__name__)
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class _WriteOutputOptions:
    """Optional parameters for write_output."""

    include_diff: bool = False
    remote_metadata: dict[str, dict[str, Any]] | None = None
    errors: tuple[tuple[str, str], ...] = ()


def compose_marketplace_json(self, resolved: list[ResolvedPackage]) -> dict[str, Any]:
    """Produce an Anthropic-compliant marketplace.json dict.

    All APM-only fields are stripped.  Key order follows the Anthropic
    schema exactly.

    Parameters
    ----------
    resolved:
        List of resolved packages (from ``resolve()``).

    Returns
    -------
    dict
        An ``OrderedDict``-style dict ready to be serialised as JSON.
    """
    resolved_tuple = tuple(resolved)
    mapper_result = self._map_output(
        DEFAULT_MARKETPLACE_OUTPUT,
        resolved_tuple,
        remote_metadata=self._prefetch_metadata(resolved_tuple),
    )
    self._compose_warnings = mapper_result.warnings
    self._compose_diagnostics = mapper_result.diagnostics
    return mapper_result.document


def compose_codex_marketplace_json(
    self,
    resolved: list[ResolvedPackage],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Produce a Codex ``.agents/plugins/marketplace.json`` document."""
    mapper_result = self._map_output(CODEX_MARKETPLACE_OUTPUT, tuple(resolved))
    return mapper_result.document, mapper_result.warnings


def write_codex_marketplace_json(
    self,
    resolved: tuple[ResolvedPackage, ...],
) -> tuple[Path, tuple[str, ...]]:
    """Write the configured Codex marketplace output using resolved packages."""
    yml = self._load_yml()
    output_path = self._project_root / yml.codex.output
    ensure_path_within(output_path, self._project_root)
    output = self.write_output(CODEX_MARKETPLACE_OUTPUT, resolved, output_path)
    return output.output_path, output.warnings


def compose_output(
    self,
    profile: MarketplaceOutputProfile,
    resolved: tuple[ResolvedPackage, ...],
    remote_metadata: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], tuple[str, ...], tuple[BuildDiagnostic, ...]]:
    """Compose the JSON document for a marketplace output profile."""
    mapper_result = self._map_output(profile, resolved, remote_metadata=remote_metadata)
    return mapper_result.document, mapper_result.warnings, mapper_result.diagnostics


def write_output(
    self,
    profile: MarketplaceOutputProfile,
    resolved: tuple[ResolvedPackage, ...],
    output_path: Path,
    **kwargs,
) -> BuildReport:
    """Write one marketplace output profile using already resolved packages.

    Keyword Args:
        include_diff: Whether to compute diff statistics (default: False).
        remote_metadata: Optional remote metadata dict.
        errors: Optional tuple of error pairs.
    """
    include_diff = kwargs.get("include_diff", False)
    remote_metadata = kwargs.get("remote_metadata")
    errors = kwargs.get("errors", ())

    ensure_path_within(output_path, self._project_root)
    new_json, warnings, diagnostics = self.compose_output(
        profile,
        resolved,
        remote_metadata=remote_metadata,
    )

    unchanged = added = updated = removed = 0
    if include_diff:
        old_json = self._load_existing_json(output_path)
        unchanged, added, updated, removed = self._compute_diff(old_json, new_json)

    if not self._options.dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(output_path, self._serialize_json(new_json))

    output_report = MarketplaceOutputReport(
        profile=profile.name,
        resolved=tuple(resolved),
        errors=tuple(errors),
        warnings=tuple(warnings),
        diagnostics=tuple(diagnostics),
        unchanged_count=unchanged,
        added_count=added,
        updated_count=updated,
        removed_count=removed,
        output_path=output_path,
        dry_run=self._options.dry_run,
    )
    return BuildReport(outputs=(output_report,))


def _extract_sha_from_source(src: Any) -> str:
    """Extract SHA from source dict or string."""
    sha = ""
    if isinstance(src, dict):
        # Accept both the new ``sha`` field (Claude-spec compliant)
        # and the legacy ``commit`` field for backward-compatibility
        # with marketplace.json files written before this PR.
        sha = src.get("sha") or src.get("commit", "")
    elif isinstance(src, str):
        sha = src  # local-path packages: use the path string itself
    return sha


def _build_plugin_maps(
    plugins: list[dict[str, Any]],
) -> dict[str, str]:
    """Build a mapping of plugin name -> SHA from plugin list."""
    result: dict[str, str] = {}
    for p in plugins:
        name = p.get("name", "")
        src = p.get("source", {})
        sha = _extract_sha_from_source(src)
        result[name] = sha
    return result


def _compute_diff(
    old_json: dict[str, Any] | None,
    new_json: dict[str, Any],
) -> tuple[int, int, int, int]:
    """Compare old vs new marketplace.json and classify each plugin.

    Returns (unchanged, added, updated, removed) counts.
    """
    if old_json is None:
        return (0, len(new_json.get("plugins", [])), 0, 0)

    old_plugins = _build_plugin_maps(old_json.get("plugins", []))
    new_plugins = _build_plugin_maps(new_json.get("plugins", []))

    unchanged = 0
    updated = 0
    added = 0
    removed = 0

    for name, sha in new_plugins.items():
        if name not in old_plugins:
            added += 1
        elif old_plugins[name] == sha:
            unchanged += 1
        else:
            updated += 1

    for name in old_plugins:
        if name not in new_plugins:
            removed += 1

    return (unchanged, added, updated, removed)


def _serialize_json(data: dict[str, Any]) -> str:
    """Serialize to JSON with 2-space indent, LF endings, trailing newline."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via tmp + rename."""
    atomic_write(path, content)


def _load_existing_json(self, path: Path) -> dict[str, Any] | None:
    """Load existing marketplace.json for diff, or None."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text)
    except (json.JSONDecodeError, OSError):
        return None


def build(self) -> BuildReport:
    """Full pipeline: load -> resolve -> compose -> write.

    Returns
    -------
    BuildReport
        Summary including diff statistics.
    """
    result = self.resolve()
    report = self.write_output(
        DEFAULT_MARKETPLACE_OUTPUT,
        result.entries,
        self._output_path(),
        include_diff=True,
        errors=result.errors,
        remote_metadata=self.remote_metadata_for_profile(
            DEFAULT_MARKETPLACE_OUTPUT,
            result.entries,
        ),
    )

    # Cleanup resolver
    if self._resolver is not None:
        self._resolver.close()

    return BuildReport(
        outputs=report.outputs,
    )
