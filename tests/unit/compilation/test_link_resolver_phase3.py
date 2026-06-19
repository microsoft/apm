"""Phase-3 unit tests for apm_cli.compilation.link_resolver.

Covers error/edge branches not exercised by the main test_link_resolver.py:
- resolve_links_for_installation with asset-rewrite enabled (package_root set)
- resolve_links_for_compilation compiled_output variants (None, file, dir)
- get_referenced_contexts (missing file, read exception)
- _rewrite_markdown_links (external URL passthrough, context passthrough,
  asset rewrite enabled/disabled, no-match passthrough)
- _is_external_url (various schemes, no netloc, malformed)
- _is_context_file (positive, negative)
- _is_rewritable_relative_link (empty, fragment, //, /, scheme, relative)
- _split_link_target (no delimiter, fragment, query, both)
- _resolve_in_package_asset_link (no package_root, non-dir package_root,
  candidate does not exist, path traversal, successful rewrite,
  cross-frame relpath anchor, fragment preserved)
- Legacy functions: resolve_markdown_links, validate_link_targets,
  _resolve_path, _remove_frontmatter
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from apm_cli.compilation.link_resolver import (
    UnifiedLinkResolver,
    _remove_frontmatter,
    _resolve_path,
    resolve_markdown_links,
    validate_link_targets,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def base_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def resolver(base_dir: Path) -> UnifiedLinkResolver:
    return UnifiedLinkResolver(base_dir)


# ---------------------------------------------------------------------------
# register_contexts
# ---------------------------------------------------------------------------


class TestRegisterContexts:
    def test_local_context_registered_by_filename(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        ctx_file = base_dir / "my.context.md"
        ctx_file.write_text("# Ctx")

        ctx_obj = MagicMock()
        ctx_obj.file_path = ctx_file
        ctx_obj.source = "local"

        primitives = MagicMock()
        primitives.contexts = [ctx_obj]
        resolver.register_contexts(primitives)

        assert "my.context.md" in resolver.context_registry

    def test_dependency_context_registered_with_qualified_name(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        ctx_file = base_dir / "dep.context.md"
        ctx_file.write_text("# Dep")

        ctx_obj = MagicMock()
        ctx_obj.file_path = ctx_file
        ctx_obj.source = "dependency:org/repo"

        primitives = MagicMock()
        primitives.contexts = [ctx_obj]
        resolver.register_contexts(primitives)

        assert "org/repo:dep.context.md" in resolver.context_registry


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


class TestResolveInPackageAssetLink:
    def _make_ctx(
        self,
        base_dir: Path,
        source_file: Path,
        package_root: Path | None,
        target_location: Path | None = None,
    ):
        from apm_cli.compilation.link_resolver import LinkResolutionContext

        return LinkResolutionContext(
            source_file=source_file,
            source_location=source_file.parent,
            target_location=target_location or base_dir / ".github",
            base_dir=base_dir,
            available_contexts={},
            package_root=package_root,
            enable_asset_rewrite=package_root is not None,
        )

    def test_no_package_root_returns_none(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        source_file = base_dir / "file.md"
        source_file.write_text("content")
        ctx = self._make_ctx(base_dir, source_file, package_root=None)
        assert resolver._resolve_in_package_asset_link("image.png", ctx) is None

    def test_package_root_not_a_dir_returns_none(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        source_file = base_dir / "file.md"
        source_file.write_text("content")
        fake_root = base_dir / "notadir.txt"
        fake_root.write_text("x")
        ctx = self._make_ctx(base_dir, source_file, package_root=fake_root)
        assert resolver._resolve_in_package_asset_link("image.png", ctx) is None

    def test_empty_path_part_returns_none(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        pkg_root = base_dir / "pkg"
        pkg_root.mkdir()
        source_file = pkg_root / "file.md"
        source_file.write_text("content")
        ctx = self._make_ctx(base_dir, source_file, package_root=pkg_root)
        # link_path with only a fragment -> path_part is empty
        assert resolver._resolve_in_package_asset_link("#section", ctx) is None

    def test_nonexistent_candidate_returns_none(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        pkg_root = base_dir / "pkg"
        pkg_root.mkdir()
        source_file = pkg_root / "file.md"
        source_file.write_text("content")
        ctx = self._make_ctx(base_dir, source_file, package_root=pkg_root)
        assert resolver._resolve_in_package_asset_link("nonexistent.png", ctx) is None

    def test_successful_rewrite_returns_relative_path(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        pkg_root = base_dir / "apm_modules" / "org" / "pkg"
        pkg_root.mkdir(parents=True)
        asset = pkg_root / "images" / "logo.png"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"\x89PNG")

        source_file = pkg_root / "prompts" / "prompt.prompt.md"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("[Logo](../images/logo.png)")

        target_dir = base_dir / ".github" / "prompts"
        target_dir.mkdir(parents=True)

        ctx = self._make_ctx(
            base_dir,
            source_file,
            package_root=pkg_root,
            target_location=target_dir,
        )
        result = resolver._resolve_in_package_asset_link("../images/logo.png", ctx)
        assert result is not None
        # Should point somewhere; verify the fragment suffix isn't added when absent
        assert "#" not in result

    def test_fragment_preserved_in_rewritten_link(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        pkg_root = base_dir / "apm_modules" / "org" / "pkg"
        pkg_root.mkdir(parents=True)
        target_file = pkg_root / "docs" / "api.md"
        target_file.parent.mkdir(parents=True)
        target_file.write_text("# API")

        source_file = pkg_root / "prompt.prompt.md"
        source_file.write_text("[API](docs/api.md#auth)")

        target_dir = base_dir / ".github"
        target_dir.mkdir(parents=True)

        ctx = self._make_ctx(
            base_dir,
            source_file,
            package_root=pkg_root,
            target_location=target_dir,
        )
        result = resolver._resolve_in_package_asset_link("docs/api.md#auth", ctx)
        assert result is not None
        assert result.endswith("#auth")

    def test_path_traversal_outside_package_root_returns_none(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        pkg_root = base_dir / "pkg"
        pkg_root.mkdir()
        # Place source file inside the package
        source_file = pkg_root / "file.md"
        source_file.write_text("content")
        # Create a real file outside the package root
        outside = base_dir / "secret.txt"
        outside.write_text("secret")

        ctx = self._make_ctx(base_dir, source_file, package_root=pkg_root)
        # The link tries to escape via ../
        result = resolver._resolve_in_package_asset_link("../secret.txt", ctx)
        assert result is None


# ---------------------------------------------------------------------------
# resolve_links_for_installation (with asset-rewrite enabled)
# ---------------------------------------------------------------------------


class TestResolveLinksForInstallationAssetRewrite:
    def test_external_url_preserved_unchanged(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        pkg_root = base_dir / "pkg"
        pkg_root.mkdir()
        resolver.package_root = pkg_root

        source_file = pkg_root / "p.prompt.md"
        source_file.write_text("[ext](https://example.com/img.png)")
        target_file = base_dir / ".github" / "p.prompt.md"
        target_file.parent.mkdir(parents=True)

        result = resolver.resolve_links_for_installation(
            "[ext](https://example.com/img.png)", source_file, target_file
        )
        assert "https://example.com/img.png" in result

    def test_relative_link_not_in_package_preserved(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        pkg_root = base_dir / "pkg"
        pkg_root.mkdir()
        resolver.package_root = pkg_root

        source_file = pkg_root / "p.prompt.md"
        source_file.write_text("[missing](nonexistent.png)")
        target_file = base_dir / ".github" / "p.prompt.md"
        target_file.parent.mkdir(parents=True)

        result = resolver.resolve_links_for_installation(
            "[missing](nonexistent.png)", source_file, target_file
        )
        # Not rewritten because the file doesn't exist
        assert "nonexistent.png" in result


# ---------------------------------------------------------------------------
# resolve_links_for_compilation
# ---------------------------------------------------------------------------


class TestResolveLinksForCompilation:
    def test_none_compiled_output_uses_source_parent(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        source_file = base_dir / "src" / "file.md"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("content")
        # Should not raise even with None compiled_output
        result = resolver.resolve_links_for_compilation("no links here", source_file, None)
        assert result == "no links here"

    def test_compiled_output_as_file_path_uses_parent(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        source_file = base_dir / "file.md"
        source_file.write_text("content")
        compiled = base_dir / "AGENTS.md"
        result = resolver.resolve_links_for_compilation("text", source_file, compiled)
        assert result == "text"

    def test_compiled_output_as_directory_uses_directory(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        source_file = base_dir / "file.md"
        source_file.write_text("content")
        out_dir = base_dir / "out"
        out_dir.mkdir()
        result = resolver.resolve_links_for_compilation("text", source_file, out_dir)
        assert result == "text"


# ---------------------------------------------------------------------------
# get_referenced_contexts
# ---------------------------------------------------------------------------


class TestGetReferencedContexts:
    def test_nonexistent_file_is_skipped(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        result = resolver.get_referenced_contexts([base_dir / "nonexistent.md"])
        assert result == set()

    def test_unreadable_file_is_skipped_silently(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        f = base_dir / "file.md"
        f.write_text("content")
        with patch.object(Path, "read_text", side_effect=OSError("no access")):
            result = resolver.get_referenced_contexts([f])
        assert result == set()

    def test_file_with_no_context_links_returns_empty(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        f = base_dir / "file.md"
        f.write_text("# Just text, no context links")
        result = resolver.get_referenced_contexts([f])
        assert result == set()

    def test_registered_context_link_is_collected(
        self, resolver: UnifiedLinkResolver, base_dir: Path
    ) -> None:
        ctx_file = base_dir / ".apm" / "context" / "api.context.md"
        ctx_file.parent.mkdir(parents=True)
        ctx_file.write_text("# API")
        resolver.context_registry["api.context.md"] = ctx_file

        source = base_dir / "prompt.md"
        source.write_text("[API Standards](api.context.md)")
        result = resolver.get_referenced_contexts([source])
        assert ctx_file in result


# ---------------------------------------------------------------------------
# Legacy functions
# ---------------------------------------------------------------------------


class TestResolveMarkdownLinks:
    def test_external_url_preserved(self, base_dir: Path) -> None:
        content = "[Example](https://example.com)"
        result = resolve_markdown_links(content, base_dir)
        urls = re.findall(r"https?://[^\s)\]\}]+", result)
        assert len(urls) == 1
        assert urlparse(urls[0]).hostname == "example.com"

    def test_anchor_link_preserved(self, base_dir: Path) -> None:
        content = "[Section](#heading)"
        result = resolve_markdown_links(content, base_dir)
        assert "#heading" in result

    def test_existing_md_file_inlined(self, base_dir: Path) -> None:
        sub = base_dir / "sub.md"
        sub.write_text("Sub content")
        content = "[Sub](sub.md)"
        result = resolve_markdown_links(content, base_dir)
        assert "Sub content" in result

    def test_missing_file_link_preserved(self, base_dir: Path) -> None:
        content = "[Missing](nonexistent.md)"
        result = resolve_markdown_links(content, base_dir)
        assert "nonexistent.md" in result


class TestValidateLinkTargets:
    def test_valid_external_url_no_errors(self, base_dir: Path) -> None:
        errors = validate_link_targets("[ext](https://example.com)", base_dir)
        assert errors == []

    def test_missing_file_produces_error(self, base_dir: Path) -> None:
        errors = validate_link_targets("[Missing](missing.md)", base_dir)
        assert any("missing.md" in e for e in errors)

    def test_existing_file_no_errors(self, base_dir: Path) -> None:
        f = base_dir / "exists.md"
        f.write_text("content")
        errors = validate_link_targets("[Exists](exists.md)", base_dir)
        assert errors == []

    def test_anchor_link_skipped(self, base_dir: Path) -> None:
        errors = validate_link_targets("[Heading](#heading)", base_dir)
        assert errors == []


class TestResolvePath:
    def test_empty_string_returns_none(self, base_dir: Path) -> None:
        assert _resolve_path("", base_dir) is None

    def test_whitespace_only_returns_none(self, base_dir: Path) -> None:
        assert _resolve_path("   ", base_dir) is None

    def test_nul_byte_returns_none(self, base_dir: Path) -> None:
        assert _resolve_path("file\x00.md", base_dir) is None

    def test_absolute_path_returned_as_is(self, tmp_path: Path) -> None:
        result = _resolve_path(str(tmp_path), tmp_path)
        assert result == tmp_path

    def test_relative_path_resolved_against_base(self, base_dir: Path) -> None:
        result = _resolve_path("sub/file.md", base_dir)
        assert result == base_dir / "sub" / "file.md"


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
