"""frontmatter-bom-bounded-yaml owner: bounded, BOM-safe front matter (#4121)."""

from __future__ import annotations

import io

import pytest
import yaml

from apm_cli.utils.yaml_io import _bounded_load, load_frontmatter, loads_frontmatter

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-frontmatter-bom-bounded-yaml")]


class TestBoundedFrontmatterOwner:
    """Exact contracts of the bounded front-matter owner."""

    def test_bounded_load_names_the_underlying_failure(self):

        with pytest.raises(yaml.YAMLError) as caught:
            _bounded_load("x: " + "1" * 6000)

        assert str(caught.value).startswith("bounded YAML parse failed: ValueError: ")

    def test_load_frontmatter_honours_the_requested_encoding(self, tmp_path):

        raw = b"---\ntitle: caf\xe9\n---\nbody\n"
        path = tmp_path / "doc.md"
        path.write_bytes(raw)

        assert load_frontmatter(path, encoding="latin-1").metadata == {"title": "caf\u00e9"}
        assert load_frontmatter(io.BytesIO(raw), encoding="latin-1").metadata == {
            "title": "caf\u00e9"
        }

    def test_load_frontmatter_reads_other_objects_as_text(self):

        class _Doc:
            def __str__(self) -> str:
                return "---\nname: x\n---\nbody\n"

        post = load_frontmatter(_Doc())

        assert post.metadata == {"name": "x"}
        assert post.content == "body"

    def test_bom_is_stripped_even_without_front_matter(self):

        post = loads_frontmatter("\ufeffplain body")

        assert post.metadata == {}
        assert post.content == "plain body"

    def test_default_body_is_normalized_but_preserve_body_keeps_it(self):

        text = "---\na: 1\n---\n\n  body\ufeff"

        assert loads_frontmatter(text).content == "body\ufeff"
        assert loads_frontmatter(text, preserve_body=True).content == "\n  body\ufeff"

    def test_preserve_body_without_trailing_line_break_is_empty(self):

        post = loads_frontmatter("---\na: 1\n---", preserve_body=True)

        assert post.metadata == {"a": 1}
        assert post.content == ""

    def test_self_referencing_frontmatter_fails_closed(self):
        """A self-referencing anchor is refused by the bounded loader, not built as a cycle."""
        with pytest.raises(yaml.YAMLError, match="expansion exceeded the safe budget"):
            loads_frontmatter("---\na: &a [*a]\n---\nbody\n")
