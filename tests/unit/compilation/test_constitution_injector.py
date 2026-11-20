"""Tests for constitution_block helpers."""

from __future__ import annotations

import pytest

from apm_cli.compilation.constants import (
    CONSTITUTION_MARKER_BEGIN,
    CONSTITUTION_MARKER_END,
    CONSTITUTION_RELATIVE_PATH,
)
from apm_cli.compilation.constitution_block import (
    ExistingBlock,
    compute_constitution_hash,
    find_existing_block,
    inject_or_update,
    render_block,
)

pytestmark = pytest.mark.unit

_BEGIN = CONSTITUTION_MARKER_BEGIN
_END = CONSTITUTION_MARKER_END


# ---------------------------------------------------------------------------
# render_block
# ---------------------------------------------------------------------------


class TestRenderBlock:
    def test_contains_begin_and_end_markers(self):
        block = render_block("Some rule.")
        assert _BEGIN in block
        assert _END in block

    def test_contains_constitution_content(self):
        block = render_block("Rule A\nRule B\n")
        assert "Rule A" in block
        assert "Rule B" in block

    def test_hash_line_present(self):
        block = render_block("content")
        lines = block.splitlines()
        assert any(l.startswith("hash:") for l in lines)  # noqa: E741

    def test_hash_matches_compute(self):
        content = "My constitution text"
        block = render_block(content)
        expected_hash = compute_constitution_hash(content)
        assert expected_hash in block

    def test_path_present(self):
        block = render_block("x")
        assert CONSTITUTION_RELATIVE_PATH in block

    def test_ends_with_newline(self):
        block = render_block("x")
        assert block.endswith("\n")

    def test_strips_trailing_whitespace_from_content(self):
        block = render_block("content   \n\n")
        # Content should be included but not cause extra blank lines inside block
        assert "content" in block


# ---------------------------------------------------------------------------
# find_existing_block
# ---------------------------------------------------------------------------


class TestFindExistingBlock:
    def _make_block_content(self, inner="Rule 1\n") -> str:
        return f"{_BEGIN}\nhash: abc123\n{inner}{_END}\n"

    def test_returns_none_when_no_block(self):
        result = find_existing_block("# No markers here\n")
        assert result is None

    def test_returns_existing_block(self):
        content = self._make_block_content()
        result = find_existing_block(content)
        assert result is not None
        assert isinstance(result, ExistingBlock)

    def test_extracts_hash(self):
        content = self._make_block_content()
        result = find_existing_block(content)
        assert result.hash == "abc123"

    def test_no_hash_returns_none_hash(self):
        content = f"{_BEGIN}\nno hash line\n{_END}\n"
        result = find_existing_block(content)
        assert result is not None
        assert result.hash is None

    def test_start_and_end_indices(self):
        prefix = "# Header\n\n"
        block_text = self._make_block_content()
        content = prefix + block_text + "\n# Body\n"
        result = find_existing_block(content)
        assert result is not None
        assert result.start_index == len(prefix)
        # end_index points to end of closing marker (regex excludes trailing newline)
        assert content[result.start_index : result.end_index] == block_text.rstrip()

    def test_raw_contains_markers(self):
        content = self._make_block_content()
        result = find_existing_block(content)
        assert _BEGIN in result.raw
        assert _END in result.raw


# ---------------------------------------------------------------------------
# inject_or_update
# ---------------------------------------------------------------------------


class TestInjectOrUpdate:
    def _rendered_block(self, content: str) -> str:
        return render_block(content)

    def test_creates_block_when_none_exists(self):
        agents_text = "# Existing content\n"
        new_block = self._rendered_block("My rule")
        updated, status = inject_or_update(agents_text, new_block)
        assert status == "CREATED"
        assert _BEGIN in updated
        assert _END in updated

    def test_created_block_at_top_by_default(self):
        agents_text = "# Existing content\n"
        new_block = self._rendered_block("My rule")
        updated, status = inject_or_update(agents_text, new_block)  # noqa: RUF059
        assert updated.startswith(_BEGIN)

    def test_updates_changed_block(self):
        original_block = self._rendered_block("Old rule")
        agents_text = original_block + "# Body\n"
        new_block = self._rendered_block("New rule")
        updated, status = inject_or_update(agents_text, new_block)
        assert status == "UPDATED"
        assert "New rule" in updated
        assert "Old rule" not in updated

    def test_unchanged_when_same_block(self):
        block = self._rendered_block("Same rule")
        agents_text = block.rstrip() + "\n# Body\n"
        # inject_or_update compares raw vs new_block.rstrip()
        updated, status = inject_or_update(agents_text, block)
        assert status == "UNCHANGED"
        assert updated == agents_text

    def test_creates_at_bottom_when_place_top_false(self):
        agents_text = "# Header\n"
        new_block = self._rendered_block("rule")
        updated, status = inject_or_update(agents_text, new_block, place_top=False)
        assert status == "CREATED"
        assert updated.startswith("# Header\n")
        assert _BEGIN in updated

    def test_empty_existing_agents(self):
        new_block = self._rendered_block("rule")
        updated, status = inject_or_update("", new_block)
        assert status == "CREATED"
        assert _BEGIN in updated


# ---------------------------------------------------------------------------
# ConstitutionInjector
# ---------------------------------------------------------------------------
