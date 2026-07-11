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
finding is exportable iff the replayed (expected) content matches
exactly one file in the owning package's source tree AND that source
file is byte-identical to the replayed content (not merely equal after
drift normalization). The second condition guarantees the emitted diff
applies to the raw on-disk source: a CRLF-, BOM-, or Build-ID-bearing
source would digest-match in normalized space yet reject every hunk at
``git apply`` time, so those are skipped with an accurate reason.
Deployments that transform their source (frontmatter rewrites,
aggregated or compiled outputs, resolved links) fail the digest match
and are reported as skipped instead of producing a patch that would
not apply.

The command consumes the replay/diff APIs read-only; it never mutates
the project tree, the cache, or the lockfile. Patch files are the only
output.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import sys
from dataclasses import dataclass
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
    from ..integration.targets import TargetProfile

# Source files above this size are never indexed as reverse-mapping
# candidates. Managed primitives are small text files; a larger blob is
# noise at best and a memory hazard at worst.
_MAX_INDEXED_BYTES = 2 * 1024 * 1024

# Mirror of utils/content_hash.py walk exclusions: these can appear in a
# materialized package tree but are never package content.
_EXCLUDED_DIRS = {".git", "__pycache__"}

# Windows reserved device names cannot be used as file stems.
_RESERVED_STEMS = (
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


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
    patches: dict[str, str]
    exported: list[ExportedEdit]
    skipped: list[SkippedEdit]


@dataclass(frozen=True)
class _SourceIndex:
    """Reverse-mapping index over one package's source tree."""

    by_digest: dict[str, list[str]]  # sha256(normalized bytes) -> source rel paths
    unindexed: tuple[str, ...]  # rel paths excluded by size cap or read errors


def patch_filename(dep_key: str) -> str:
    """Map a lockfile dependency key to a filesystem-safe patch filename.

    Lossy (many keys can share one name); use :func:`patch_filenames`
    when writing a batch so collisions are disambiguated.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", dep_key).strip("-.") or "package"
    if safe.lower() in _RESERVED_STEMS:
        safe = f"pkg-{safe}"
    return f"{safe}.patch"


def patch_filenames(dep_keys: list[str]) -> dict[str, str]:
    """Assign a unique patch filename to every dependency key.

    The sanitizer is many-to-one, so colliding keys get a short digest
    suffix; without this, one package's patch would silently overwrite
    another's.
    """
    by_name: dict[str, list[str]] = {}
    for key in dep_keys:
        by_name.setdefault(patch_filename(key), []).append(key)
    result: dict[str, str] = {}
    for name, keys in by_name.items():
        if len(keys) == 1:
            result[keys[0]] = name
            continue
        stem = name[: -len(".patch")]
        for key in keys:
            suffix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
            result[key] = f"{stem}-{suffix}.patch"
    return result


def _index_source_tree(source_root: Path) -> _SourceIndex:
    """Index a package source tree by normalized content digest.

    Multiple paths under one digest means the content alone cannot
    identify the source file (ambiguous -- the caller must skip).
    Symlinks and ``.git``/``__pycache__`` trees are never candidates
    (mirroring ``utils/content_hash.py``), and the root cache-pin
    marker is install metadata, not package content. Files excluded by
    the size cap or by read errors are recorded so skip reasons can
    stay accurate.
    """
    from ..install.cache_pin import MARKER_FILENAME

    by_digest: dict[str, list[str]] = {}
    unindexed: list[str] = []
    for path in sorted(source_root.rglob("*")):
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        rel_parts = path.relative_to(source_root).parts
        if any(part in _EXCLUDED_DIRS for part in rel_parts):
            continue
        if len(rel_parts) == 1 and path.name == MARKER_FILENAME:
            continue
        rel = "/".join(rel_parts)
        try:
            if path.stat().st_size > _MAX_INDEXED_BYTES:
                unindexed.append(rel)
                continue
            digest = hashlib.sha256(_normalize(path.read_bytes())).hexdigest()
        except OSError:
            unindexed.append(rel)
            continue
        by_digest.setdefault(digest, []).append(rel)
    return _SourceIndex(by_digest=by_digest, unindexed=tuple(unindexed))


def _diff_lines(text: str) -> list[str]:
    """Split ``text`` into lines using git's line model.

    ``str.splitlines`` also breaks on bare CR, form feed, U+2028, etc.,
    which git treats as ordinary bytes within a line -- using it would
    emit hunk headers whose line counts disagree with ``git apply``.
    Only ``\\n`` terminates a line here; a final unterminated line is
    kept without its newline so the caller can mark it.
    """
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _unified_diff(a_text: str, b_text: str, rel: str) -> str:
    """Build a ``git apply``-compatible unified diff for one file.

    Emits the ``\\ No newline at end of file`` marker where either side
    lacks a trailing newline, which :func:`difflib.unified_diff` does not
    produce on its own.
    """
    a_lines = _diff_lines(a_text)
    b_lines = _diff_lines(b_text)
    out: list[str] = []
    for line in difflib.unified_diff(a_lines, b_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}"):
        if line.endswith("\n"):
            out.append(line)
        else:
            out.append(line + "\n")
            out.append("\\ No newline at end of file\n")
    return "".join(out)


def _header_value(value: object) -> str:
    """Sanitize a lockfile-sourced value for a single-line patch header.

    Control characters (including newlines) are collapsed to a space:
    header text is only inert to ``git apply`` while it stays on one
    ``#`` comment line, and these fields originate from registries and
    manifests, not from the user.
    """
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()


def _strip_userinfo(url: str) -> str:
    """Drop URL userinfo (``user:token@host``) before it reaches a header.

    Private-registry and authenticated-source URLs can legally embed
    credentials; a patch file is made to be shared, so they must never
    be written into it.
    """
    if "://" not in url:
        return url
    return re.sub(r"(?<=//)[^/@]*@", "", url)


def _base_label(dep: LockedDependency) -> str:
    """Human-readable description of the snapshot the patch applies to."""
    if dep.source == "registry":
        parts = [f"version {_header_value(dep.version or 'unknown')}"]
        if dep.resolved_url:
            parts.append(f"({_header_value(_strip_userinfo(dep.resolved_url))})")
        return " ".join(parts)
    if dep.resolved_commit:
        label = f"commit {_header_value(dep.resolved_commit)}"
        if dep.resolved_ref:
            label += f" ({_header_value(dep.resolved_ref)})"
        return label
    return f"version {_header_value(dep.version or 'unknown')}"


def _patch_header(dep_key: str, dep: LockedDependency) -> str:
    """Leading comment block for a per-package patch document.

    ``git apply`` ignores text before the first ``---``/``+++`` header,
    so the metadata rides inside the patch file itself.
    """
    lines = [
        "# Exported by 'apm export-patch'",
        f"# package: {_header_value(dep_key)}",
        f"# source: {_header_value(_strip_userinfo(dep.source_url or dep.repo_url))}",
        f"# base: {_base_label(dep)}",
        "# Apply from the package repository root, checked out at the",
        "# base above:  git apply <this file>",
    ]
    return "\n".join(lines) + "\n"


def _dir_prefix_table(lockfile: LockFile) -> list[tuple[str, str]]:
    """Trailing-slash ``deployed_files`` entries as (prefix, key) pairs.

    Longest prefix first, so nested dir entries attribute to the most
    specific owner. Covers legacy lockfiles that track a skill directory
    rather than every file inside it; ``local_deployed_files`` dirs map
    to the self key so project-local content keeps its accurate skip
    reason.
    """
    from ..deps.lockfile import _SELF_KEY

    pairs: list[tuple[str, str]] = []
    for key, dep in lockfile.dependencies.items():
        for tracked in dep.deployed_files or []:
            if tracked.endswith("/"):
                pairs.append((tracked, key))
    for tracked in lockfile.local_deployed_files or []:
        if tracked.endswith("/"):
            pairs.append((tracked, _SELF_KEY))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return pairs


def _resolve_package_key(
    finding_path: str, package: str, dir_prefixes: list[tuple[str, str]]
) -> str:
    """Resolve the owning dependency key for a finding.

    Findings normally carry the key already; the fallback matches the
    longest tracked directory prefix.
    """
    if package:
        return package
    for prefix, key in dir_prefixes:
        if finding_path.startswith(prefix):
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

    dir_prefixes = _dir_prefix_table(lockfile)
    modified = [f for f in findings if f.kind == "modified"]
    by_package: dict[str, list[DriftFinding]] = {}
    for finding in modified:
        key = _resolve_package_key(finding.path, finding.package, dir_prefixes)
        by_package.setdefault(key, []).append(finding)

    apm_modules_dir = project_root / "apm_modules"

    for key in sorted(by_package):
        group = by_package[key]

        def skip_group(reason: str, *, key: str = key, group: list = group) -> None:
            result_skipped.extend(SkippedEdit(f.path, key, reason) for f in group)

        if not key:
            skip_group("not tracked to a package in the lockfile")
            continue

        dep = lockfile.get_dependency(key)
        if key == _SELF_KEY or dep is None:
            skip_group("project-local content; the source already lives in this project")
            continue
        if dep.source == "local" or dep.local_path:
            skip_group(f"local package ({dep.local_path}); edit the source file directly")
            continue

        try:
            source_root = _materialize_install_path(
                dep, project_root, apm_modules_dir, cache_only=True, lockfile=lockfile
            )
        except CacheMissError as exc:
            skip_group(f"package cache unavailable: {exc}")
            continue

        index = _index_source_tree(source_root)
        # (deployed path, source rel, diff text) per successful mapping;
        # grouped afterwards so several deployed copies of one source
        # file cannot emit clashing hunks into a single patch.
        successes: list[tuple[str, str, str]] = []
        for finding in sorted(group, key=lambda f: f.path):
            edit = _export_one(finding, key, index, scratch_root, project_root, source_root)
            if isinstance(edit, SkippedEdit):
                result_skipped.append(edit)
                continue
            successes.append(edit)

        by_source: dict[str, list[tuple[str, str, str]]] = {}
        for success in successes:
            by_source.setdefault(success[1], []).append(success)

        file_diffs: list[str] = []
        for source_rel in sorted(by_source):
            copies = by_source[source_rel]
            unique_diffs = {diff for _, _, diff in copies}
            if len(unique_diffs) > 1:
                paths = ", ".join(deployed for deployed, _, _ in copies)
                result_skipped.extend(
                    SkippedEdit(
                        deployed,
                        key,
                        f"conflicting edits across {len(copies)} deployed copies of "
                        f"{source_rel} ({paths}); reconcile them and re-run",
                    )
                    for deployed, _, _ in copies
                )
                continue
            # Identical edits in every deployed copy map to one source
            # change: emit the diff once, report every copy as exported.
            file_diffs.append(copies[0][2])
            result_exported.extend(
                ExportedEdit(deployed, source_rel, key) for deployed, _, _ in copies
            )

        if file_diffs:
            patches[key] = _patch_header(key, dep) + "".join(file_diffs)

    return PatchExport(patches=patches, exported=result_exported, skipped=result_skipped)


def _export_one(
    finding: DriftFinding,
    dep_key: str,
    index: _SourceIndex,
    scratch_root: Path,
    project_root: Path,
    source_root: Path,
) -> SkippedEdit | tuple[str, str, str]:
    """Map one ``modified`` finding to (deployed, source rel, diff text).

    Returns a :class:`SkippedEdit` with the reason when the finding
    cannot be exported.
    """
    scratch_path = scratch_root / finding.path
    project_path = project_root / finding.path
    try:
        scratch_bytes = _normalize(scratch_path.read_bytes())
        project_bytes = _normalize(project_path.read_bytes())
    except OSError as exc:
        return SkippedEdit(finding.path, dep_key, f"unreadable: {exc}")

    digest = hashlib.sha256(scratch_bytes).hexdigest()
    candidates = index.by_digest.get(digest, [])
    if not candidates:
        reason = (
            "deployed content does not match any source file byte-for-byte "
            "(transformed during deployment); port the change manually"
        )
        if index.unindexed:
            reason += (
                f" -- note: {len(index.unindexed)} source file(s) were not "
                "indexed (size cap or unreadable)"
            )
        return SkippedEdit(finding.path, dep_key, reason)
    if len(candidates) > 1:
        preview = ", ".join(candidates[:3])
        return SkippedEdit(
            finding.path,
            dep_key,
            f"ambiguous source: {len(candidates)} identical files ({preview})",
        )

    source_rel = candidates[0]
    try:
        source_raw = (source_root / source_rel).read_bytes()
    except OSError as exc:
        return SkippedEdit(finding.path, dep_key, f"source unreadable: {exc}")
    # The digest matched in normalized space; the patch must apply to the
    # RAW source file. Only a normalization-clean source (no CRLF, BOM, or
    # Build-ID header) guarantees the normalized diff context matches the
    # on-disk bytes -- otherwise git apply would reject every hunk.
    if _normalize(source_raw) != source_raw:
        return SkippedEdit(
            finding.path,
            dep_key,
            f"source file {source_rel} contains CRLF line endings, a BOM, or a "
            "build-id header; the exported diff would not apply to it -- port "
            "the change manually",
        )

    try:
        a_text = scratch_bytes.decode("utf-8")
        b_text = project_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return SkippedEdit(finding.path, dep_key, "binary or non-UTF-8 content")

    diff_text = _unified_diff(a_text, b_text, source_rel)
    return finding.path, source_rel, diff_text


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


def _resolve_diff_targets(project_root: Path) -> list[TargetProfile]:
    """Resolve targets for the diff phase, honoring apm.yml ``target:``.

    Must match how the replay resolves targets (drift.py, #1924):
    auto-detection alone would skip declared targets whose root
    directory does not exist, silently hiding their findings.
    """
    from ..install.drift import _read_apm_yml_target
    from ..integration.targets import resolve_targets

    return resolve_targets(project_root, explicit_target=_read_apm_yml_target(project_root))


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
    try:
        _run_export_patch(logger, out_dir, dry_run, verbose)
    except SystemExit:
        raise
    except Exception as exc:
        logger.error(f"Error exporting patches: {exc}")
        sys.exit(1)


def _run_export_patch(logger: CommandLogger, out_dir: str, dry_run: bool, verbose: bool) -> None:
    project_root = Path.cwd()

    if not (project_root / APM_YML_FILENAME).exists():
        logger.error(f"No {APM_YML_FILENAME} found. Run 'apm init' first.")
        sys.exit(1)

    lockfile_path = get_lockfile_path(project_root)
    if not lockfile_path.exists():
        logger.error("No lockfile found. Run 'apm install' first.")
        sys.exit(1)
    lockfile = LockFile.read(lockfile_path)
    if lockfile is None:
        logger.error(
            f"Lockfile at {lockfile_path} could not be parsed; fix it or re-run 'apm install'."
        )
        sys.exit(1)

    out_path = Path(out_dir)
    resolved_out = (
        out_path.resolve() if out_path.is_absolute() else (project_root / out_path).resolve()
    )
    apm_modules_dir = (project_root / "apm_modules").resolve()
    if resolved_out == apm_modules_dir or apm_modules_dir in resolved_out.parents:
        logger.error("--out must not point inside apm_modules/ (the install cache).")
        sys.exit(1)

    remote_deps = [
        dep
        for dep in lockfile.dependencies.values()
        if dep.source != "local" and not dep.local_path
    ]
    if not remote_deps:
        logger.success(
            "All dependencies are project-local or local-path packages; "
            "their sources already live on disk, so there is nothing to "
            "export upstream."
        )
        return

    from ..deps.path_anchoring import LocalResolutionError
    from ..install.drift import (
        CacheMissError,
        CheckLogger,
        ReplayConfig,
        diff_scratch_against_project,
        run_replay,
    )

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

    targets = _resolve_diff_targets(project_root)
    findings = diff_scratch_against_project(scratch, project_root, lockfile, targets)

    export = build_patch_export(project_root, scratch, lockfile, findings)

    for skip in export.skipped:
        logger.warning(f"skipped {skip.deployed_path}: {skip.reason}")

    if not export.exported and not export.skipped:
        logger.success("No local edits to managed files detected; nothing to export.")
        return

    if not export.exported:
        logger.warning(
            f"{len(export.skipped)} modified managed file(s) found, but none "
            "could be exported as a patch (see reasons above)."
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

    from ..utils.atomic_io import atomic_write_text

    filenames = patch_filenames(sorted(export.patches))
    try:
        resolved_out.mkdir(parents=True, exist_ok=True)
        for dep_key in sorted(export.patches):
            target = resolved_out / filenames[dep_key]
            atomic_write_text(target, export.patches[dep_key])
            logger.progress(f"wrote {target}")
    except OSError as exc:
        logger.error(f"Failed to write patch files: {exc}")
        sys.exit(1)

    logger.success(
        f"Exported {len(export.exported)} edit(s) into {len(export.patches)} "
        f"patch file(s) under {resolved_out}. Apply each with 'git apply' from "
        "the package repository root at the base noted in the patch header."
    )
