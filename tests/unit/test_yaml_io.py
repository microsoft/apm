"""Tests for apm_cli.utils.yaml_io -- cross-platform UTF-8 YAML I/O."""

import io

import pytest
import yaml
from ruamel.yaml import YAMLError as RuamelYAMLError

from apm_cli.utils.yaml_io import (
    _roundtrip_yaml,
    dump_yaml,
    dump_yaml_roundtrip,
    load_frontmatter,
    load_yaml,
    load_yaml_roundtrip,
    load_yaml_str,
    yaml_to_str,
)


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

    @pytest.mark.windows_compat
    def test_dump_writes_lf_only_bytes(self, tmp_path):
        """On-disk bytes use LF exclusively on every platform (apm#2619).

        Callers such as ``stamp_plugin_version`` rewrite ``apm.yml`` INSIDE
        an installed package tree that ``compute_package_hash`` hashes raw,
        so a platform-native CRLF write on Windows would make the lockfile
        ``content_hash`` diverge from POSIX for identical upstream content.
        """
        p = tmp_path / "test.yml"
        dump_yaml(
            {"name": "skills", "version": "2c7ec5e", "description": "", "type": "hybrid"},
            p,
        )
        raw = p.read_bytes()
        assert b"\r" not in raw
        assert raw.endswith(b"\n")


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


class TestLoadYamlRoundtrip:
    """Tests for comment-preserving YAML round-trip helpers."""

    def test_roundtrip_preserves_comments(self, tmp_path):
        """A load/mutate/dump cycle preserves existing comments."""
        p = tmp_path / "apm.yml"
        p.write_text(
            "# project note\nname: demo\n# deps note\ndependencies: []\n", encoding="utf-8"
        )

        data = load_yaml_roundtrip(p)
        data["dependencies"].append("github:owner/new-skill@v1")
        dump_yaml_roundtrip(data, p)

        rendered = p.read_text(encoding="utf-8")
        assert "# project note" in rendered
        assert "# deps note" in rendered
        assert "github:owner/new-skill@v1" in rendered

    @pytest.mark.windows_compat
    def test_dump_roundtrip_writes_deterministic_lf(self, tmp_path):
        """Rewrites land as LF bytes on every OS -- no CRLF flip-flop (apm#2624).

        Project-file rewrite paths (install / uninstall / package
        resolution) previously wrote platform-native newlines here while
        other commands wrote LF, so a Windows apm.yml alternated line
        endings between consecutive commands and churned git diffs.
        """
        p = tmp_path / "apm.yml"
        p.write_bytes(b"# note\r\nname: demo\r\ndependencies: []\r\n")

        data = load_yaml_roundtrip(p)
        dump_yaml_roundtrip(data, p)

        raw = p.read_bytes()
        assert b"\r" not in raw
        assert b"# note\n" in raw

    def test_alias_expansion_bomb_fails_closed(self, tmp_path):
        """The round-trip path keeps the bounded-loader expansion guard."""
        p = tmp_path / "apm.yml"
        lines = ["l0: &l0 [x]"]
        prev = "l0"
        for i in range(1, 25):
            cur = f"l{i}"
            lines.append(f"{cur}: &{cur} [*{prev}, *{prev}]")
            prev = cur
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with pytest.raises(yaml.YAMLError):
            load_yaml_roundtrip(p)

    def test_roundtrip_parser_rejects_python_tags(self):
        """The ruamel layer also fails closed on unsafe Python tags."""
        with pytest.raises(RuamelYAMLError):
            _roundtrip_yaml().load("x: !!python/name:os.system ''\n")


class TestLoadFrontmatter:
    """Tests for load_frontmatter() -- bounded python-frontmatter entrypoint."""

    def test_parses_metadata_and_content(self):
        """Valid front matter yields the metadata mapping and body content."""
        text = "---\nmcp:\n  - github\n---\nbody text\n"
        post = load_frontmatter(io.StringIO(text))
        assert post.metadata["mcp"] == ["github"]
        assert "body text" in post.content

    def test_no_frontmatter_returns_empty_metadata(self):
        """A plain document with no front matter yields empty metadata."""
        post = load_frontmatter(io.StringIO("just body, no fence\n"))
        assert post.metadata == {}

    def test_merge_bomb_frontmatter_fails_closed(self):
        """A merge-key bomb in front matter fails closed as yaml.YAMLError."""
        lines = ["a0: &a0 {k: v}"]
        prev = "a0"
        for i in range(1, 40):
            cur = f"a{i}"
            lines.append(f"{cur}: &{cur}")
            lines.append(f"  <<: [*{prev}, *{prev}]")
            prev = cur
        bomb = "---\n" + "\n".join(lines) + "\n---\nbody\n"
        with pytest.raises(yaml.YAMLError):
            load_frontmatter(io.StringIO(bomb))

    @pytest.mark.windows_compat
    def test_leading_bom_does_not_break_the_fence(self, tmp_path):
        """A UTF-8 BOM (PowerShell/Notepad on Windows) doesn't hide the front matter (apm#2683)."""
        text = '---\ndescription: Scoped rule\napplyTo: "**/*.py"\n---\n# Style\n'
        path = tmp_path / "withbom.md"
        path.write_bytes(text.encode("utf-8-sig"))
        post = load_frontmatter(str(path))
        assert post.metadata["applyTo"] == "**/*.py"
        assert post.metadata["description"] == "Scoped rule"

    @pytest.mark.windows_compat
    def test_bom_and_no_bom_files_parse_identically(self, tmp_path):
        """A BOM'd and a plain file with the same content yield the same metadata."""
        text = '---\napplyTo: "**/*.py"\n---\nbody\n'
        no_bom = tmp_path / "nobom.md"
        with_bom = tmp_path / "withbom.md"
        no_bom.write_bytes(text.encode("utf-8"))
        with_bom.write_bytes(text.encode("utf-8-sig"))
        assert load_frontmatter(str(no_bom)).metadata == load_frontmatter(str(with_bom)).metadata

    @pytest.mark.windows_compat
    def test_leading_bom_is_stripped_from_already_open_utf8_stream(self, tmp_path):
        """The shared parser owns BOM removal even for already-open streams."""
        text = '---\napplyTo: "**/*.py"\n---\nbody\n'
        path = tmp_path / "withbom.md"
        path.write_bytes(text.encode("utf-8-sig"))

        with path.open(encoding="utf-8") as f:
            assert load_frontmatter(f).metadata["applyTo"] == "**/*.py"


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


class TestWriteYamlTextAtomic:
    """Tests for write_yaml_text_atomic()."""

    def test_atomic_write_creates_file(self, tmp_path):
        """The rendered text lands at the target path."""
        from apm_cli.utils.yaml_io import write_yaml_text_atomic

        target = tmp_path / "out.yml"
        write_yaml_text_atomic(target, "key: value\n")
        assert target.read_text(encoding="utf-8") == "key: value\n"

    def test_atomic_write_replaces_existing(self, tmp_path):
        """An existing file is replaced wholesale by the new content."""
        from apm_cli.utils.yaml_io import write_yaml_text_atomic

        target = tmp_path / "out.yml"
        target.write_text("old: 1\n", encoding="utf-8")
        write_yaml_text_atomic(target, "new: 2\n")
        assert target.read_text(encoding="utf-8") == "new: 2\n"

    @pytest.mark.windows_compat
    def test_atomic_write_is_lf_deterministic(self, tmp_path):
        """On-disk bytes are LF regardless of platform or input domain (apm#2624)."""
        from apm_cli.utils.yaml_io import write_yaml_text_atomic

        target = tmp_path / "out.yml"
        write_yaml_text_atomic(target, "a: 1\nb: 2\n")
        assert target.read_bytes() == b"a: 1\nb: 2\n"

        write_yaml_text_atomic(target, "a: 1\r\nb: 2\r\n")
        assert target.read_bytes() == b"a: 1\nb: 2\n"

    def test_atomic_write_leaves_original_on_replace_failure(self, tmp_path, monkeypatch):
        """If os.replace fails, the original file is untouched and temp cleaned."""

        from apm_cli.utils import atomic_io
        from apm_cli.utils.yaml_io import write_yaml_text_atomic

        target = tmp_path / "out.yml"
        target.write_text("orig: 1\n", encoding="utf-8")

        def _boom(src, dst):
            raise OSError("replace denied")

        monkeypatch.setattr(atomic_io, "_replace_atomic_file", _boom)
        with pytest.raises(OSError):
            write_yaml_text_atomic(target, "new: 2\n")
        assert target.read_text(encoding="utf-8") == "orig: 1\n"
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".out.yml.")]
        assert leftovers == []
