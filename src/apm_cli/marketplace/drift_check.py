"""Release-time marketplace-drift gate for ``apm pack --check-clean``.

For each configured marketplace output profile:

1. Compose the JSON document the builder would write right now (using
   the same resolver path the normal build uses, but in dry-run mode).
2. Compare it against the file currently on disk at the output path.
3. Classify as ``unchanged`` / ``drift`` / ``missing`` and emit per-key
   differences.

The gate writes nothing -- it constructs a ``MarketplaceBuilder``
configured with ``BuildOptions.dry_run=True`` so that even
``write_output`` paths that may be exercised do not mutate the working
tree.

See ``.apm/skills/wave-4-design.md`` section 4.3 for the flow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apm_cli.marketplace.builder import MarketplaceBuilder, ResolveResult
from apm_cli.marketplace.output_profiles import MARKETPLACE_OUTPUTS, MarketplaceOutputProfile
from apm_cli.marketplace.yml_schema import MarketplaceConfig
from apm_cli.utils.paths import portable_relpath

_MAX_DIFFS_RENDERED = 20


@dataclass(frozen=True)
class DriftDifference:
    """One leaf-key difference between on-disk and regenerated JSON."""

    path: str
    old: Any
    new: Any

    def to_json_dict(self) -> dict[str, Any]:
        return {"path": self.path, "old": self.old, "new": self.new}


@dataclass(frozen=True)
class DriftOutputReport:
    """Drift status for a single marketplace output profile."""

    format: str
    path: str
    status: str  # "unchanged" | "missing" | "drift"
    differences: tuple[DriftDifference, ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "path": self.path,
            "status": self.status,
            "differences": [d.to_json_dict() for d in self.differences],
        }


@dataclass(frozen=True)
class DriftReport:
    """Aggregate drift report across all configured outputs."""

    ok: bool
    outputs: tuple[DriftOutputReport, ...] = field(default_factory=tuple)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "outputs": [out.to_json_dict() for out in self.outputs],
        }

    def error_messages(self) -> list[str]:
        msgs: list[str] = []
        for out in self.outputs:
            if out.status == "missing":
                msgs.append(f"{out.path}: missing on disk (would be created)")
            elif out.status == "drift":
                count = len(out.differences)
                msgs.append(f"{out.path}: {count} differences vs. regenerated output")
        return msgs


def _format_path_segment(parent: str, key: str) -> str:
    if not parent:
        return key
    return f"{parent}.{key}"


def _index_segment(parent: str, idx: int) -> str:
    return f"{parent}[{idx}]"


def json_key_diff(old: Any, new: Any, *, prefix: str = "") -> list[DriftDifference]:
    """Recursively diff two JSON values, emitting per-leaf differences.

    Paths use ``dotted.bracket[i]`` notation. Added keys have ``old is
    None``; removed keys have ``new is None``.
    """
    diffs: list[DriftDifference] = []

    # Dict-vs-dict: per-key walk.
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old.keys()) | set(new.keys())):
            child_prefix = _format_path_segment(prefix, key)
            if key not in old:
                diffs.append(DriftDifference(child_prefix, None, new[key]))
            elif key not in new:
                diffs.append(DriftDifference(child_prefix, old[key], None))
            else:
                diffs.extend(json_key_diff(old[key], new[key], prefix=child_prefix))
        return diffs

    # List-vs-list: index-aligned walk.
    if isinstance(old, list) and isinstance(new, list):
        max_len = max(len(old), len(new))
        for i in range(max_len):
            child_prefix = _index_segment(prefix, i)
            if i >= len(old):
                diffs.append(DriftDifference(child_prefix, None, new[i]))
            elif i >= len(new):
                diffs.append(DriftDifference(child_prefix, old[i], None))
            else:
                diffs.extend(json_key_diff(old[i], new[i], prefix=child_prefix))
        return diffs

    # Mismatched types / scalar mismatch.
    if old != new:
        diffs.append(DriftDifference(prefix, old, new))
    return diffs


def _output_path_for_profile(
    config: MarketplaceConfig, profile: MarketplaceOutputProfile, project_root: Path
) -> Path:
    """Return the on-disk path this profile writes to."""
    # Prefer the explicit output_specs mapping if present, else fall back
    # to the per-profile config attribute (claude.output / codex.output).
    for spec in config.output_specs:
        if spec.name == profile.name and spec.path:
            return project_root / spec.path
    cfg_obj = getattr(config, profile.config_attr, None)
    output_attr = getattr(cfg_obj, "output", None) if cfg_obj is not None else None
    if isinstance(output_attr, str) and output_attr:
        return project_root / output_attr
    return project_root / profile.default_output


def _load_on_disk(path: Path) -> tuple[dict[str, Any] | None, bool]:
    """Return (parsed_json_or_none, exists)."""
    if not path.exists():
        return None, False
    try:
        return json.loads(path.read_text(encoding="utf-8")), True
    except (json.JSONDecodeError, OSError):
        return None, True


def check_marketplace_drift(
    builder: MarketplaceBuilder,
    config: MarketplaceConfig,
    project_root: Path,
) -> DriftReport:
    """Run the drift gate using *builder* (must be dry-run) and compare
    its composed output for each configured profile against the on-disk
    artifact at the profile's resolved output path.
    """
    resolve_result: ResolveResult = builder.resolve()

    # Honor the configured outputs list (claude / codex / ...).
    configured = tuple(config.outputs) if config.outputs else ("claude",)
    output_reports: list[DriftOutputReport] = []

    for name in configured:
        profile = MARKETPLACE_OUTPUTS.get(name)
        if profile is None:  # pragma: no cover - schema rejects unknown names
            continue
        out_path = _output_path_for_profile(config, profile, project_root)
        rel_display = (
            portable_relpath(out_path, project_root)
            if out_path.is_relative_to(project_root)
            else str(out_path)
        )

        remote_metadata = builder.remote_metadata_for_profile(profile, resolve_result.entries)
        new_doc, _warnings, _diagnostics = builder.compose_output(
            profile, resolve_result.entries, remote_metadata=remote_metadata
        )
        on_disk, exists = _load_on_disk(out_path)

        if not exists:
            # Treat every key in the regenerated doc as added.
            diffs = json_key_diff({}, new_doc)
            output_reports.append(
                DriftOutputReport(
                    format=profile.name,
                    path=rel_display,
                    status="missing",
                    differences=tuple(diffs),
                )
            )
            continue

        if on_disk is None:
            # File exists but is unparseable; treat as drift with one whole-doc diff.
            output_reports.append(
                DriftOutputReport(
                    format=profile.name,
                    path=rel_display,
                    status="drift",
                    differences=(DriftDifference(path="", old=None, new=new_doc),),
                )
            )
            continue

        # Round-trip both sides through the canonical serializer so the
        # diff is semantic, not whitespace-driven.
        canonical_new = json.loads(MarketplaceBuilder._serialize_json(new_doc))
        diffs = json_key_diff(on_disk, canonical_new)
        if not diffs:
            output_reports.append(
                DriftOutputReport(
                    format=profile.name,
                    path=rel_display,
                    status="unchanged",
                )
            )
        else:
            output_reports.append(
                DriftOutputReport(
                    format=profile.name,
                    path=rel_display,
                    status="drift",
                    differences=tuple(diffs),
                )
            )

    output_reports.sort(key=lambda r: r.format)
    overall_ok = all(r.status == "unchanged" for r in output_reports)
    return DriftReport(ok=overall_ok, outputs=tuple(output_reports))


def render_diff_lines(report: DriftOutputReport, limit: int = _MAX_DIFFS_RENDERED) -> list[str]:
    """Return human-readable per-diff lines bounded to *limit* entries."""
    rendered: list[str] = []
    diffs = report.differences
    for diff in diffs[:limit]:
        old_str = json.dumps(diff.old, ensure_ascii=True)
        new_str = json.dumps(diff.new, ensure_ascii=True)
        rendered.append(f"  {diff.path}  {old_str} -> {new_str}")
    extra = len(diffs) - limit
    if extra > 0:
        rendered.append(f"  ... and {extra} more differences")
    return rendered
