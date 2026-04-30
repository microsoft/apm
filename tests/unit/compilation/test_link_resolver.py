"""Tests for context link resolution in APM primitives.

Following TDD approach - tests written before implementation.
"""

import re
from pathlib import Path
from textwrap import dedent
from urllib.parse import urlparse

import pytest

from apm_cli.compilation.link_resolver import (
    LinkResolutionContext,
    UnifiedLinkResolver,
    _resolve_path,
)
from apm_cli.primitives.models import Context, PrimitiveCollection


@pytest.fixture
def base_dir(tmp_path):
    """Create a temporary base directory for testing."""
    return tmp_path


@pytest.fixture
def resolver(base_dir):
    """Create a UnifiedLinkResolver for testing."""
    return UnifiedLinkResolver(base_dir)


@pytest.fixture
def sample_primitives(base_dir):
    """Create sample primitives for testing."""
    collection = PrimitiveCollection()

    # Create some context files
    context_dir = base_dir / ".apm" / "context"
    context_dir.mkdir(parents=True, exist_ok=True)

    # Local context file
    api_context = context_dir / "api-standards.context.md"
    api_context.write_text("# API Standards\n\nOur API guidelines...", encoding="utf-8")
    collection.add_primitive(
        Context(
            name="api-standards",
            file_path=api_context,
            content="# API Standards\n\nOur API guidelines...",
            source="local",
        )
    )

    # Another local context
    security_context = context_dir / "security.context.md"
    security_context.write_text("# Security\n\nSecurity guidelines...", encoding="utf-8")
    collection.add_primitive(
        Context(
            name="security",
            file_path=security_context,
            content="# Security\n\nSecurity guidelines...",
            source="local",
        )
    )

    # Dependency context
    dep_dir = base_dir / "apm_modules" / "company" / "standards" / ".apm" / "context"
    dep_dir.mkdir(parents=True, exist_ok=True)
    dep_api_context = dep_dir / "api.context.md"
    dep_api_context.write_text("# Company API Standards", encoding="utf-8")
    collection.add_primitive(
        Context(
            name="api",
            file_path=dep_api_context,
            content="# Company API Standards",
            source="dependency:company/standards",
        )
    )

    return collection


class TestContextRegistry:
    """Tests for context file registration."""

    def test_register_local_contexts(self, resolver, sample_primitives):
        """Local context files are registered by filename."""
        resolver.register_contexts(sample_primitives)

        # Should be able to find by filename
        assert "api-standards.context.md" in resolver.context_registry
        assert "security.context.md" in resolver.context_registry

    def test_register_dependency_contexts(self, resolver, sample_primitives):
        """Dependency contexts are registered with qualified names."""
        resolver.register_contexts(sample_primitives)

        # Should be registered with qualified name
        assert "company/standards:api.context.md" in resolver.context_registry
        # Also by simple filename for convenience
        assert "api.context.md" in resolver.context_registry

    def test_context_paths_are_correct(self, resolver, sample_primitives, base_dir):
        """Registered paths point to actual file locations."""
        resolver.register_contexts(sample_primitives)

        api_path = resolver.context_registry["api-standards.context.md"]
        assert api_path.exists()
        assert api_path.name == "api-standards.context.md"


class TestLinkRewriting:
    """Tests for markdown link rewriting logic."""

    def test_preserve_external_urls(self, resolver):
        """HTTP/HTTPS URLs should not be modified."""
        content = dedent("""
            # Documentation
            
            See [external docs](https://example.com/docs)
            and [another site](http://example.org)
        """)

        ctx = LinkResolutionContext(
            source_file=Path("/project/.apm/instructions/test.instructions.md"),
            source_location=Path("/project/.apm/instructions"),
            target_location=Path("/project"),
            base_dir=Path("/project"),
            available_contexts={},
        )

        result = resolver._rewrite_markdown_links(content, ctx)

        # Extract markdown link destinations using regex and check presence of the expected URLs
        link_urls = re.findall(r"\[[^\]]+\]\(([^)]+)\)", result)
        # Validate URLs using urlparse
        assert any(
            urlparse(url).scheme in ("http", "https") and urlparse(url).netloc == "example.com"
            for url in link_urls
        )
        assert any(
            urlparse(url).scheme in ("http", "https") and urlparse(url).netloc == "example.org"
            for url in link_urls
        )

    def test_reject_non_http_schemes(self, resolver):
        """Non-HTTP schemes should NOT be treated as external URLs."""
        # These should be treated as internal paths (potentially rewritten or preserved)
        test_cases = [
            ("javascript:alert('xss')", "javascript scheme"),
            ("data:text/html,<script>alert('xss')</script>", "data scheme"),
            ("file:///etc/passwd", "file scheme"),
            ("ftp://example.com/file", "ftp scheme"),
            ("mailto:user@example.com", "mailto scheme"),
        ]

        for url, description in test_cases:
            content = f"See [link]({url})"

            ctx = LinkResolutionContext(
                source_file=Path("/project/.apm/instructions/test.instructions.md"),
                source_location=Path("/project/.apm/instructions"),
                target_location=Path("/project"),
                base_dir=Path("/project"),
                available_contexts={},
            )

            result = resolver._rewrite_markdown_links(content, ctx)  # noqa: F841

            # These URLs should NOT be preserved as-is since they're not external
            # They may be rewritten or preserved depending on whether they match patterns
            # The key is that _is_external_url returns False for these
            assert not resolver._is_external_url(url), f"{description} should not be external"

    def test_reject_malformed_http_urls(self, resolver):
        """Malformed HTTP URLs without netloc should not be treated as external."""
        test_cases = [
            "http:relative/path",
            "https:/no-double-slash",
            "http://",  # No netloc
            "https://",  # No netloc
        ]

        for url in test_cases:
            assert not resolver._is_external_url(url), f"{url} should not be external"

    def test_handle_urls_with_whitespace(self, resolver):
        """URLs with surrounding whitespace should be handled correctly."""
        # Valid URL with whitespace should still be recognized
        assert resolver._is_external_url(" https://example.com ")
        assert resolver._is_external_url("\thttps://example.com\t")

        # Invalid URL with whitespace should not be recognized
        assert not resolver._is_external_url(" javascript:alert('xss') ")

    def test_preserve_non_context_links(self, resolver):
        """Links to non-context .md files should not be modified."""
        content = dedent("""
            # Documentation
            
            See [README](./README.md) for more info.
        """)

        ctx = LinkResolutionContext(
            source_file=Path("/project/.apm/instructions/test.instructions.md"),
            source_location=Path("/project/.apm/instructions"),
            target_location=Path("/project"),
            base_dir=Path("/project"),
            available_contexts={},
        )

        result = resolver._rewrite_markdown_links(content, ctx)

        assert "./README.md" in result

    def test_rewrite_relative_context_link_same_directory(self, resolver, base_dir):
        """Links to context files in same directory are rewritten."""
        # Setup context registry
        context_path = base_dir / ".apm" / "context" / "api.context.md"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text("# API", encoding="utf-8")

        resolver.context_registry["api.context.md"] = context_path

        content = dedent("""
            # Backend Instructions
            
            Follow [API standards](./api.context.md)
        """)

        source_file = base_dir / ".apm" / "instructions" / "backend.instructions.md"
        source_file.parent.mkdir(parents=True, exist_ok=True)

        ctx = LinkResolutionContext(
            source_file=source_file,
            source_location=source_file.parent,
            target_location=base_dir / "backend" / "AGENTS.md",
            base_dir=base_dir,
            available_contexts=resolver.context_registry,
        )

        result = resolver._rewrite_markdown_links(content, ctx)

        # Should be rewritten to point to actual source location from backend/ to .apm/context/
        # Relative path from backend/AGENTS.md to .apm/context/api.context.md
        # The relative_to() method produces .apm/context/api.context.md (without ../)
        assert ".apm/context/api.context.md" in result

    def test_rewrite_relative_context_link_parent_directory(self, resolver, base_dir):
        """Links using ../ to access parent directory context are rewritten."""
        # Setup context registry
        context_path = base_dir / ".apm" / "context" / "api.context.md"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text("# API", encoding="utf-8")

        resolver.context_registry["api.context.md"] = context_path

        content = dedent("""
            # Backend Instructions
            
            Follow [API standards](../context/api.context.md)
        """)

        source_file = base_dir / ".apm" / "instructions" / "backend.instructions.md"
        source_file.parent.mkdir(parents=True, exist_ok=True)

        ctx = LinkResolutionContext(
            source_file=source_file,
            source_location=source_file.parent,
            target_location=base_dir / "AGENTS.md",
            base_dir=base_dir,
            available_contexts=resolver.context_registry,
        )

        result = resolver._rewrite_markdown_links(content, ctx)

        # Should be rewritten to point to actual source location
        assert ".apm/context/api.context.md" in result


class TestInstallationLinkResolution:
    """Tests for link resolution during installation (apm install)."""

    def test_resolve_links_when_copying_from_dependency(self, resolver, base_dir):
        """Links in files copied from dependencies are resolved correctly."""
        # Setup: dependency has an agent that links to a context
        dep_dir = base_dir / "apm_modules" / "company" / "standards" / ".apm"
        agent_file = dep_dir / "agents" / "backend-expert.agent.md"
        agent_file.parent.mkdir(parents=True, exist_ok=True)

        context_file = dep_dir / "context" / "api.context.md"
        context_file.parent.mkdir(parents=True, exist_ok=True)
        context_file.write_text("# API Standards", encoding="utf-8")

        # Register the context
        resolver.context_registry["company/standards:api.context.md"] = context_file
        resolver.context_registry["api.context.md"] = context_file

        # Agent content with relative link
        agent_content = dedent("""
            ---
            description: Backend expert
            ---
            
            # Backend Expert
            
            Follow [API standards](../context/api.context.md)
        """)

        # Resolve links for installation
        target_file = base_dir / ".github" / "agents" / "backend-expert.agent.md"

        result = resolver.resolve_links_for_installation(
            content=agent_content, source_file=agent_file, target_file=target_file
        )

        # Should point to apm_modules (direct link to dependency)
        assert "apm_modules/company/standards/.apm/context/api.context.md" in result


class TestCompilationLinkResolution:
    """Tests for link resolution during compilation (apm compile)."""

    def test_resolve_links_in_generated_agents_md(self, resolver, base_dir):
        """Links in compiled AGENTS.md point directly to .apm/context/."""
        # Setup context
        context_path = base_dir / ".apm" / "context" / "api.context.md"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text("# API", encoding="utf-8")

        resolver.context_registry["api.context.md"] = context_path

        # Content with context link
        content = dedent("""
            # Instructions
            
            Follow [API standards](../context/api.context.md)
        """)

        source_file = base_dir / ".apm" / "instructions" / "backend.instructions.md"
        compiled_output = base_dir / "backend" / "AGENTS.md"

        result = resolver.resolve_links_for_compilation(
            content=content, source_file=source_file, compiled_output=compiled_output
        )

        # Should point directly to source location
        # From backend/AGENTS.md to .apm/context/api.context.md
        assert ".apm/context/api.context.md" in result


class TestContextValidation:
    """Tests for validating referenced contexts (no copying needed)."""

    def test_get_referenced_contexts(self, resolver, base_dir, sample_primitives):
        """Only context files that are actually referenced should be identified."""
        # Register all contexts
        resolver.register_contexts(sample_primitives)

        # Create an instruction that references one context
        instruction_file = base_dir / ".apm" / "instructions" / "backend.instructions.md"
        instruction_file.parent.mkdir(parents=True, exist_ok=True)
        instruction_file.write_text(
            dedent("""
            ---
            applyTo: "backend/**/*.py"
            description: Backend guidelines
            ---
            
            Follow [API standards](../context/api-standards.context.md)
        """),
            encoding="utf-8",
        )

        # Get referenced contexts (no copying)
        referenced = resolver.get_referenced_contexts(all_files_to_scan=[instruction_file])

        # Only the referenced context should be identified
        assert len(referenced) == 1
        assert any("api-standards.context.md" in str(path) for path in referenced)

    def test_multiple_references(self, resolver, base_dir, sample_primitives):
        """Multiple files referencing contexts are all identified."""
        # Register all contexts
        resolver.register_contexts(sample_primitives)

        # Create two instructions referencing different contexts
        inst1 = base_dir / ".apm" / "instructions" / "backend.instructions.md"
        inst1.parent.mkdir(parents=True, exist_ok=True)
        inst1.write_text("Follow [API](../context/api-standards.context.md)", encoding="utf-8")

        inst2 = base_dir / ".apm" / "instructions" / "security.instructions.md"
        inst2.write_text("Follow [Security](../context/security.context.md)", encoding="utf-8")

        # Get referenced contexts
        referenced = resolver.get_referenced_contexts(all_files_to_scan=[inst1, inst2])

        # Should find both contexts
        assert len(referenced) == 2
        context_names = [p.name for p in referenced]
        assert "api-standards.context.md" in context_names
        assert "security.context.md" in context_names


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_missing_context_file(self, resolver, base_dir):
        """Links to non-existent contexts are preserved with warning."""
        content = dedent("""
            Follow [missing context](../context/missing.context.md)
        """)

        source_file = base_dir / ".apm" / "instructions" / "test.instructions.md"

        result = resolver.resolve_links_for_compilation(
            content=content, source_file=source_file, compiled_output=base_dir / "AGENTS.md"
        )

        # Original link should be preserved (will be broken but documented)
        assert "../context/missing.context.md" in result

    def test_empty_context_registry(self, resolver):
        """Resolver handles empty context registry gracefully."""
        content = dedent("""
            Follow [some context](../context/api.context.md)
        """)

        ctx = LinkResolutionContext(
            source_file=Path("/project/.apm/instructions/test.instructions.md"),
            source_location=Path("/project/.apm/instructions"),
            target_location=Path("/project"),
            base_dir=Path("/project"),
            available_contexts={},
        )

        # Should not crash, just preserve original links
        result = resolver._rewrite_markdown_links(content, ctx)
        assert isinstance(result, str)

    def test_memory_context_files(self, resolver, base_dir):
        """Memory files (.memory.md) are handled like context files."""
        # Setup memory file
        memory_path = base_dir / ".apm" / "context" / "project.memory.md"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text("# Project Memory", encoding="utf-8")

        resolver.context_registry["project.memory.md"] = memory_path

        content = dedent("""
            See [project memory](../context/project.memory.md)
        """)

        source_file = base_dir / ".apm" / "instructions" / "test.instructions.md"

        result = resolver.resolve_links_for_compilation(
            content=content, source_file=source_file, compiled_output=base_dir / "AGENTS.md"
        )

        # Should be rewritten to actual source location
        assert ".apm/context/project.memory.md" in result


class TestResolvePathInputGuards:
    """Containment tests for _resolve_path: empty / whitespace / NUL / traversal."""

    def test_empty_string_returns_none(self, base_dir):
        """Empty link should resolve to None, not the base directory."""
        assert _resolve_path("", base_dir) is None

    def test_whitespace_only_returns_none(self, base_dir):
        """Whitespace-only link should resolve to None."""
        assert _resolve_path("   ", base_dir) is None
        assert _resolve_path("\t", base_dir) is None
        assert _resolve_path("\n", base_dir) is None

    def test_embedded_nul_byte_returns_none(self, base_dir):
        """An embedded NUL byte must produce ``None``, not a ``Path``.

        NUL bytes survive ``Path()`` construction on POSIX, but every
        downstream filesystem call (``.exists()``, ``.is_file()``,
        ``.read_text()``) raises ``ValueError``. Callers in
        ``link_resolver`` (``resolve_markdown_links`` /
        ``validate_link_targets``) do not catch ``ValueError``, so
        returning a ``Path`` here would abort markdown link
        resolution. The resolver rejects NUL at its boundary instead.
        """
        assert _resolve_path("foo\x00bar", base_dir) is None
        assert _resolve_path("\x00", base_dir) is None
        assert _resolve_path("a/b\x00c.md", base_dir) is None

    def test_posix_backslash_traversal_stays_relative(self, base_dir):
        """Backslashes are literal characters on POSIX, so the path stays under base_dir."""
        result = _resolve_path("foo\\..\\..\\etc\\passwd", base_dir)
        assert result is not None
        # The literal backslash filename is interpreted as a single segment under base_dir.
        assert result == base_dir / "foo\\..\\..\\etc\\passwd"

    def test_file_uri_on_posix_is_treated_as_relative(self, base_dir):
        """`file://...` is not absolute on POSIX, so it joins under base_dir rather than escaping it."""
        result = _resolve_path("file:///etc/passwd", base_dir)
        assert result is not None
        assert str(result).startswith(str(base_dir))

    def test_nonexistent_relative_target_resolves_normally(self, base_dir):
        """The happy path: a syntactically-valid relative target resolves even if the target file is missing."""
        result = _resolve_path("does/not/exist.md", base_dir)
        assert result == base_dir / "does/not/exist.md"
