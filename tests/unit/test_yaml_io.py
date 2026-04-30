"""Tests for apm_cli.utils.yaml_io -- cross-platform UTF-8 YAML I/O."""

import pytest
import yaml

from apm_cli.utils.yaml_io import dump_yaml, load_yaml, yaml_to_str


class TestLoadYaml:
    """Tests for load_yaml()."""

    def test_load_utf8_content(self, tmp_path):
        """Non-ASCII content is read correctly."""
        p = tmp_path / "test.yml"
        p.write_text('author: "Lopez"\n', encoding="utf-8")
        data = load_yaml(p)
        assert data["author"] == "Lopez"

    def test_load_unicode_author(self, tmp_path):
        """Unicode characters (accented, CJK) are preserved."""
        p = tmp_path / "test.yml"
        # YAML \xF3 escape is decoded by the parser into the real char
        p.write_text('author: "L\\xF3pez"\n', encoding="utf-8")
        data = load_yaml(p)
        assert data["author"] == "L\u00f3pez"

    def test_load_real_utf8_bytes(self, tmp_path):
        """Real UTF-8 encoded non-ASCII round-trips correctly."""
        p = tmp_path / "test.yml"
        # Write raw UTF-8 bytes (as allow_unicode=True would produce)
        content = "author: L\u00f3pez\norg: \u7530\u4e2d\u592a\u90ce\n"
        p.write_text(content, encoding="utf-8")
        data = load_yaml(p)
        assert data["author"] == "L\u00f3pez"
        assert data["org"] == "\u7530\u4e2d\u592a\u90ce"

    def test_load_empty_file(self, tmp_path):
        """Empty YAML file returns None."""
        p = tmp_path / "empty.yml"
        p.write_text("", encoding="utf-8")
        assert load_yaml(p) is None

    def test_load_file_not_found(self):
        """Missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_yaml("/nonexistent/path.yml")

    def test_load_invalid_yaml(self, tmp_path):
        """Malformed YAML raises yaml.YAMLError."""
        p = tmp_path / "bad.yml"
        p.write_text(":\n  - :\n  bad: [unmatched", encoding="utf-8")
        with pytest.raises(yaml.YAMLError):
            load_yaml(p)


class TestDumpYaml:
    """Tests for dump_yaml()."""

    def test_dump_utf8_roundtrip(self, tmp_path):
        """Non-ASCII data survives write -> read cycle."""
        p = tmp_path / "test.yml"
        dump_yaml({"author": "L\u00f3pez"}, p)
        assert load_yaml(p)["author"] == "L\u00f3pez"

    def test_dump_unicode_not_escaped(self, tmp_path):
        """File contains real UTF-8, not \\xNN escape sequences."""
        p = tmp_path / "test.yml"
        dump_yaml({"author": "L\u00f3pez"}, p)
        raw = p.read_bytes()
        assert b"\\xf3" not in raw
        assert b"\\xF3" not in raw
        assert "L\u00f3pez".encode("utf-8") in raw

    def test_dump_cjk_characters(self, tmp_path):
        """CJK characters are written as real UTF-8."""
        p = tmp_path / "test.yml"
        dump_yaml({"author": "\u7530\u4e2d\u592a\u90ce"}, p)
        raw = p.read_text(encoding="utf-8")
        assert "\u7530\u4e2d\u592a\u90ce" in raw
        assert "\\u" not in raw

    def test_dump_preserves_key_order(self, tmp_path):
        """Keys stay in insertion order (sort_keys=False default)."""
        p = tmp_path / "test.yml"
        dump_yaml({"z": 1, "a": 2, "m": 3}, p)
        lines = p.read_text(encoding="utf-8").strip().split("\n")
        keys = [line.split(":")[0] for line in lines]
        assert keys == ["z", "a", "m"]

    def test_dump_sort_keys_option(self, tmp_path):
        """sort_keys=True sorts alphabetically."""
        p = tmp_path / "test.yml"
        dump_yaml({"z": 1, "a": 2, "m": 3}, p, sort_keys=True)
        lines = p.read_text(encoding="utf-8").strip().split("\n")
        keys = [line.split(":")[0] for line in lines]
        assert keys == ["a", "m", "z"]

    def test_dump_block_style(self, tmp_path):
        """Output uses block style (not flow/inline)."""
        p = tmp_path / "test.yml"
        dump_yaml({"items": ["a", "b", "c"]}, p)
        raw = p.read_text(encoding="utf-8")
        assert "- a" in raw
        assert "{" not in raw


class TestYamlToStr:
    """Tests for yaml_to_str()."""

    def test_unicode_preserved(self):
        """String serialization preserves unicode characters."""
        result = yaml_to_str({"author": "\u7530\u4e2d\u592a\u90ce"})
        assert "\u7530\u4e2d\u592a\u90ce" in result
        assert "\\u" not in result

    def test_latin_unicode(self):
        """Latin extended characters preserved."""
        result = yaml_to_str({"name": "L\u00f3pez S\u00e1nchez"})
        assert "L\u00f3pez" in result
        assert "\\x" not in result

    def test_preserves_key_order(self):
        """Keys stay in insertion order by default."""
        result = yaml_to_str({"z": 1, "a": 2})
        assert result.index("z") < result.index("a")

    def test_returns_string(self):
        """Return type is str, not bytes."""
        result = yaml_to_str({"key": "value"})
        assert isinstance(result, str)


class TestCrossPlatformSafety:
    """Simulate the Windows cp1252 mismatch scenario."""

    def test_utf8_written_reads_back_correctly(self, tmp_path):
        """Verify that dump_yaml output reads back identically via load_yaml.

        This is the core regression test: on Windows without explicit
        encoding, the read would produce mojibake.
        """
        p = tmp_path / "test.yml"
        original = {
            "name": "my-project",
            "author": "Alejandro L\u00f3pez S\u00e1nchez",
            "description": "A project by \u7530\u4e2d\u592a\u90ce",
        }
        dump_yaml(original, p)
        loaded = load_yaml(p)
        assert loaded == original

    def test_raw_bytes_are_utf8(self, tmp_path):
        """The file on disk is valid UTF-8 (not cp1252 or latin-1)."""
        p = tmp_path / "test.yml"
        dump_yaml({"author": "L\u00f3pez"}, p)
        raw_bytes = p.read_bytes()
        decoded = raw_bytes.decode("utf-8")
        assert "L\u00f3pez" in decoded
