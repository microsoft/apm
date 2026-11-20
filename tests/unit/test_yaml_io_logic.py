"""Tests for apm_cli.utils.yaml_io -- cross-platform UTF-8 YAML I/O (pure logic cases)."""

import pytest
import yaml

from apm_cli.utils.yaml_io import (
    load_yaml_str,
    loads_frontmatter,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-frontmatter-bom-bounded-yaml")]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("body", ["\n  indented\n\n", "  indented\n", "", "\n\n"])
def test_frontmatter_preserves_body_after_closing_fence(newline, body):
    text = ("\ufeff---\ndescription: Prompt\n---\n" + body).replace("\n", newline)
    post = loads_frontmatter(text, preserve_body=True)
    assert post.metadata == {"description": "Prompt"}
    assert post.content == body.replace("\n", newline)


class TestLoadYamlStr:
    """Tests for load_yaml_str() -- the in-memory bounded twin of load_yaml."""

    def test_parses_mapping(self):
        """A valid YAML string returns the parsed mapping."""
        data = load_yaml_str("name: demo\nversion: 1\n")
        assert data == {"name": "demo", "version": 1}

    def test_empty_string_returns_none(self):
        """Empty input parses to None (mirrors load_yaml on an empty file)."""
        assert load_yaml_str("") is None

    def test_malformed_raises_yaml_error(self):
        """Malformed YAML raises yaml.YAMLError."""
        with pytest.raises(yaml.YAMLError):
            load_yaml_str("key: [unterminated\n")

    def test_merge_key_bomb_fails_closed(self):
        """An eager << merge-key bomb fails closed as yaml.YAMLError, not a hang."""
        lines = ["a0: &a0 {k: v}"]
        prev = "a0"
        for i in range(1, 40):
            cur = f"a{i}"
            lines.append(f"{cur}: &{cur}")
            lines.append(f"  <<: [*{prev}, *{prev}]")
            prev = cur
        bomb = "\n".join(lines) + "\n"
        with pytest.raises(yaml.YAMLError):
            load_yaml_str(bomb)

    def test_huge_int_normalized_to_yaml_error(self):
        """A huge decimal-int scalar (past int_max_str_digits) is normalized to YAMLError."""
        bomb = "bignum: " + ("9" * 6000) + "\n"
        with pytest.raises(yaml.YAMLError):
            load_yaml_str(bomb)

    def test_benign_reused_anchor_dag_still_parses(self):
        """A benign reused-anchor DAG (shared refs, no expansion) still parses."""
        doc = "base: &b {x: 1}\nuse1: *b\nuse2: *b\n"
        data = load_yaml_str(doc)
        assert data["use1"] == {"x": 1}
        assert data["use2"] == {"x": 1}


class TestBoundedMergeHappyPath:
    """Legitimate YAML merge keys resolve through the bounded flatten_mapping."""

    def test_single_mapping_merge(self):
        """A `<<: *anchor` mapping merge resolves identically to stock PyYAML."""
        text = "base: &base\n  timeout: 30\n  retries: 3\njob:\n  <<: *base\n  retries: 5\n"
        data = load_yaml_str(text)
        assert data["job"]["timeout"] == 30
        assert data["job"]["retries"] == 5

    def test_sequence_merge_list_of_mappings(self):
        """A `<<: [*a, *b]` sequence merge flattens all referenced mappings."""
        text = "a: &a\n  x: 1\nb: &b\n  y: 2\nmerged:\n  <<: [*a, *b]\n  z: 3\n"
        data = load_yaml_str(text)
        assert data["merged"]["x"] == 1
        assert data["merged"]["y"] == 2
        assert data["merged"]["z"] == 3

    def test_nested_mapping_merge(self):
        """A merge whose value is itself a merged mapping flattens recursively."""
        text = "root: &root\n  a: 1\nmid: &mid\n  <<: *root\n  b: 2\nleaf:\n  <<: *mid\n  c: 3\n"
        data = load_yaml_str(text)
        assert data["leaf"] == {"a": 1, "b": 2, "c": 3}

    def test_merge_with_scalar_value_rejected(self):
        """A `<<:` whose value is a plain scalar raises a constructor error."""
        text = "bad:\n  <<: notamapping\n  k: v\n"
        with pytest.raises(yaml.YAMLError):
            load_yaml_str(text)

    def test_sequence_merge_with_scalar_member_rejected(self):
        """A `<<: [*a, scalar]` with a non-mapping member raises."""
        text = "a: &a\n  x: 1\nbad:\n  <<: [*a, plainscalar]\n"
        with pytest.raises(yaml.YAMLError):
            load_yaml_str(text)
