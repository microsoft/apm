"""Tests for OpenCode frontmatter validate-and-warn (Phase 1 of #581).

Covers both the pure validator and the install-time integration that
fires it before copy_agent() writes the file to .opencode/agents/.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.integration.opencode_frontmatter import validate_opencode_frontmatter

pytestmark = pytest.mark.unit


class TestValidateOpencodeFrontmatter:
    """Pure unit tests for validate_opencode_frontmatter()."""

    def test_tools_as_dict_no_warning(self):
        msgs = validate_opencode_frontmatter(
            {"tools": {"Read": True, "Grep": False}},
            Path("agent.md"),
        )
        assert msgs == []

    def test_tools_as_list_warns(self):
        msgs = validate_opencode_frontmatter(
            {"tools": ["Read", "Grep"]},
            Path("bad.agent.md"),
        )
        assert len(msgs) == 1
        assert "bad.agent.md" in msgs[0]
        assert "tools" in msgs[0]
        assert "list" in msgs[0]

    def test_tools_as_string_warns(self):
        msgs = validate_opencode_frontmatter(
            {"tools": "Read, Grep, Glob"},
            Path("claude.agent.md"),
        )
        assert len(msgs) == 1
        assert "tools" in msgs[0]
        assert "str" in msgs[0]

    def test_tools_dict_with_non_bool_value_warns(self):
        msgs = validate_opencode_frontmatter(
            {"tools": {"Read": "yes"}},
            Path("bad.agent.md"),
        )
        assert len(msgs) == 1
        assert "non-boolean" in msgs[0]

    def test_color_hex_no_warning(self):
        msgs = validate_opencode_frontmatter(
            {"color": "#aabbcc"},
            Path("agent.md"),
        )
        assert msgs == []

    def test_color_short_hex_no_warning(self):
        msgs = validate_opencode_frontmatter({"color": "#abc"}, Path("a.md"))
        assert msgs == []

    def test_color_theme_enum_no_warning(self):
        for theme in ("primary", "secondary", "accent", "success", "warning", "error", "info"):
            assert validate_opencode_frontmatter({"color": theme}, Path("a.md")) == []

    def test_color_named_not_in_enum_warns(self):
        msgs = validate_opencode_frontmatter(
            {"color": "cyan"},
            Path("cyan.agent.md"),
        )
        assert len(msgs) == 1
        assert "color" in msgs[0]
        assert "cyan" in msgs[0]

    def test_color_non_string_warns(self):
        msgs = validate_opencode_frontmatter({"color": 123}, Path("a.md"))
        assert len(msgs) == 1
        assert "color" in msgs[0]

    def test_empty_frontmatter_no_warning(self):
        assert validate_opencode_frontmatter({}, Path("a.md")) == []
        assert validate_opencode_frontmatter(None, Path("a.md")) == []

    def test_multiple_problems_each_warned(self):
        msgs = validate_opencode_frontmatter(
            {"tools": ["Read"], "color": "magenta"},
            Path("a.md"),
        )
        assert len(msgs) == 2

    def test_messages_are_ascii(self):
        msgs = validate_opencode_frontmatter(
            {"tools": ["x"], "color": "magenta"},
            Path("a.md"),
        )
        for m in msgs:
            m.encode("ascii")  # raises if non-ASCII

    def test_non_ascii_filename_sanitized(self):
        msgs = validate_opencode_frontmatter(
            {"color": "magenta"},
            Path("cy\u00e1n-agent.md"),
        )
        assert len(msgs) == 1
        # Non-ASCII filename codepoints replaced with '?' so message stays ASCII.
        msgs[0].encode("ascii")
        assert "?" in msgs[0]

    def test_non_ascii_color_value_sanitized(self):
        msgs = validate_opencode_frontmatter(
            {"color": "magent\u00e1"},
            Path("a.md"),
        )
        assert len(msgs) == 1
        # ascii() escapes non-ASCII codepoints in the repr.
        msgs[0].encode("ascii")
        assert "\\xe1" in msgs[0]

    def test_non_ascii_tool_key_and_value_sanitized(self):
        msgs = validate_opencode_frontmatter(
            {"tools": {"R\u00e9ad": "y\u00e8s"}},
            Path("a.md"),
        )
        assert len(msgs) == 1
        msgs[0].encode("ascii")
        # Both key and value escaped via ascii() rather than raw !r.
        assert "\\xe9" in msgs[0]
        assert "\\xe8" in msgs[0]

    def test_fm_none_accepted_by_signature(self):
        # Annotation is dict | None; calling with None must not raise.
        assert validate_opencode_frontmatter(None, Path("a.md")) == []

    def test_package_qualifier_prefixes_identifier(self):
        # Multi-package installs benefit from knowing which dependency
        # shipped the bad frontmatter; the package name appears as a
        # '<pkg>/<file>' prefix in every warning.
        msgs = validate_opencode_frontmatter(
            {"tools": ["Read"]},
            Path("demo.agent.md"),
            package_name="acme/security-pack",
        )
        assert len(msgs) == 1
        assert "'acme/security-pack/demo.agent.md'" in msgs[0]

    def test_package_qualifier_omitted_when_not_supplied(self):
        # Backward compatibility: bare filename when no package given.
        msgs = validate_opencode_frontmatter(
            {"tools": ["Read"]},
            Path("demo.agent.md"),
        )
        assert "'demo.agent.md'" in msgs[0]

    def test_package_qualifier_sanitized(self):
        # ASCII control chars and non-ASCII codepoints in the package
        # name are stripped/replaced so a malicious package name can
        # never inject ANSI escapes via the warning channel.
        msgs = validate_opencode_frontmatter(
            {"color": "magenta"},
            Path("a.md"),
            package_name="evil\x1b[31mpkg",
        )
        assert len(msgs) == 1
        msgs[0].encode("ascii")
        assert "\x1b" not in msgs[0]

    def test_filename_control_chars_stripped(self):
        # ASCII control chars in the filename (DEL, ESC, BEL) get
        # replaced by '?' rather than echoed verbatim, defending the
        # terminal against agent files crafted to inject escape codes.
        msgs = validate_opencode_frontmatter(
            {"color": "magenta"},
            Path("a\x1b[31mb.agent.md"),
        )
        assert len(msgs) == 1
        msgs[0].encode("ascii")
        assert "\x1b" not in msgs[0]
        assert "?" in msgs[0]

    def test_remediation_pointer_in_tools_warning(self):
        msgs = validate_opencode_frontmatter({"tools": ["Read"]}, Path("a.md"))
        assert "Fix:" in msgs[0]
        assert "tools:" in msgs[0]

    def test_remediation_pointer_in_color_warning(self):
        msgs = validate_opencode_frontmatter({"color": "cyan"}, Path("a.md"))
        assert "Fix:" in msgs[0]
        # Both 3- and 6-char hex literals are accepted by the validator;
        # the remediation pointer must mention both so users aren't
        # misled into thinking only '#rrggbb' is allowed.
        assert "#rgb" in msgs[0]
        assert "#rrggbb" in msgs[0]
