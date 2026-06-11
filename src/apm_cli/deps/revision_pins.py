"""Revision-pin update helpers for ``apm update`` and ``apm outdated``."""

from __future__ import annotations

import re
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, RemoteRef
from apm_cli.utils.console import STATUS_SYMBOLS

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockedDependency

_SHA_RE = re.compile(r"^[a-fA-F0-9]{40}$")
_SHA_DISPLAY_LEN = 8


class RevisionPinResolutionError(RuntimeError):
    """Raised when a SHA pin cannot be safely mapped to an annotated tag."""


class RemoteRefDownloader(Protocol):
    """Downloader surface needed for authoritative remote tag checks."""

    def list_remote_tag_refs(self, dep_ref: DependencyReference) -> Iterable[RemoteRef]:
        """List tag refs from the dependency's authoritative upstream."""
        ...


@dataclass(frozen=True)
class AnnotatedTagCandidate:
    """Latest annotated tag candidate for a revision-pinned dependency."""

    tag: str
    commit_sha: str


@dataclass(frozen=True)
class RevisionPinUpdate:
    """Manifest change needed to move one SHA pin to an annotated tag."""

    dep_key: str
    old_sha: str
    new_sha: str
    tag: str
    display_name: str


def is_full_revision_pin(ref: str | None) -> bool:
    """Return True when *ref* is a full 40-character commit SHA."""
    return bool(ref and _SHA_RE.match(ref.strip()))


def abbreviate_sha(sha: str | None) -> str:
    """Return the user-facing short SHA used by update/outdated."""
    return (sha or "")[:_SHA_DISPLAY_LEN]


def _package_name(dep_ref: DependencyReference) -> str:
    """Return the tag-pattern package name for *dep_ref*."""
    if dep_ref.is_virtual_subdirectory() and dep_ref.virtual_path:
        return dep_ref.virtual_path.rstrip("/").rsplit("/", 1)[-1]
    return dep_ref.repo_url.rstrip("/").rsplit("/", 1)[-1]


def find_latest_annotated_tag(
    remote_refs: Iterable[RemoteRef],
    *,
    package_name: str,
) -> AnnotatedTagCandidate:
    """Return the highest semver annotated tag from *remote_refs*.

    Branches and lightweight tags are deliberately ignored. A revision-pin
    update must be grounded in an annotated tag so a branch named like a
    release can never masquerade as the update target.
    """
    from apm_cli.deps.git_semver_resolver import DEFAULT_TAG_PATTERNS, FALLBACK_BARE_PATTERN
    from apm_cli.marketplace.semver import SemVer, parse_semver
    from apm_cli.marketplace.tag_pattern import build_tag_regex

    patterns = (*DEFAULT_TAG_PATTERNS, FALLBACK_BARE_PATTERN)
    candidates: list[tuple[SemVer, str, str]] = []
    for ref in remote_refs:
        if ref.ref_type != GitReferenceType.TAG:
            continue
        # Fail-closed security fence: reject branches and lightweight tags.
        # ``annotated`` is set true only when ls-remote emitted a peeled
        # ``^{}`` ref (see git_remote_ops.parse_ls_remote_output), which only
        # annotated tags produce. Skipping non-annotated refs here is what
        # stops a branch named like a release from being chosen as the
        # revision-pin update target.
        if not ref.annotated:
            continue
        for pattern in patterns:
            expanded = pattern.replace("{name}", package_name)
            match = build_tag_regex(expanded).match(ref.name)
            if not match:
                continue
            version = parse_semver(match.group("version"))
            if version is not None and not version.is_prerelease:
                candidates.append((version, ref.name, ref.commit_sha))
            break

    if not candidates:
        raise RevisionPinResolutionError(
            "No annotated tag found for revision-pinned dependency. "
            "APM will not replace a SHA pin with a branch or lightweight tag."
        )

    _, tag, sha = max(candidates, key=lambda item: item[0])
    return AnnotatedTagCandidate(tag=tag, commit_sha=sha)


def resolve_revision_pin_updates(
    dependencies: Iterable[DependencyReference],
    downloader: RemoteRefDownloader,
    *,
    only_packages: set[str] | None = None,
    max_workers: int = 4,
) -> list[RevisionPinUpdate]:
    """Resolve direct SHA-pinned dependencies to latest annotated tag SHAs."""
    eligible: list[DependencyReference] = []
    for dep_ref in dependencies:
        dep_key = dep_ref.get_unique_key()
        if only_packages is not None and dep_key not in only_packages:
            continue
        if dep_ref.is_local or dep_ref.source == "registry" or dep_ref.artifactory_prefix:
            continue
        if is_full_revision_pin(getattr(dep_ref, "reference", None)):
            eligible.append(dep_ref)

    if not eligible:
        return []

    def _resolve_one(dep_ref: DependencyReference) -> RevisionPinUpdate | None:
        dep_key = dep_ref.get_unique_key()
        old_sha = (dep_ref.reference or "").strip().lower()
        remote_refs = downloader.list_remote_tag_refs(dep_ref)
        latest = find_latest_annotated_tag(remote_refs, package_name=_package_name(dep_ref))
        latest_sha = latest.commit_sha.strip().lower()
        if not is_full_revision_pin(latest_sha):
            raise RevisionPinResolutionError(
                f"Remote returned an invalid SHA for {dep_key}: expected 40-character hex."
            )
        if latest_sha == old_sha:
            return None
        return RevisionPinUpdate(
            dep_key=dep_key,
            old_sha=old_sha,
            new_sha=latest_sha,
            tag=latest.tag,
            display_name=dep_key,
        )

    worker_count = max(1, min(max_workers, len(eligible)))
    if worker_count == 1:
        resolved = [_resolve_one(dep_ref) for dep_ref in eligible]
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            resolved = list(executor.map(_resolve_one, eligible))
    return [update for update in resolved if update is not None]


def render_revision_pin_update_plan(updates: Iterable[RevisionPinUpdate]) -> str:
    """Render revision-pin updates as an ASCII plan."""
    ordered = list(updates)
    if not ordered:
        return ""
    info_symbol = STATUS_SYMBOLS.get("info", "[i]")
    update_symbol = STATUS_SYMBOLS.get("update", "[~]")
    lines = [f"{info_symbol} Revision pin updates for apm.yml", ""]
    for update in ordered:
        lines.append(f"  {update_symbol} {update.display_name}")
        lines.append(
            f"      ref: {abbreviate_sha(update.old_sha)} -> {abbreviate_sha(update.new_sha)} ({update.tag})"
        )
        lines.append("")
    count = len(ordered)
    lines.append(f"  {count} revision pin {'update' if count == 1 else 'updates'}")
    return "\n".join(lines).rstrip()


def apply_revision_pin_updates(manifest_path: Path, updates: Iterable[RevisionPinUpdate]) -> None:
    """Rewrite SHA pins in *manifest_path* and annotate each with its tag."""
    manifest_path = Path(manifest_path)
    if manifest_path.name not in {"apm.yml", "apm.yaml"}:
        raise RevisionPinResolutionError(
            "Revision-pin updates can only rewrite an apm.yml manifest."
        )
    original = manifest_path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    for update in updates:
        lines = _replace_one_revision_pin_line(lines, update)
    new_content = "".join(lines)
    if new_content == original:
        return
    from apm_cli.utils.yaml_io import write_yaml_text_atomic

    write_yaml_text_atomic(
        manifest_path,
        new_content,
        tmp_suffix=".apm-update-pins.tmp",
    )


def _replace_one_revision_pin_line(
    lines: list[str],
    update: RevisionPinUpdate,
) -> list[str]:
    matches: list[int] = []
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if update.old_sha.lower() in line.lower():
            matches.append(idx)
    if len(matches) > 1:
        scoped_matches = [
            idx
            for idx in matches
            if update.dep_key in lines[idx] or update.display_name in lines[idx]
        ]
        if len(scoped_matches) == 1:
            matches = scoped_matches
    if len(matches) != 1:
        raise RevisionPinResolutionError(
            f"Expected exactly one apm.yml entry for {update.old_sha[:12]}, "
            f"found {len(matches)}. No manifest changes were written."
        )

    idx = matches[0]
    line = lines[idx]
    newline = "\n" if line.endswith("\n") else ""
    body = line[:-1] if newline else line
    old_idx = body.lower().index(update.old_sha.lower())
    prefix = body[:old_idx]
    suffix = body[old_idx + len(update.old_sha) :].strip()
    if suffix and not suffix.startswith("#"):
        raise RevisionPinResolutionError(
            f"Unexpected trailing content after SHA pin for {update.display_name}. "
            "No manifest changes were written."
        )
    if re.search(r"[\x00-\x1f\x7f]", update.tag):
        raise RevisionPinResolutionError(
            f"Unexpected control character in tag for {update.display_name}. "
            "No manifest changes were written."
        )
    lines[idx] = f"{prefix}{update.new_sha} # {update.tag}{newline}"
    return lines


def dependency_ref_from_locked(locked: LockedDependency) -> DependencyReference:
    """Rebuild a DependencyReference suitable for authoritative upstream checks.

    This named seam keeps ``apm outdated`` coupled to the revision-pin
    resolver contract instead of reaching through the lockfile type directly.
    """
    return locked.to_dependency_ref()
