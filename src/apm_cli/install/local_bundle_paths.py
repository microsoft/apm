"""Path routing helpers for local bundle installation."""

from __future__ import annotations

from typing import Any


def bundle_pack_files(bundle_info: Any) -> dict[str, str]:
    """Read the packed file manifest from bundle lockfile metadata."""
    if not bundle_info.lockfile:
        return {}
    pack = bundle_info.lockfile.get("pack") or {}
    bundle_files = pack.get("bundle_files") or {}
    if not isinstance(bundle_files, dict):
        return {}
    return {str(path): str(digest) for path, digest in bundle_files.items()}


def known_bundle_deploy_prefixes() -> set[str]:
    """Return all target-owned roots that can prefix packed bundle paths."""
    from ..integration.targets import KNOWN_TARGETS

    return {
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
    }


def bundle_deploy_relative_path(
    rel_path: str,
    target: Any,
    known_deploy_prefixes: set[str],
) -> str | None:
    """Return a bundle path relative to a target-owned deployment root."""
    allowed_prefixes = {
        f"{root.strip('/')}/"
        for root in (
            target.root_dir,
            *(mapping.deploy_root for mapping in target.primitives.values() if mapping.deploy_root),
        )
    }
    matched_prefixes = {prefix for prefix in known_deploy_prefixes if rel_path.startswith(prefix)}
    allowed_matches = matched_prefixes & allowed_prefixes
    if matched_prefixes and not allowed_matches:
        return None
    matched_prefix = max(allowed_matches, key=len, default="")
    return rel_path[len(matched_prefix) :] if matched_prefix else rel_path
