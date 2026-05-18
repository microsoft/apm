"""Context link resolution for APM primitives.

Resolves markdown links to context files across the APM lifecycle:
- Installation: Rewrite links when copying from dependencies
- Compilation: Rewrite links when generating AGENTS.md
- Runtime: Resolve links when executing prompts

Following KISS principle - simple, pragmatic implementation.

Module layout
-------------
``link_resolver`` (this file)
    Public API: :class:`LinkResolutionContext`, :class:`UnifiedLinkResolver`,
    and backward-compatibility re-exports of legacy helpers.

``_link_asset_rewrite``
    In-package asset link rewriting helpers (feature #1147), extracted to
    keep this file under 500 lines. Used only by :class:`UnifiedLinkResolver`.

``_link_legacy``
    Legacy module-level helper functions kept for backward compatibility.
    Re-exported from this module so existing imports remain unbroken.
"""

import builtins
import os
import re
from dataclasses import dataclass
from pathlib import Path

from apm_cli.compilation import _link_asset_rewrite, _link_context_utils
from apm_cli.compilation._link_legacy import (
    _detect_circular_references,
    _remove_frontmatter,
    _resolve_path,
    resolve_markdown_links,
    validate_link_targets,
)

# CRITICAL: Shadow Click commands to prevent namespace collision
set = builtins.set
list = builtins.list
dict = builtins.dict

__all__ = [
    # Core public API
    "LinkResolutionContext",
    "UnifiedLinkResolver",
    # Legacy re-exports (backward compatibility)
    "_detect_circular_references",
    "_remove_frontmatter",
    "_resolve_path",
    "resolve_markdown_links",
    "validate_link_targets",
]


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
        """Delegate to :func:`_link_context_utils.is_external_url`."""
        return _link_context_utils.is_external_url(path)

    def _is_context_file(self, path: str) -> bool:
        """Delegate to :func:`_link_context_utils.is_context_file`."""
        return _link_context_utils.is_context_file(path, self.CONTEXT_EXTENSIONS)

    # ------------------------------------------------------------------
    # In-package asset link rewriting (#1147) — delegated to _link_asset_rewrite
    # ------------------------------------------------------------------

    def _is_rewritable_relative_link(self, link_path: str) -> bool:
        """Delegate to :func:`_link_asset_rewrite.is_rewritable_relative_link`."""
        return _link_asset_rewrite.is_rewritable_relative_link(link_path)

    @staticmethod
    def _split_link_target(link_path: str) -> tuple[str, str]:
        """Delegate to :func:`_link_asset_rewrite.split_link_target`."""
        return _link_asset_rewrite.split_link_target(link_path)

    def _resolve_in_package_asset_link(
        self, link_path: str, ctx: LinkResolutionContext
    ) -> str | None:
        """Delegate to :func:`_link_asset_rewrite.resolve_in_package_asset_link`."""
        return _link_asset_rewrite.resolve_in_package_asset_link(link_path, ctx)
