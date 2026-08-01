"""Path routing helpers for local bundle installation."""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

from ..utils.path_security import PathTraversalError, validate_path_segments

if TYPE_CHECKING:
    from ..bundle.local_bundle import LocalBundleInfo
    from ..integration.targets import TargetProfile


def bundle_pack_files(bundle_info: LocalBundleInfo) -> dict[str, str]:
    """Read the packed file manifest from bundle lockfile metadata."""
    if not bundle_info.lockfile:
        return {}
    pack = bundle_info.lockfile.get("pack") or {}
    bundle_files = pack.get("bundle_files") or {}
    if not isinstance(bundle_files, dict):
        return {}
    return {str(path): str(digest) for path, digest in bundle_files.items()}


@cache
def known_bundle_deploy_prefixes() -> frozenset[str]:
    """Return all target-owned roots that can prefix packed bundle paths."""
    from ..integration.targets import KNOWN_TARGETS

    return frozenset(
        f"{root.strip('/')}/"
        for profile in KNOWN_TARGETS.values()
        for root in (
            profile.root_dir,
            *(
                mapping.deploy_root
                for mapping in profile.primitives.values()
                if mapping.deploy_root
            ),
        )
    )


def target_bundle_deploy_prefixes(target: TargetProfile) -> frozenset[str]:
    """Return deployment roots owned by one target."""
    return frozenset(
        f"{root.strip('/')}/"
        for root in (
            target.root_dir,
            *(mapping.deploy_root for mapping in target.primitives.values() if mapping.deploy_root),
        )
    )


def bundle_slug_validation_error(slug: object) -> str | None:
    """Return an error when a bundle slug is unsafe for path construction."""
    slug_text = str(slug)
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if (
        not slug_text
        or any(char not in allowed for char in slug_text)
        or slug_text.startswith(".")
        or slug_text.endswith(".")
        or ".." in slug_text
    ):
        return "slug must match [A-Za-z0-9._-]+ with no leading/trailing dot, no '..'"
    try:
        validate_path_segments(slug_text, context="bundle slug")
    except PathTraversalError as exc:
        return str(exc)
    return None


def bundle_deploy_relative_path(
    rel_path: str,
    allowed_prefixes: frozenset[str],
    known_deploy_prefixes: frozenset[str],
) -> str | None:
    """Return a bundle path relative to a target-owned deployment root."""
    validate_path_segments(rel_path, context="bundle deploy path")
    parts = rel_path.split("/")
    path_prefixes = {"/".join(parts[:index]) + "/" for index in range(1, len(parts))}
    matched_prefixes = path_prefixes & known_deploy_prefixes
    allowed_matches = matched_prefixes & allowed_prefixes
    if matched_prefixes and not allowed_matches:
        return None
    matched_prefix = max(allowed_matches, key=len, default="")
    return rel_path[len(matched_prefix) :] if matched_prefix else rel_path
