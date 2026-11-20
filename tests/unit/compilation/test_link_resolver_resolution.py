"""Unit tests for pure link classification helpers in apm_cli.compilation.link_resolver.

Covers:
- _is_external_url (various schemes, no netloc, malformed)
- _is_context_file (positive, negative)
- _is_rewritable_relative_link (empty, fragment, //, /, scheme, relative)
- _split_link_target (no delimiter, fragment, query, both)
- _remove_frontmatter
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.compilation.link_resolver import (
    UnifiedLinkResolver,
    _remove_frontmatter,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def resolver() -> UnifiedLinkResolver:
    return UnifiedLinkResolver(Path("/project"))


# ---------------------------------------------------------------------------
# register_contexts
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _is_external_url
# ---------------------------------------------------------------------------


class TestIsExternalUrl:
    def test_http_with_netloc_is_external(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_external_url("http://example.com/path") is True

    def test_https_with_netloc_is_external(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_external_url("https://github.com/org/repo") is True

    def test_ftp_is_not_external(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_external_url("ftp://files.example.com") is False

    def test_no_scheme_is_not_external(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_external_url("relative/path.md") is False

    def test_http_without_netloc_is_not_external(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_external_url("http:relative") is False

    def test_data_scheme_is_not_external(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_external_url("data:text/html,<h1>hi</h1>") is False

    def test_javascript_scheme_is_not_external(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_external_url("javascript:alert(1)") is False

    def test_empty_string_is_not_external(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_external_url("") is False


# ---------------------------------------------------------------------------
# _is_context_file
# ---------------------------------------------------------------------------


class TestIsContextFile:
    def test_context_extension_returns_true(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_context_file("api-standards.context.md") is True

    def test_memory_extension_returns_true(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_context_file("notes.memory.md") is True

    def test_plain_md_returns_false(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_context_file("README.md") is False

    def test_uppercase_extension_returns_true(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_context_file("API.CONTEXT.MD") is True


# ---------------------------------------------------------------------------
# _is_rewritable_relative_link
# ---------------------------------------------------------------------------


class TestIsRewritableRelativeLink:
    def test_empty_returns_false(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_rewritable_relative_link("") is False

    def test_whitespace_only_returns_false(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_rewritable_relative_link("   ") is False

    def test_fragment_only_returns_false(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_rewritable_relative_link("#section") is False

    def test_protocol_relative_returns_false(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_rewritable_relative_link("//cdn.example.com/lib.js") is False

    def test_root_absolute_returns_false(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_rewritable_relative_link("/absolute/path.md") is False

    def test_http_scheme_returns_false(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_rewritable_relative_link("http://example.com") is False

    def test_relative_path_returns_true(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_rewritable_relative_link("images/logo.png") is True

    def test_dotdot_relative_returns_true(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_rewritable_relative_link("../sibling/file.md") is True

    def test_current_dir_relative_returns_true(self, resolver: UnifiedLinkResolver) -> None:
        assert resolver._is_rewritable_relative_link("./local.md") is True


# ---------------------------------------------------------------------------
# _split_link_target
# ---------------------------------------------------------------------------


class TestSplitLinkTarget:
    def test_no_delimiter_returns_full_path_and_empty(self, resolver: UnifiedLinkResolver) -> None:
        path, suffix = resolver._split_link_target("images/logo.png")
        assert path == "images/logo.png"
        assert suffix == ""

    def test_fragment_delimiter_splits_correctly(self, resolver: UnifiedLinkResolver) -> None:
        path, suffix = resolver._split_link_target("doc.md#section")
        assert path == "doc.md"
        assert suffix == "#section"

    def test_query_delimiter_splits_correctly(self, resolver: UnifiedLinkResolver) -> None:
        path, suffix = resolver._split_link_target("file.md?v=2")
        assert path == "file.md"
        assert suffix == "?v=2"

    def test_both_delimiters_splits_at_first(self, resolver: UnifiedLinkResolver) -> None:
        path, suffix = resolver._split_link_target("file.md?x=1#sec")
        assert path == "file.md"
        assert suffix == "?x=1#sec"

    def test_fragment_before_query(self, resolver: UnifiedLinkResolver) -> None:
        path, suffix = resolver._split_link_target("file.md#sec?x=1")
        assert path == "file.md"
        assert suffix == "#sec?x=1"


# ---------------------------------------------------------------------------
# _resolve_in_package_asset_link
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# resolve_links_for_installation (with asset-rewrite enabled)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# resolve_links_for_compilation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_referenced_contexts
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Legacy functions
# ---------------------------------------------------------------------------


class TestRemoveFrontmatter:
    def test_no_frontmatter_returns_stripped_content(self) -> None:
        assert _remove_frontmatter("# Heading\n\nContent") == "# Heading\n\nContent"

    def test_frontmatter_is_stripped(self) -> None:
        content = "---\ntitle: Test\n---\n# Heading\n\nBody"
        result = _remove_frontmatter(content)
        assert "title" not in result
        assert "# Heading" in result

    def test_empty_frontmatter_is_stripped(self) -> None:
        content = "---\n---\n# Heading"
        result = _remove_frontmatter(content)
        assert result == "# Heading"

    def test_content_without_leading_dashes_unchanged(self) -> None:
        content = "# No frontmatter\n\nBody text"
        assert _remove_frontmatter(content) == content
