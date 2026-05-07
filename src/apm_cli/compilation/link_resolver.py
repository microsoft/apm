"""Context link resolution for APM primitives.

Resolves markdown links to context files across the APM lifecycle:
- Installation: Rewrite links when copying from dependencies
- Compilation: Rewrite links when generating AGENTS.md
- Runtime: Resolve links when executing prompts

Following KISS principle - simple, pragmatic implementation.
"""

import builtins
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set  # noqa: F401, UP035
from urllib.parse import urlparse

from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

# CRITICAL: Shadow Click commands to prevent namespace collision
set = builtins.set
list = builtins.list
dict = builtins.dict


@dataclass
class LinkResolutionContext:
    """Context for resolving links during different APM operations."""

    source_file: Path  # File containing the link
    source_location: Path  # Original location (directory)
    target_location: Path  # Where file will live (directory or file)
    base_dir: Path  # Project root
    available_contexts: builtins.dict[str, Path]  # Map of context name -> actual path
    # Authoritative source-package root (e.g. apm_modules/<owner>/<repo>/ or
    # apm_modules/_local/<name>/). When set, in-package asset links may be
    # rewritten to point at the package's install location. None disables
    # generalized asset rewriting (compile path, legacy callers).
    package_root: Path | None = None
    # Whether to attempt generalized in-package asset link rewriting (#1147).
    # Only enabled by ``resolve_links_for_installation`` where source/target
    # are a true 1:1 pair. Compilation must leave this False because the
    # source_file is a synthetic AGENTS.md output dir, not per-link provenance.
    enable_asset_rewrite: bool = False


class UnifiedLinkResolver:
    """Resolves markdown links across all APM operations.

    Simple implementation focusing on:
    - Registering available context files from .apm/ and apm_modules/
    - Rewriting links to point directly to source locations
    - No copying needed - links point to actual files
    """

    # Regex for markdown links: [text](path)
    LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

    # Context file extensions we handle
    CONTEXT_EXTENSIONS = {".context.md", ".memory.md"}  # noqa: RUF012

    def __init__(self, base_dir: Path):
        """Initialize link resolver.

        Args:
            base_dir: Project root directory
        """
        self.base_dir = Path(base_dir)
        self.context_registry: builtins.dict[str, Path] = {}
        # Authoritative source-package root, set by integrators after
        # init_link_resolver(). Used by generalized in-package asset
        # rewriting (#1147). None for compile / legacy callers disables
        # the generalization safely.
        self.package_root: Path | None = None

    def register_contexts(self, primitives) -> None:
        """Build registry of all available context files.

        Registers contexts by:
        1. Simple filename: "api-standards.context.md" -> path
        2. Qualified name (for dependencies): "company/standards:api.context.md" -> path

        Args:
            primitives: Collection of discovered primitives (PrimitiveCollection)
        """
        for context in primitives.contexts:
            filename = context.file_path.name

            # Register by simple filename
            self.context_registry[filename] = context.file_path

            # If from dependency, also register with qualified name
            if context.source and context.source.startswith("dependency:"):
                package = context.source.replace("dependency:", "")
                qualified_name = f"{package}:{filename}"
                self.context_registry[qualified_name] = context.file_path

    def resolve_links_for_installation(
        self, content: str, source_file: Path, target_file: Path
    ) -> str:
        """Resolve links when copying files during installation.

        Called when copying .prompt.md/.agent.md/.instructions.md from
        ``apm_modules/`` to the host's deploy directory (e.g. ``.github/``).

        Two rewrite passes apply:

        1. Context/memory link rewriting (existing behaviour).
        2. Generalized in-package asset link rewriting (#1147), enabled
           when ``self.package_root`` is set. Rewrites any relative link
           whose target file exists inside the source package tree to a
           stable path under ``apm_modules/`` so the deployed file's
           sibling references survive the host-tool path split.

        Args:
            content: File content to process
            source_file: Original file path in apm_modules/
            target_file: Target path in .github/

        Returns:
            Content with resolved links
        """
        ctx = LinkResolutionContext(
            source_file=source_file,
            source_location=source_file.parent,
            target_location=target_file.parent,
            base_dir=self.base_dir,
            available_contexts=self.context_registry,
            package_root=self.package_root,
            enable_asset_rewrite=self.package_root is not None,
        )

        return self._rewrite_markdown_links(content, ctx)

    def resolve_links_for_compilation(
        self, content: str, source_file: Path, compiled_output: Path | None = None
    ) -> str:
        """Resolve links when generating AGENTS.md.

        Links are rewritten to point directly to source files in:
        - .apm/context/ (local contexts)
        - apm_modules/org/repo/.apm/context/ (dependency contexts)

        Args:
            content: Content to process
            source_file: Source file or directory
            compiled_output: Where AGENTS.md will be written

        Returns:
            Content with resolved links
        """
        # If compiled_output is None, use source_file directory
        if compiled_output is None:
            compiled_output = source_file if source_file.is_dir() else source_file.parent

        # If compiled_output is a file, use its parent directory
        if compiled_output.is_file() or str(compiled_output).endswith(".md"):
            target_location = compiled_output.parent
        else:
            target_location = compiled_output

        ctx = LinkResolutionContext(
            source_file=source_file,
            source_location=source_file if source_file.is_dir() else source_file.parent,
            target_location=target_location,
            base_dir=self.base_dir,
            available_contexts=self.context_registry,
            # Compilation must NOT enable asset rewrite: source_file here is
            # a synthetic AGENTS.md output dir aggregating multiple sources,
            # so per-link source provenance is lost. Generalized rewriting
            # would mis-resolve consumer-repo-relative links. (#1147)
            package_root=None,
            enable_asset_rewrite=False,
        )

        return self._rewrite_markdown_links(content, ctx)

    def get_referenced_contexts(self, all_files_to_scan: builtins.list[Path]) -> builtins.set[Path]:
        """Scan files for context references (for reporting/validation).

        Args:
            all_files_to_scan: Files to scan for context references

        Returns:
            Set of referenced context file paths
        """
        referenced_contexts: builtins.set[Path] = builtins.set()

        for file_path in all_files_to_scan:
            if not file_path.exists():
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
                refs = self._extract_context_references(content, file_path)
                referenced_contexts.update(refs)
            except Exception:  # noqa: S112
                continue

        return referenced_contexts

    def _rewrite_markdown_links(self, content: str, ctx: LinkResolutionContext) -> str:
        """Core link rewriting logic.

        Process markdown links and rewrite:

        1. Context/memory file references (existing behaviour, all callers).
        2. In-package asset references (#1147), enabled only when
           ``ctx.enable_asset_rewrite`` is True and ``ctx.package_root`` is
           set. Skipped otherwise to preserve compile/legacy semantics.

        Args:
            content: Content to process
            ctx: Resolution context

        Returns:
            Content with rewritten links
        """

        def replace_link(match):
            link_text = match.group(1)
            link_path = match.group(2)

            # Skip external URLs
            if self._is_external_url(link_path):
                return match.group(0)  # Return unchanged

            # Context / memory files: existing behaviour
            if self._is_context_file(link_path):
                resolved_path = self._resolve_context_link(link_path, ctx)
                if resolved_path:
                    return f"[{link_text}]({resolved_path})"
                return match.group(0)

            # Generalized in-package asset link rewriting (#1147).
            # Strictly opt-in: requires both the context flag AND a
            # package_root, which only ``resolve_links_for_installation``
            # provides. Compile callers leave both unset.
            if ctx.enable_asset_rewrite and ctx.package_root is not None:
                if not self._is_rewritable_relative_link(link_path):
                    return match.group(0)
                rewritten = self._resolve_in_package_asset_link(link_path, ctx)
                if rewritten:
                    return f"[{link_text}]({rewritten})"
                return match.group(0)

            return match.group(0)

        return self.LINK_PATTERN.sub(replace_link, content)

    def _extract_context_references(self, content: str, source_file: Path) -> builtins.set[Path]:
        """Extract all context file references from content.

        Args:
            content: Content to scan
            source_file: File containing the content

        Returns:
            Set of resolved context file paths
        """
        references: builtins.set[Path] = builtins.set()

        for match in self.LINK_PATTERN.finditer(content):
            link_path = match.group(2)

            # Skip external URLs and non-context files
            if self._is_external_url(link_path) or not self._is_context_file(link_path):
                continue

            # Try to resolve to actual file path
            resolved = self._resolve_to_actual_file(link_path, source_file)
            if resolved and resolved.exists():
                references.add(resolved)

        return references

    def _resolve_context_link(self, link_path: str, ctx: LinkResolutionContext) -> str | None:
        """Resolve a context link to point directly to source file.

        Links point to actual source locations:
        - .apm/context/file.context.md (local)
        - apm_modules/org/repo/.apm/context/file.context.md (dependency)

        Args:
            link_path: Original link path
            ctx: Resolution context

        Returns:
            Resolved relative path to actual source file, or None if can't resolve
        """
        # Find the actual source file
        actual_file = self._resolve_to_actual_file(link_path, ctx.source_file)

        if not actual_file or not actual_file.exists():
            # Can't find the file - preserve original link
            return None

        # Calculate relative path from target location to actual source file
        # Use os.path.relpath to support ../ for paths outside target directory
        try:
            relative_path = os.path.relpath(actual_file, ctx.target_location)
            # Normalize to forward slashes for markdown link compatibility
            return relative_path.replace(os.sep, "/")
        except Exception:
            return None

    def _resolve_to_actual_file(self, link_path: str, source_file: Path) -> Path | None:
        """Resolve a link path to the actual file on disk.

        Args:
            link_path: Link path from markdown
            source_file: File containing the link

        Returns:
            Resolved file path or None
        """
        # Get filename from link
        filename = Path(link_path).name

        # Try context registry first
        if filename in self.context_registry:
            return self.context_registry[filename]

        # Try resolving relative to source file
        if source_file.is_file():  # noqa: SIM108
            source_dir = source_file.parent
        else:
            source_dir = source_file

        potential_path = (source_dir / link_path).resolve()
        if potential_path.exists():
            return potential_path

        # Try resolving relative to base_dir
        potential_path = (self.base_dir / link_path).resolve()
        if potential_path.exists():
            return potential_path

        return None

    def _is_external_url(self, path: str) -> bool:
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
            # Strip whitespace to prevent bypass attempts
            path = path.strip()

            # Parse the URL
            parsed = urlparse(path)

            # Only allow http/https schemes
            if parsed.scheme not in ("http", "https"):
                return False

            # Must have a netloc (domain) to be a valid external URL
            # This prevents URLs like "http:relative/path" from being treated as external
            if not parsed.netloc:  # noqa: SIM103
                return False

            return True
        except Exception:
            return False

    def _is_context_file(self, path: str) -> bool:
        """Check if path is a context or memory file.

        Args:
            path: Path to check

        Returns:
            True if context/memory file
        """
        path_lower = path.lower()
        return any(path_lower.endswith(ext) for ext in self.CONTEXT_EXTENSIONS)

    # ------------------------------------------------------------------
    # In-package asset link rewriting (#1147)
    # ------------------------------------------------------------------

    def _is_rewritable_relative_link(self, link_path: str) -> bool:
        """Decide whether a link is a candidate for in-package asset rewrite.

        Filters out everything that obviously is not a relative filesystem
        path inside the package: empty links, fragment-only links, links
        with any URL scheme, root-absolute paths, and protocol-relative
        URLs. The remaining links are *relative paths* that may resolve
        to a sibling file inside the source package.

        Args:
            link_path: Raw link target as it appears in the markdown.

        Returns:
            True if the link should be considered for asset rewriting.
        """
        if not link_path:
            return False
        stripped = link_path.strip()
        if not stripped:
            return False
        if stripped.startswith("#"):
            return False
        if stripped.startswith("//"):
            return False
        if stripped.startswith("/"):
            # Root-absolute paths are consumer-side, not package-relative.
            return False
        # Any URL scheme (http:, mailto:, file:, javascript:, ...): skip.
        try:
            parsed = urlparse(stripped)
        except Exception:
            return False
        return not parsed.scheme

    @staticmethod
    def _split_link_target(link_path: str) -> tuple[str, str]:
        """Split a markdown link target into ``(path, suffix)``.

        Preserves a trailing ``#fragment`` or ``?query`` so the resolver
        can rewrite only the path component and re-append the suffix
        verbatim. Markdown link titles (``"title"`` after a space) are
        intentionally NOT stripped here -- the existing ``LINK_PATTERN``
        treats the whole inside of the parentheses as a single group, so
        a title would be embedded in ``link_path``. Such links are passed
        through unchanged by ``_is_rewritable_relative_link`` indirectly
        (they typically contain a space and resolve to nothing).

        Returns:
            ``(path_part, suffix)`` where ``suffix`` includes its leading
            delimiter (``#`` or ``?``) or is the empty string. When both
            delimiters are present (e.g. ``doc.md?x=1#sec``), the split
            occurs at whichever appears first so the full remainder is
            preserved verbatim.
        """
        candidates = [link_path.find(sep) for sep in ("#", "?")]
        positions = [idx for idx in candidates if idx != -1]
        if not positions:
            return link_path, ""
        idx = min(positions)
        return link_path[:idx], link_path[idx:]

    def _resolve_in_package_asset_link(
        self, link_path: str, ctx: LinkResolutionContext
    ) -> str | None:
        """Rewrite an in-package relative link to its post-install location.

        Resolves ``link_path`` relative to ``ctx.source_file.parent``,
        validates the resolved path lies inside ``ctx.package_root`` via
        :func:`ensure_path_within` (which also normalises symlinks and
        Windows extended prefixes), and returns the relative path from
        ``ctx.target_location`` to the resolved file. Preserves any
        ``#fragment`` or ``?query`` suffix.

        Returns ``None`` if any of the following hold; the caller
        preserves the original link unchanged:

        * ``ctx.package_root`` is not a directory (defensive).
        * The candidate file does not exist or is not a regular file.
        * The candidate escapes ``ctx.package_root`` (symlink traversal,
          ``..`` chains, etc.).
        * Path computation raises (broken filesystem, encoding, ...).
        """
        if ctx.package_root is None:
            return None
        if not ctx.package_root.is_dir():
            return None

        path_part, suffix = self._split_link_target(link_path)
        if not path_part:
            return None

        try:
            source_dir = (
                ctx.source_file.parent if ctx.source_file.is_file() else ctx.source_location
            )
        except OSError:
            return None

        try:
            candidate = (source_dir / path_part).resolve()
        except (OSError, ValueError):
            return None

        if not candidate.exists() or not candidate.is_file():
            return None

        try:
            ensure_path_within(candidate, ctx.package_root)
        except PathTraversalError:
            return None

        # Replay-frame translation (#1182): during audit-replay of a
        # self-package, ``ctx.base_dir`` is the scratch tmpdir but
        # ``ctx.package_root`` (and therefore ``candidate``) still points
        # at the real project tree. Computing ``relpath`` directly would
        # produce a tmpdir-traversal link (e.g. ``../../../../Users/...``)
        # that diverges from what real install writes to disk, causing
        # spurious drift. Detect the cross-frame case (candidate outside
        # base_dir) and re-anchor the target onto package_root so the
        # rewrite mirrors the install-time output.
        relpath_anchor = ctx.target_location
        try:
            candidate_in_base = candidate.is_relative_to(ctx.base_dir)
        except (OSError, ValueError):
            candidate_in_base = True
        if not candidate_in_base:
            try:
                target_rel = ctx.target_location.relative_to(ctx.base_dir)
                relpath_anchor = ctx.package_root / target_rel
            except (OSError, ValueError):
                relpath_anchor = ctx.target_location

        try:
            relative_path = os.path.relpath(candidate, relpath_anchor)
        except (OSError, ValueError):
            return None

        rewritten = relative_path.replace(os.sep, "/")
        return f"{rewritten}{suffix}"


# Legacy functions for backward compatibility
def resolve_markdown_links(content: str, base_path: Path) -> str:
    """Resolve markdown links and inline referenced content.

    Args:
        content (str): Content with markdown links to resolve.
        base_path (Path): Base directory for resolving relative paths.

    Returns:
        str: Content with resolved links and inlined content where appropriate.
    """
    # Pattern to match markdown links: [text](path)
    link_pattern = r"\[([^\]]+)\]\(([^)]+)\)"

    def replace_link(match):
        text = match.group(1)
        path = match.group(2)

        # Skip external URLs
        if path.startswith(("http://", "https://", "ftp://", "mailto:")):
            return match.group(0)  # Return original link

        # Skip anchors
        if path.startswith("#"):
            return match.group(0)  # Return original link

        # Resolve relative path
        full_path = _resolve_path(path, base_path)

        if full_path and full_path.exists() and full_path.is_file():
            # For certain file types, inline the content
            if full_path.suffix.lower() in [".md", ".txt"]:
                try:
                    file_content = full_path.read_text(encoding="utf-8")
                    # Remove frontmatter if present
                    file_content = _remove_frontmatter(file_content)
                    return f"**{text}**:\n\n{file_content}"
                except (OSError, UnicodeDecodeError):
                    # Fall back to original link if file can't be read
                    return match.group(0)
            else:
                # For other file types, keep the link but update path if needed
                return match.group(0)
        else:
            # File doesn't exist, keep original link (will be caught by validation)
            return match.group(0)

    return re.sub(link_pattern, replace_link, content)


def validate_link_targets(content: str, base_path: Path) -> builtins.list[str]:
    """Validate that all referenced files exist.

    Args:
        content (str): Content to validate links in.
        base_path (Path): Base directory for resolving relative paths.

    Returns:
        List[str]: List of error messages for missing or invalid links.
    """
    errors = []

    # Check markdown links
    link_pattern = r"\[([^\]]+)\]\(([^)]+)\)"
    for match in re.finditer(link_pattern, content):
        text = match.group(1)
        path = match.group(2)

        # Skip external URLs and anchors
        if path.startswith(("http://", "https://", "ftp://", "mailto:")) or path.startswith("#"):
            continue

        # Resolve and check path
        full_path = _resolve_path(path, base_path)
        if not full_path or not full_path.exists():
            errors.append(f"Referenced file not found: {path} (in link '{text}')")
        elif not full_path.is_file() and not full_path.is_dir():
            errors.append(
                f"Referenced path is neither a file nor directory: {path} (in link '{text}')"
            )

    return errors


def _resolve_path(path: str, base_path: Path) -> Path | None:
    """Resolve a relative path against a base path.

    Args:
        path (str): Relative path to resolve.
        base_path (Path): Base directory for resolution.

    Returns:
        Optional[Path]: Resolved path or None if invalid.
    """
    if not path or not path.strip():
        return None
    # NUL bytes survive ``Path()`` construction on POSIX but every downstream
    # filesystem call (``.exists()``, ``.is_file()``, ``.read_text()``) raises
    # ``ValueError``. Callers in this module do not catch ``ValueError`` so an
    # unguarded NUL would abort markdown link resolution / validation. Reject
    # at the resolver boundary instead.
    if "\x00" in path:
        return None
    try:
        if Path(path).is_absolute():
            return Path(path)
        else:
            return base_path / path
    except (OSError, ValueError):
        return None


def _remove_frontmatter(content: str) -> str:
    """Remove YAML frontmatter from content.

    Args:
        content (str): Content that may contain frontmatter.

    Returns:
        str: Content without frontmatter.
    """
    # Remove YAML frontmatter (--- at start, --- at end)
    if content.startswith("---\n"):
        lines = content.split("\n")
        in_frontmatter = True
        content_lines = []

        for i, line in enumerate(lines[1:], 1):  # Skip first ---  # noqa: B007
            if line.strip() == "---" and in_frontmatter:
                in_frontmatter = False
                continue
            if not in_frontmatter:
                content_lines.append(line)

        content = "\n".join(content_lines)

    return content.strip()


def _detect_circular_references(
    content: str, base_path: Path, visited: set | None = None
) -> builtins.list[str]:
    """Detect circular references in markdown links.

    Args:
        content (str): Content to check for circular references.
        base_path (Path): Base directory for resolving paths.
        visited (Optional[set]): Set of already visited files.

    Returns:
        List[str]: List of circular reference errors.
    """
    if visited is None:
        visited = set()

    errors = []
    current_file = base_path

    if current_file in visited:
        errors.append(f"Circular reference detected: {current_file}")
        return errors

    visited.add(current_file)

    # Check markdown links for potential circular references
    link_pattern = r"\[([^\]]+)\]\(([^)]+)\)"
    for match in re.finditer(link_pattern, content):
        path = match.group(2)

        # Skip external URLs and anchors
        if path.startswith(("http://", "https://", "ftp://", "mailto:")) or path.startswith("#"):
            continue

        full_path = _resolve_path(path, base_path.parent if base_path.is_file() else base_path)
        if full_path and full_path.exists() and full_path.is_file():
            if full_path.suffix.lower() in [".md", ".txt"]:
                try:
                    linked_content = full_path.read_text(encoding="utf-8")
                    errors.extend(
                        _detect_circular_references(linked_content, full_path, visited.copy())
                    )
                except (OSError, UnicodeDecodeError):
                    continue

    return errors
