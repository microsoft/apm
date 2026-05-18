"""URL and path predicates extracted from UnifiedLinkResolver.

Extracted from link_resolver to keep that module under 400 lines.
Module-level functions are stateless; callers pass explicit parameters
instead of relying on instance state.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse


def is_external_url(path: str) -> bool:
    """Check if path is an external URL.

    Security: Only http/https URLs with valid netloc are considered external.
    All other schemes (javascript:, data:, file:, etc.) are treated as internal
    paths to prevent potential security issues.

    Args:
        path: Path to check

    Returns:
        True if external URL (http/https with valid netloc)
    """
    try:
        path = path.strip()
        parsed = urlparse(path)
        if parsed.scheme not in ("http", "https"):
            return False
        if not parsed.netloc:  # noqa: SIM103
            return False
        return True
    except Exception:
        return False


def is_context_file(path: str, extensions: frozenset[str]) -> bool:
    """Check if path is a context or memory file.

    Args:
        path: Path to check
        extensions: Set of file extensions that qualify as context files

    Returns:
        True if context/memory file
    """
    path_lower = path.lower()
    return any(path_lower.endswith(ext) for ext in extensions)


def resolve_to_actual_file(
    link_path: str,
    source_file: Path,
    context_registry: dict[str, Path],
    base_dir: Path,
) -> Path | None:
    """Resolve a link path to the actual file on disk.

    Args:
        link_path: Link path from markdown
        source_file: File containing the link
        context_registry: Mapping of filename to resolved path
        base_dir: Project base directory for fallback resolution

    Returns:
        Resolved file path or None
    """
    filename = Path(link_path).name

    if filename in context_registry:
        return context_registry[filename]

    if source_file.is_file():  # noqa: SIM108
        source_dir = source_file.parent
    else:
        source_dir = source_file

    potential_path = (source_dir / link_path).resolve()
    if potential_path.exists():
        return potential_path

    potential_path = (base_dir / link_path).resolve()
    if potential_path.exists():
        return potential_path

    return None
