"""Release-time version-alignment gate for ``apm pack --check-versions``.

Pure helper: reads each local-path package's apm.yml top-level
``version`` field and compares it against the configured
``marketplace.versioning.strategy``. No git, no network.

Returns a :class:`VersionAlignmentReport` that both ``pack`` and
``marketplace doctor`` consume.

See ``.apm/skills/wave-4-design.md`` section 4.2 for the algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from apm_cli.marketplace.tag_pattern import render_tag
from apm_cli.marketplace.yml_schema import MarketplaceConfig, PackageEntry


@dataclass(frozen=True)
class PackageVersionRow:
    """One package's version-alignment status."""

    path: str
    version: str | None
    ok: bool
    reason: str
    rendered_tag: str | None = None


@dataclass(frozen=True)
class VersionAlignmentReport:
    """Result of running ``check_version_alignment``."""

    strategy: str
    expected: str | None
    ok: bool
    packages: tuple[PackageVersionRow, ...] = field(default_factory=tuple)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "expected": self.expected,
            "ok": self.ok,
            "packages": [
                {
                    "path": row.path,
                    "version": row.version,
                    "ok": row.ok,
                    "reason": row.reason,
                }
                for row in self.packages
            ],
        }

    def error_messages(self) -> list[str]:
        """Return one human-readable error string per misaligned package."""
        msgs: list[str] = []
        for row in self.packages:
            if row.ok:
                continue
            if row.reason == "missing_version":
                msgs.append(f"{row.path}: missing 'version' in apm.yml")
            elif row.reason == "invalid_yaml":
                msgs.append(f"{row.path}: malformed YAML in apm.yml (failed to parse)")
            elif row.reason == "no_apm_yml":
                msgs.append(f"{row.path}: no apm.yml found")
            elif row.reason.startswith("drift:expected="):
                expected = row.reason.split("=", 1)[1]
                msgs.append(f"{row.path}: expected {expected}, found {row.version}")
            elif row.reason.startswith("duplicate_tag:other="):
                other = row.reason.split("=", 1)[1]
                msgs.append(f"{row.path}: rendered tag collides with {other}")
            else:
                msgs.append(f"{row.path}: {row.reason}")
        return msgs


def _is_local_package(entry: PackageEntry) -> bool:
    """Return True if *entry* is a local-path source (``./...``)."""
    if entry.is_local:
        return True
    src = entry.source or ""
    return src.startswith("./") or src.startswith("../")


def _local_path(entry: PackageEntry) -> str:
    """Return the project-relative local path for an entry."""
    src = (entry.source or "").rstrip("/")
    if src.startswith("./"):
        src = src[2:]
    return src


def _read_local_version(project_root: Path, rel_source: str) -> tuple[str | None, str]:
    """Read top-level ``version:`` from ``<project_root>/<rel_source>/apm.yml``.

    Returns ``(version_or_None, status_code)`` where status_code is:

    * ``"ok"`` when a non-empty string version was found
    * ``"no_apm_yml"`` when the file does not exist
    * ``"invalid_yaml"`` when the file exists but does not parse as YAML
    * ``"missing_version"`` when the file parses as a mapping but has no
      usable ``version`` scalar
    """
    pkg_yml = project_root / rel_source / "apm.yml"
    if not pkg_yml.is_file():
        return None, "no_apm_yml"
    try:
        raw = yaml.safe_load(pkg_yml.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None, "invalid_yaml"
    if not isinstance(raw, dict):
        return None, "invalid_yaml"
    version = raw.get("version")
    if not isinstance(version, str) or not version.strip():
        return None, "missing_version"
    return version.strip(), "ok"


def _resolve_tag_pattern(entry: PackageEntry, default_pattern: str) -> str:
    """Return the tag pattern to use for *entry*."""
    if entry.tag_pattern:
        return entry.tag_pattern
    return default_pattern


def check_version_alignment(
    config: MarketplaceConfig, project_root: Path
) -> VersionAlignmentReport:
    """Run the version-alignment gate against *config*'s local packages.

    The function is pure: it only reads files under *project_root*. It
    never spawns git or makes network calls.
    """
    strategy = config.versioning.strategy
    local_entries = [e for e in config.packages if _is_local_package(e)]

    # Collect each local package's declared version + tag (when relevant).
    rows: list[PackageVersionRow] = []
    rendered: dict[str, str] = {}  # rendered_tag -> first package path that produced it

    for entry in local_entries:
        rel = _local_path(entry)
        version, status = _read_local_version(project_root, rel)
        if status == "no_apm_yml":
            rows.append(PackageVersionRow(path=rel, version=None, ok=False, reason="no_apm_yml"))
            continue
        if status == "invalid_yaml":
            rows.append(PackageVersionRow(path=rel, version=None, ok=False, reason="invalid_yaml"))
            continue
        if status == "missing_version":
            rows.append(
                PackageVersionRow(path=rel, version=None, ok=False, reason="missing_version")
            )
            continue

        # Strategy-specific evaluation.
        if strategy == "lockstep":
            if version == config.version:
                rows.append(PackageVersionRow(path=rel, version=version, ok=True, reason="matches"))
            else:
                rows.append(
                    PackageVersionRow(
                        path=rel,
                        version=version,
                        ok=False,
                        reason=f"drift:expected={config.version}",
                    )
                )
        elif strategy == "tag_pattern":
            pattern = _resolve_tag_pattern(entry, config.build.tag_pattern)
            try:
                tag = render_tag(pattern, name=entry.name, version=version)
            except Exception:
                rows.append(
                    PackageVersionRow(
                        path=rel,
                        version=version,
                        ok=False,
                        reason="missing_version",
                        rendered_tag=None,
                    )
                )
                continue
            if tag in rendered:
                other = rendered[tag]
                rows.append(
                    PackageVersionRow(
                        path=rel,
                        version=version,
                        ok=False,
                        reason=f"duplicate_tag:other={other}",
                        rendered_tag=tag,
                    )
                )
                # Also flip the earlier-matched row to drift since both collide.
                for i, prev in enumerate(rows[:-1]):
                    if prev.path == other and prev.ok:
                        rows[i] = PackageVersionRow(
                            path=prev.path,
                            version=prev.version,
                            ok=False,
                            reason=f"duplicate_tag:other={rel}",
                            rendered_tag=prev.rendered_tag,
                        )
                        break
                # Track the most recent colliding entry so a 3rd+ collision
                # blames its nearest sibling instead of the original one.
                rendered[tag] = rel
            else:
                rendered[tag] = rel
                rows.append(
                    PackageVersionRow(
                        path=rel,
                        version=version,
                        ok=True,
                        reason="matches",
                        rendered_tag=tag,
                    )
                )
        elif strategy == "per_package":
            # Only requires version field; equality not enforced.
            rows.append(PackageVersionRow(path=rel, version=version, ok=True, reason="matches"))
        else:  # pragma: no cover - defensive; schema validates strategy upstream
            rows.append(
                PackageVersionRow(
                    path=rel,
                    version=version,
                    ok=False,
                    reason=f"unknown_strategy:{strategy}",
                )
            )

    rows_sorted = tuple(sorted(rows, key=lambda r: r.path))
    expected = config.version if strategy == "lockstep" else None
    overall_ok = all(r.ok for r in rows_sorted)
    return VersionAlignmentReport(
        strategy=strategy,
        expected=expected,
        ok=overall_ok,
        packages=rows_sorted,
    )
