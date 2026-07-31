"""Drift diff engine -- compares a replay scratch tree against the project.

Extracted from ``install/drift.py`` to keep that module under the
repository file-length gate. ``drift.py`` re-exports every public and
private name defined here, so existing callers and tests that import
them from ``apm_cli.install.drift`` keep working unchanged (RULE B).

Cross-module calls route back through the ``drift`` module object via
``_d()`` rather than binding the sibling's own globals at import time.
That preserves the monkeypatch seam: a test patching
``apm_cli.install.drift._walk_managed`` still affects the comparison
performed here, exactly as it did before the split.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.utils.normalization import _normalize

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.drift import DriftFinding
    from apm_cli.integration.targets import TargetProfile


def _d():
    """Return the ``drift`` facade module (late import; preserves patching)."""
    from apm_cli.install import drift

    return drift


_INLINE_DIFF_BYTE_CAP = 100 * 1024  # 100 KB


def _governed_root_dirs(targets: list[TargetProfile]) -> set[str]:
    """Return the set of top-level managed directory names to walk.

    Includes each target's top-level ``root_dir`` (plus ``.apm``) AND every
    per-primitive ``deploy_root`` override (e.g. the ``copilot`` target routing
    ``skills`` to ``.agents``). Walking the deploy roots is what lets the drift
    differ compare committed skill bundles under ``.agents/skills/`` against the
    replay, closing the gap where deployed skill content could silently diverge
    from source (issue #1716). The replay reproduces the deploy-time link
    rewrite faithfully, so byte-identical skills do not surface as false drift.
    Only the first path segment is kept so nested deploy roots collapse to a
    single walk root.
    """
    roots: set[str] = {".apm"}
    for t in targets or []:
        root = getattr(t, "root_dir", None)
        if root:
            roots.add(str(root).split("/", 1)[0])
        primitives = getattr(t, "primitives", None) or {}
        for mapping in primitives.values():
            deploy_root = getattr(mapping, "deploy_root", None)
            if deploy_root:
                roots.add(str(deploy_root).split("/", 1)[0])
    return roots


def _walk_managed(root: Path, governed_roots: set[str]) -> dict[str, Path]:
    """Return a mapping of project-relative posix paths to absolute paths."""
    out: dict[str, Path] = {}
    if not root.exists():
        return out
    for top in governed_roots:
        base = root / top
        if not base.exists():
            continue
        if base.is_file():
            out[top] = base
            continue
        for p in base.rglob("*"):
            if p.is_file():
                rel = p.relative_to(root).as_posix()
                out[rel] = p
    # AGENTS.md is a flat top-level file in some target layouts.
    agents_md = root / "AGENTS.md"
    if agents_md.is_file():
        out["AGENTS.md"] = agents_md
    return out


def _claimed_prefixes(
    tracked: dict[str, str],
    hashed_files: set[str],
    project_files: dict[str, Path],
) -> tuple[str, ...]:
    """Return ``dir/`` prefixes for the tracked entries that name directories.

    A manifest entry may name a directory rather than each file beneath it
    (``scan_lockfile_packages`` and ``_check_deployed_files_present`` both
    accept that shape), so a file is claimed when a tracked *directory*
    covers it.

    Entries with a recorded hash are files even if the current project path was
    replaced by a directory. Entries present in *project_files* are also files,
    which preserves the same rule for legacy hashless lockfiles. A file claim
    must never act as a prefix: otherwise replacing tracked ``a.md`` with an
    ``a.md/`` directory would let every descendant dodge the membership check.
    """
    return tuple(
        normalized + "/"
        for path in tracked
        if (normalized := path.rstrip("/"))
        and normalized not in hashed_files
        and normalized not in project_files
    )


def _inline_diff_for(scratch_path: Path, project_path: Path) -> str:
    """Build an inline diff hint, capped to keep findings compact."""
    try:
        s_size = scratch_path.stat().st_size
        p_size = project_path.stat().st_size
    except OSError:
        return ""
    if s_size > _INLINE_DIFF_BYTE_CAP or p_size > _INLINE_DIFF_BYTE_CAP:
        return "(file too large for inline diff; use 'git diff --no-index' to compare)"
    return ""


def _canvas_deploy_prefixes(targets) -> set[str]:
    """Return ``root/subdir/`` prefixes for every target carrying a canvas mapping.

    Used to exclude canvas extension deploy paths from drift comparison
    (the replay deliberately does not re-integrate canvases).
    """
    prefixes: set[str] = set()
    for target in targets or []:
        mapping = getattr(target, "primitives", {}).get("canvas")
        if mapping is None:
            continue
        effective_root = mapping.deploy_root or target.root_dir
        if mapping.subdir:
            prefixes.add(f"{effective_root}/{mapping.subdir}/")
        else:
            prefixes.add(f"{effective_root}/")
    return prefixes


def diff_scratch_against_project(
    scratch_root: Path,
    project_root: Path,
    lockfile: LockFile,
    targets,
    *,
    tracked_files: frozenset[str] | None = None,
) -> list[DriftFinding]:
    """Compare the replay scratch tree against the project tree.

    Four kinds of findings are emitted:

    * ``modified``     -- file exists in both, normalized content differs.
    * ``unintegrated`` -- file exists in scratch but not in project, and the
      path is part of the committed working tree when git tracking is known.
    * ``orphaned``     -- file exists in project + tracked in lockfile
      ``deployed_files`` but no longer in scratch.
    * ``unrecorded``   -- the replay deploys the file and the project has it,
      but no lockfile entry claims it: the committed manifest under-records.

    ``unrecorded`` is the mirror image of ``orphaned``. Both are membership
    findings, and between them they make manifest membership symmetric:
    ``orphaned`` catches a claim with nothing behind it, ``unrecorded``
    catches a deployed file with no claim in front of it. The latter matters
    because manifest membership defines the scope of every downstream
    lockfile-driven gate -- ``content-integrity`` hashes and Unicode-scans
    exactly the recorded set, so a file missing from it is exempt from those
    checks for as long as it stays missing (issue #2379).

    Untracked extra files in governed directories that the replay does NOT
    produce are still ignored -- those are user-authored content, and APM
    deploying the file is what makes a missing claim a defect. Hook merge
    targets are exempt for the same reason in reverse: APM writes into them
    but shares them with the user, so it never claims them.
    """
    scratch_root = scratch_root.resolve()
    project_root = project_root.resolve()
    governed = _d()._governed_root_dirs(targets)
    scratch_files = _d()._walk_managed(scratch_root, governed)
    project_files = _d()._walk_managed(project_root, governed)
    tracked = _d()._collect_tracked_files(lockfile)
    claimed_prefixes = _claimed_prefixes(
        tracked,
        _d()._collect_hashed_files(lockfile),
        project_files,
    )
    # Hook merge targets (.claude/settings.json, .cursor/hooks.json, and their
    # apm-hooks.json sidecars) are shared with the user and never claimed in
    # deployed_files, so they can never be "unrecorded".
    from apm_cli.install.manifest_reconcile import merge_hook_config_paths

    merge_config_paths = merge_hook_config_paths(targets)

    # Imperative local bundles have no authored source tree for replay. Their
    # deployed bytes are already bound by local_deployed_file_hashes and the
    # policy/ci_checks.py::_check_content_integrity check, so comparing them to
    # an empty scratch projection would misclassify every clean bundle file as
    # orphaned.
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    local_bundle_paths = DeploymentLedgerCodec.local_bundle_paths(lockfile)
    if local_bundle_paths:
        scratch_files = {
            relative_path: path
            for relative_path, path in scratch_files.items()
            if relative_path not in local_bundle_paths
        }
        project_files = {
            relative_path: path
            for relative_path, path in project_files.items()
            if relative_path not in local_bundle_paths
        }

    # Canvas extensions are executable bundles that the drift replay does
    # not re-integrate (their integrator is intentionally omitted from the
    # replay bundle). Exclude their deploy prefixes from BOTH trees so a
    # deployed canvas is never mis-reported as orphaned/unintegrated. Full
    # canvas drift detection is a deferred follow-up.
    _canvas_prefixes = _d()._canvas_deploy_prefixes(targets)
    if _canvas_prefixes:

        def _is_canvas(rel: str) -> bool:
            norm = rel.replace("\\", "/")
            return any(norm.startswith(p) for p in _canvas_prefixes)

        scratch_files = {r: p for r, p in scratch_files.items() if not _is_canvas(r)}
        project_files = {r: p for r, p in project_files.items() if not _is_canvas(r)}

    findings: list[DriftFinding] = []

    for rel, scratch_path in sorted(scratch_files.items()):
        project_path = project_files.get(rel)
        if project_path is None:
            if tracked_files is not None and rel not in tracked_files:
                continue
            findings.append(
                _d().DriftFinding(
                    path=rel,
                    kind="unintegrated",
                    package=tracked.get(rel, ""),
                )
            )
            continue
        try:
            s_bytes = _normalize(scratch_path.read_bytes())
            p_bytes = _normalize(project_path.read_bytes())
        except OSError as exc:
            findings.append(
                _d().DriftFinding(
                    path=rel,
                    kind="modified",
                    package=tracked.get(rel, ""),
                    inline_diff=f"(read error: {exc})",
                )
            )
            continue
        if s_bytes != p_bytes:
            findings.append(
                _d().DriftFinding(
                    path=rel,
                    kind="modified",
                    package=tracked.get(rel, ""),
                    inline_diff=_d()._inline_diff_for(scratch_path, project_path),
                )
            )
        elif not (rel in tracked or rel.startswith(claimed_prefixes) or rel in merge_config_paths):
            # Content agrees, so nothing else in the audit will ever look at
            # this file again: no manifest entry means no hash baseline and no
            # Unicode scan. Reported only in the content-clean case -- when the
            # bytes differ, ``modified`` already fails the check and 'apm
            # install' is the same remedy for both.
            findings.append(_d().DriftFinding(path=rel, kind="unrecorded", package=""))

    for rel in sorted(project_files.keys()):
        if rel in scratch_files:
            continue
        if rel in tracked:
            findings.append(
                _d().DriftFinding(
                    path=rel,
                    kind="orphaned",
                    package=tracked.get(rel, ""),
                )
            )
        # else: untracked governed file -- ignore (user authored).

    return findings
