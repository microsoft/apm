"""APM export-patch command.

Turns ``modified`` drift findings into patches against package sources.
``apm export-patch`` builds on the drift replay (``install/drift.py``): a
``modified`` finding means a deployed, APM-managed file differs from what
a clean install of the locked dependency graph would produce. For
deployments that copy their source file verbatim, that difference IS the
user's local edit -- so it can be re-expressed as a unified diff against
the package source file and applied upstream (``git apply`` in a clone
of the package repository, checked out at the locked base).

Eligibility is decided by content, not by a per-format allowlist: a
finding is exportable iff the replayed (expected) content is
byte-identical -- after drift normalization -- to exactly one file in
the owning package's source tree. Deployments that transform their
source (frontmatter rewrites, aggregated/compiled outputs, resolved
links) fail that match and are reported as skipped with a reason,
instead of producing a patch that would not apply.

The command consumes the replay/diff APIs read-only; it never mutates
the project tree, the cache, or the lockfile. Patch files are the only
output.
"""

from __future__ import annotations

import difflib
import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import click

from ..constants import APM_YML_FILENAME
from ..core.command_logger import CommandLogger
from ..deps.lockfile import LockFile, get_lockfile_path
from ..utils.normalization import _normalize

if TYPE_CHECKING:
    from ..deps.lockfile import LockedDependency
    from ..install.drift import DriftFinding

# Source files above this size are never indexed as reverse-mapping
# candidates. Managed primitives are small text files; a larger blob is
# noise at best and a memory hazard at worst.
_MAX_INDEXED_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class ExportedEdit:
    """A local edit that was successfully mapped back to a source file."""

    deployed_path: str  # project-relative posix path of the edited file
    source_path: str  # package-source-relative posix path the diff applies to
    package: str  # lockfile dependency key


@dataclass(frozen=True)
class SkippedEdit:
    """A ``modified`` finding that could not be exported, with the reason."""

    deployed_path: str
    package: str
    reason: str


@dataclass(frozen=True)
class PatchExport:
    """Result of a patch-export computation over drift findings."""

    # dependency key -> full patch document (header + unified diffs)
    patches: dict[str, str] = field(default_factory=dict)
    exported: list[ExportedEdit] = field(default_factory=list)
    skipped: list[SkippedEdit] = field(default_factory=list)


def patch_filename(dep_key: str) -> str:
    """Map a lockfile dependency key to a filesystem-safe patch filename."""
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in dep_key)
    safe = safe.strip("-.") or "package"
    return f"{safe}.patch"


def _index_source_tree(source_root: Path) -> dict[str, list[str]]:
    """Index a package source tree by normalized content digest.

    Returns ``{sha256(normalized bytes): [source-relative posix paths]}``.
    Multiple paths under one digest means the content alone cannot
    identify the source file (ambiguous -- the caller must skip).
    """
    from ..install.cache_pin import MARKER_FILENAME

    index: dict[str, list[str]] = {}
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        # The cache-pin marker is install metadata, not package content;
        # it must never be offered as a reverse-mapping candidate.
        if path.name == MARKER_FILENAME:
            continue
        try:
            if path.stat().st_size > _MAX_INDEXED_BYTES:
                continue
            digest = hashlib.sha256(_normalize(path.read_bytes())).hexdigest()
        except OSError:
            continue
        rel = path.relative_to(source_root).as_posix()
        index.setdefault(digest, []).append(rel)
    return index


def _unified_diff(a_text: str, b_text: str, rel: str) -> str:
    """Build a ``git apply``-compatible unified diff for one file.

    Emits the ``\\ No newline at end of file`` marker where either side
    lacks a trailing newline, which :func:`difflib.unified_diff` does not
    produce on its own.
    """
    a_lines = a_text.splitlines(keepends=True)
    b_lines = b_text.splitlines(keepends=True)
    out: list[str] = []
    for line in difflib.unified_diff(a_lines, b_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}"):
        if line.endswith("\n"):
            out.append(line)
        else:
            out.append(line + "\n")
            out.append("\\ No newline at end of file\n")
    return "".join(out)


def _base_label(dep: LockedDependency) -> str:
    """Human-readable description of the snapshot the patch applies to."""
    if dep.source == "registry":
        parts = [f"version {dep.version or 'unknown'}"]
        if dep.resolved_url:
            parts.append(f"({dep.resolved_url})")
        return " ".join(parts)
    if dep.resolved_commit:
        label = f"commit {dep.resolved_commit}"
        if dep.resolved_ref:
            label += f" ({dep.resolved_ref})"
        return label
    return f"version {dep.version or 'unknown'}"


def _patch_header(dep_key: str, dep: LockedDependency) -> str:
    """Leading comment block for a per-package patch document.

    ``git apply`` ignores text before the first ``---``/``+++`` header,
    so the metadata rides inside the patch file itself.
    """
    lines = [
        "# Exported by 'apm export-patch'",
        f"# package: {dep_key}",
        f"# source: {dep.source_url or dep.repo_url}",
        f"# base: {_base_label(dep)}",
        "# Apply from the package repository root, checked out at the",
        "# base above:  git apply <this file>",
    ]
    return "\n".join(lines) + "\n"


def _resolve_package_key(finding_path: str, package: str, lockfile: LockFile) -> str:
    """Resolve the owning dependency key for a finding.

    Findings normally carry the key already; the fallback covers legacy
    lockfiles whose ``deployed_files`` track a skill directory (trailing
    ``/``) rather than every file inside it.
    """
    if package:
        return package
    for key, dep in lockfile.dependencies.items():
        for tracked in dep.deployed_files or []:
            if tracked.endswith("/") and finding_path.startswith(tracked):
                return key
    return ""


def build_patch_export(
    project_root: Path,
    scratch_root: Path,
    lockfile: LockFile,
    findings: list[DriftFinding],
) -> PatchExport:
    """Compute exportable patches from drift findings.

    Only ``modified`` findings are considered: ``unintegrated`` (file
    deleted locally) does not imply intent to delete upstream, and
    ``orphaned`` is a lockfile-hygiene issue rather than a content edit.
    """
    from ..deps.lockfile import _SELF_KEY
    from ..install.drift import CacheMissError, _materialize_install_path

    result_exported: list[ExportedEdit] = []
    result_skipped: list[SkippedEdit] = []
    patches: dict[str, str] = {}

    modified = [f for f in findings if f.kind == "modified"]
    by_package: dict[str, list[DriftFinding]] = {}
    for finding in modified:
        key = _resolve_package_key(finding.path, finding.package, lockfile)
        by_package.setdefault(key, []).append(finding)

    apm_modules_dir = project_root / "apm_modules"

    for key in sorted(by_package):
        group = by_package[key]
        if not key:
            result_skipped.extend(
                SkippedEdit(f.path, "", "not tracked to a package in the lockfile") for f in group
            )
            continue

        dep = lockfile.get_dependency(key)
        if key == _SELF_KEY or dep is None or dep.local_path == _SELF_KEY:
            result_skipped.extend(
                SkippedEdit(
                    f.path,
                    key,
                    "project-local content; the source already lives in this project",
                )
                for f in group
            )
            continue
        if dep.source == "local" or dep.local_path:
            result_skipped.extend(
                SkippedEdit(
                    f.path,
                    key,
                    f"local package ({dep.local_path}); edit the source file directly",
                )
                for f in group
            )
            continue

        try:
            source_root = _materialize_install_path(
                dep, project_root, apm_modules_dir, cache_only=True, lockfile=lockfile
            )
        except CacheMissError as exc:
            result_skipped.extend(
                SkippedEdit(f.path, key, f"package cache unavailable: {exc}") for f in group
            )
            continue

        index = _index_source_tree(source_root)
        file_diffs: list[str] = []
        for finding in sorted(group, key=lambda f: f.path):
            edit = _export_one(finding, key, index, scratch_root, project_root)
            if isinstance(edit, SkippedEdit):
                result_skipped.append(edit)
                continue
            exported, diff_text = edit
            result_exported.append(exported)
            file_diffs.append(diff_text)

        if file_diffs:
            patches[key] = _patch_header(key, dep) + "".join(file_diffs)

    return PatchExport(patches=patches, exported=result_exported, skipped=result_skipped)


def _export_one(
    finding: DriftFinding,
    dep_key: str,
    index: dict[str, list[str]],
    scratch_root: Path,
    project_root: Path,
) -> SkippedEdit | tuple[ExportedEdit, str]:
    """Map one ``modified`` finding to a unified diff, or a skip reason."""
    scratch_path = scratch_root / finding.path
    project_path = project_root / finding.path
    try:
        scratch_bytes = _normalize(scratch_path.read_bytes())
        project_bytes = _normalize(project_path.read_bytes())
    except OSError as exc:
        return SkippedEdit(finding.path, dep_key, f"unreadable: {exc}")

    digest = hashlib.sha256(scratch_bytes).hexdigest()
    candidates = index.get(digest, [])
    if not candidates:
        return SkippedEdit(
            finding.path,
            dep_key,
            "deployed content is a transform of its source "
            "(no byte-identical source file); port the change manually",
        )
    if len(candidates) > 1:
        preview = ", ".join(candidates[:3])
        return SkippedEdit(
            finding.path,
            dep_key,
            f"ambiguous source: {len(candidates)} identical files ({preview})",
        )

    try:
        a_text = scratch_bytes.decode("utf-8")
        b_text = project_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return SkippedEdit(finding.path, dep_key, "binary or non-UTF-8 content")

    source_rel = candidates[0]
    diff_text = _unified_diff(a_text, b_text, source_rel)
    return ExportedEdit(finding.path, source_rel, dep_key), diff_text


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command(
    name="export-patch",
    help="Export local edits to APM-managed files as patches against their source packages",
)
@click.option(
    "--out",
    "-o",
    "out_dir",
    type=click.Path(file_okay=False),
    default="apm-patches",
    show_default=True,
    help="Directory to write per-package .patch files into",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="List what would be exported without writing patch files",
)
@click.option("--verbose", "-v", is_flag=True, help="Show replay progress and skip details")
def export_patch(out_dir, dry_run, verbose):
    """Export local edits to APM-managed files as upstream patches.

    Replays the locked install into a scratch directory (the same
    machinery as the 'apm audit' drift check), then re-expresses every
    locally modified managed file as a unified diff against the source
    file inside the package that deployed it. Each package with
    exportable edits gets one .patch file, applicable with 'git apply'
    from the package repository root at the base recorded in the patch
    header.

    Only verbatim-deployed files can be exported. Deployments that
    transform their source (frontmatter rewrites, compiled or aggregated
    outputs, resolved links) are listed as skipped with a reason.

    \b
    Examples:
        apm export-patch                # write patches to ./apm-patches/
        apm export-patch -o /tmp/out    # write patches elsewhere
        apm export-patch --dry-run      # preview without writing

    \b
    Exit codes:
        0  Success (including "nothing to export")
        1  Replay or export failed
    """
    logger = CommandLogger("export-patch", verbose=verbose, dry_run=dry_run)
    project_root = Path.cwd()

    if not (project_root / APM_YML_FILENAME).exists():
        logger.error(f"No {APM_YML_FILENAME} found. Run 'apm init' first.")
        sys.exit(1)

    lockfile_path = get_lockfile_path(project_root)
    lockfile = LockFile.read(lockfile_path) if lockfile_path.exists() else None
    if lockfile is None:
        logger.error("No lockfile found. Run 'apm install' first.")
        sys.exit(1)

    from ..deps.path_anchoring import LocalResolutionError
    from ..install.drift import (
        CacheMissError,
        CheckLogger,
        ReplayConfig,
        diff_scratch_against_project,
        run_replay,
    )
    from ..integration.targets import resolve_targets

    config = ReplayConfig(project_root=project_root, lockfile_path=lockfile_path)
    check_logger = CheckLogger(verbose=verbose)
    try:
        scratch = run_replay(config, check_logger)
    except CacheMissError as exc:
        logger.error(f"Cannot replay the locked install: {exc}")
        sys.exit(1)
    except (LocalResolutionError, NotImplementedError) as exc:
        logger.error(f"Drift replay failed: {exc}")
        sys.exit(1)

    targets = resolve_targets(project_root)
    findings = diff_scratch_against_project(scratch, project_root, lockfile, targets)
    modified_count = sum(1 for f in findings if f.kind == "modified")
    if modified_count == 0:
        logger.success("No local edits to managed files detected; nothing to export.")
        return

    export = build_patch_export(project_root, scratch, lockfile, findings)

    for skip in export.skipped:
        logger.warning(f"skipped {skip.deployed_path}: {skip.reason}")

    if not export.exported:
        logger.warning(
            f"{modified_count} modified managed file(s) found, but none could be "
            "exported as a patch (see reasons above)."
        )
        return

    for edit in export.exported:
        logger.progress(f"{edit.deployed_path} -> {edit.package}:{edit.source_path}")

    if dry_run:
        logger.success(
            f"Dry run: would write {len(export.patches)} patch file(s) "
            f"covering {len(export.exported)} edit(s)."
        )
        return

    out_path = Path(out_dir)
    try:
        out_path.mkdir(parents=True, exist_ok=True)
        for dep_key, patch_text in sorted(export.patches.items()):
            target = out_path / patch_filename(dep_key)
            target.write_bytes(patch_text.encode("utf-8"))
            logger.progress(f"wrote {target}")
    except OSError as exc:
        logger.error(f"Failed to write patch files: {exc}")
        sys.exit(1)

    logger.success(
        f"Exported {len(export.exported)} edit(s) into {len(export.patches)} "
        f"patch file(s) under {out_path}. Apply each with 'git apply' from the "
        "package repository root at the base noted in the patch header."
    )
