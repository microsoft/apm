"""Acceptance tests for managed-section AGENTS.md updates (issue #1540).

These tests verify:
1. replace-between-markers preserves surrounding content
2. duplicate-marker -> loud error
3. marker-absent -> conservative behavior (error)
"""

import pytest

from apm_cli.compilation.managed_section import (
    ManagedSectionError,
    apply_managed_section,
)

DEFAULT_START = "<!-- apm:start -->"
DEFAULT_END = "<!-- apm:end -->"


class TestApplyManagedSection:
    """Tests for apply_managed_section()."""

    # ------------------------------------------------------------------
    # Acceptance criterion 1: replace between markers, preserve surrounds
    # ------------------------------------------------------------------

    def test_replaces_content_between_markers(self):
        existing = (
            "# Repo guidance\n\n"
            "Human-authored content stays here.\n\n"
            f"{DEFAULT_START}\n"
            "Old generated content.\n"
            f"{DEFAULT_END}\n\n"
            "More human content.\n"
        )
        new_section = "New generated content."

        result = apply_managed_section(existing, new_section, DEFAULT_START, DEFAULT_END)

        assert "Human-authored content stays here." in result
        assert "More human content." in result
        assert "New generated content." in result
        assert "Old generated content." not in result
        assert DEFAULT_START in result
        assert DEFAULT_END in result

    def test_preserves_content_before_marker(self):
        existing = f"# Title\nBefore content.\n{DEFAULT_START}\nOld content.\n{DEFAULT_END}\n"
        result = apply_managed_section(existing, "New.", DEFAULT_START, DEFAULT_END)
        assert result.startswith("# Title\nBefore content.\n")

    def test_preserves_content_after_marker(self):
        existing = f"{DEFAULT_START}\nOld content.\n{DEFAULT_END}\nAfter content.\n"
        result = apply_managed_section(existing, "New.", DEFAULT_START, DEFAULT_END)
        assert "After content." in result
        assert result.endswith("After content.\n") or "After content." in result

    def test_new_section_content_appears_between_markers(self):
        existing = f"{DEFAULT_START}\nOld.\n{DEFAULT_END}\n"
        new_section = "Generated block line 1.\nGenerated block line 2."
        result = apply_managed_section(existing, new_section, DEFAULT_START, DEFAULT_END)

        start_idx = result.index(DEFAULT_START)
        end_idx = result.index(DEFAULT_END)
        between = result[start_idx + len(DEFAULT_START) : end_idx]
        assert "Generated block line 1." in between
        assert "Generated block line 2." in between

    def test_custom_markers_are_respected(self):
        start = "<!-- custom-start -->"
        end = "<!-- custom-end -->"
        existing = f"{start}\nOld.\n{end}\n"
        result = apply_managed_section(existing, "New.", start, end)
        assert "New." in result
        assert "Old." not in result

    def test_empty_new_section_clears_managed_block(self):
        existing = f"Before.\n{DEFAULT_START}\nOld content.\n{DEFAULT_END}\nAfter.\n"
        result = apply_managed_section(existing, "", DEFAULT_START, DEFAULT_END)
        assert "Old content." not in result
        assert "Before." in result
        assert "After." in result

    # ------------------------------------------------------------------
    # Input-validation guards: empty / identical markers
    # ------------------------------------------------------------------

    def test_empty_start_marker_raises_error(self):
        with pytest.raises(ManagedSectionError, match=r"non-empty"):
            apply_managed_section("content", "new", "", DEFAULT_END)

    def test_empty_end_marker_raises_error(self):
        with pytest.raises(ManagedSectionError, match=r"non-empty"):
            apply_managed_section("content", "new", DEFAULT_START, "")

    def test_identical_markers_raises_error(self):
        with pytest.raises(ManagedSectionError, match=r"distinct"):
            apply_managed_section("content", "new", "<!-- x -->", "<!-- x -->")

    # ------------------------------------------------------------------
    # Acceptance criterion 2: duplicate markers -> loud error
    # ------------------------------------------------------------------

    def test_duplicate_start_marker_raises_error(self):
        existing = (
            f"{DEFAULT_START}\n"
            "Section 1.\n"
            f"{DEFAULT_END}\n"
            f"{DEFAULT_START}\n"
            "Section 2.\n"
            f"{DEFAULT_END}\n"
        )
        with pytest.raises(ManagedSectionError, match=r"(?i)duplicate|multiple|more than one"):
            apply_managed_section(existing, "New.", DEFAULT_START, DEFAULT_END)

    def test_duplicate_end_marker_raises_error(self):
        existing = f"{DEFAULT_START}\nSection 1.\n{DEFAULT_END}\nMiddle.\n{DEFAULT_END}\n"
        with pytest.raises(ManagedSectionError, match=r"(?i)duplicate|multiple|more than one"):
            apply_managed_section(existing, "New.", DEFAULT_START, DEFAULT_END)

    def test_reversed_markers_raises_error(self):
        existing = f"{DEFAULT_END}\nContent.\n{DEFAULT_START}\n"
        with pytest.raises(ManagedSectionError, match=r"(?i)before.*start|end.*before|order|first"):
            apply_managed_section(existing, "New.", DEFAULT_START, DEFAULT_END)

    # ------------------------------------------------------------------
    # Acceptance criterion 3: markers absent -> conservative (error)
    # ------------------------------------------------------------------

    def test_missing_both_markers_raises_error(self):
        existing = "# Title\nHuman content only.\n"
        with pytest.raises(ManagedSectionError, match=r"(?i)marker|not found|missing|absent"):
            apply_managed_section(existing, "New.", DEFAULT_START, DEFAULT_END)

    def test_missing_start_marker_raises_error(self):
        existing = f"Some content.\n{DEFAULT_END}\n"
        with pytest.raises(ManagedSectionError, match=r"(?i)marker|not found|missing|absent"):
            apply_managed_section(existing, "New.", DEFAULT_START, DEFAULT_END)

    def test_missing_end_marker_raises_error(self):
        existing = f"{DEFAULT_START}\nSome content.\n"
        with pytest.raises(ManagedSectionError, match=r"(?i)marker|not found|missing|absent"):
            apply_managed_section(existing, "New.", DEFAULT_START, DEFAULT_END)

    def test_error_message_includes_guidance(self):
        """Error messages should tell users what to do."""
        existing = "# Title\nHuman content only.\n"
        with pytest.raises(ManagedSectionError) as exc_info:
            apply_managed_section(existing, "New.", DEFAULT_START, DEFAULT_END)
        # Error message should mention the markers or how to add them
        msg = str(exc_info.value)
        assert DEFAULT_START in msg or DEFAULT_END in msg or "marker" in msg.lower()

    # ------------------------------------------------------------------
    # Issue #1595: message polish
    # ------------------------------------------------------------------

    def test_missing_one_marker_says_missing_not_both(self):
        """When only start marker is absent, message must not say 'both markers'."""
        existing = f"Some content.\n{DEFAULT_END}\n"
        with pytest.raises(ManagedSectionError) as exc_info:
            apply_managed_section(existing, "New.", DEFAULT_START, DEFAULT_END)
        msg = str(exc_info.value)
        assert "both markers" not in msg.lower()
        assert "missing marker" in msg.lower() or "marker(s)" in msg.lower()

    def test_duplicate_only_start_does_not_mention_end_count(self):
        """When only the start marker is duplicated, message must not report end marker count."""
        existing = f"{DEFAULT_START}\nSection 1.\n{DEFAULT_END}\n{DEFAULT_START}\nSection 2.\n"
        with pytest.raises(ManagedSectionError) as exc_info:
            apply_managed_section(existing, "New.", DEFAULT_START, DEFAULT_END)
        msg = str(exc_info.value)
        # end marker appears exactly once -- should not appear in duplicate report
        assert "end marker" not in msg and DEFAULT_END not in msg

    def test_duplicate_only_end_does_not_mention_start_count(self):
        """When only the end marker is duplicated, message must not report start marker count."""
        existing = f"{DEFAULT_START}\nSection 1.\n{DEFAULT_END}\nMiddle.\n{DEFAULT_END}\n"
        with pytest.raises(ManagedSectionError) as exc_info:
            apply_managed_section(existing, "New.", DEFAULT_START, DEFAULT_END)
        msg = str(exc_info.value)
        # start marker appears exactly once -- should not appear in duplicate report
        assert "start marker" not in msg and DEFAULT_START not in msg


class TestManagedSectionInCompilationConfig:
    """Tests for agents_md config parsing in CompilationConfig."""

    def test_default_mode_is_full(self):
        from apm_cli.compilation.agents_compiler import CompilationConfig

        config = CompilationConfig()
        assert config.agents_md_mode == "full"

    def test_default_markers(self):
        from apm_cli.compilation.agents_compiler import CompilationConfig

        config = CompilationConfig()
        assert config.agents_md_start_marker == "<!-- apm:start -->"
        assert config.agents_md_end_marker == "<!-- apm:end -->"

    def test_from_apm_yml_parses_agents_md_section(self, tmp_path, monkeypatch):
        import yaml

        from apm_cli.compilation.agents_compiler import CompilationConfig

        monkeypatch.chdir(tmp_path)
        apm_yml = {
            "compilation": {
                "agents_md": {
                    "mode": "managed_section",
                    "start_marker": "<!-- my-start -->",
                    "end_marker": "<!-- my-end -->",
                }
            }
        }
        (tmp_path / "apm.yml").write_text(yaml.dump(apm_yml))
        config = CompilationConfig.from_apm_yml()
        assert config.agents_md_mode == "managed_section"
        assert config.agents_md_start_marker == "<!-- my-start -->"
        assert config.agents_md_end_marker == "<!-- my-end -->"

    def test_from_apm_yml_mode_only_defaults_markers(self, tmp_path, monkeypatch):
        import yaml

        from apm_cli.compilation.agents_compiler import CompilationConfig

        monkeypatch.chdir(tmp_path)
        apm_yml = {"compilation": {"agents_md": {"mode": "managed_section"}}}
        (tmp_path / "apm.yml").write_text(yaml.dump(apm_yml))
        config = CompilationConfig.from_apm_yml()
        assert config.agents_md_mode == "managed_section"
        assert config.agents_md_start_marker == "<!-- apm:start -->"
        assert config.agents_md_end_marker == "<!-- apm:end -->"

    def test_invalid_mode_raises_value_error(self):
        from apm_cli.compilation.agents_compiler import CompilationConfig

        with pytest.raises(ValueError, match=r"Unknown agents_md\.mode"):
            CompilationConfig(agents_md_mode="managed-section")


class TestManagedSectionWriteIntegration:
    """Integration: when mode=managed_section, write replaces only the section."""

    def test_write_output_file_managed_section(self, tmp_path, monkeypatch):
        """When agents_md_mode=managed_section, writing preserves surrounding content."""
        from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig

        start = "<!-- apm:start -->"
        end = "<!-- apm:end -->"
        output_file = tmp_path / "AGENTS.md"
        output_file.write_text(
            "# Repo guidance\n\n"
            "Human content.\n\n"
            f"{start}\n"
            "Old generated block.\n"
            f"{end}\n\n"
            "Footer.\n"
        )

        config = CompilationConfig(
            output_path=str(output_file),
            agents_md_mode="managed_section",
            agents_md_start_marker=start,
            agents_md_end_marker=end,
            dry_run=False,
        )

        compiler = AgentsCompiler(str(tmp_path))
        compiler._write_output_file_with_config(str(output_file), "New generated block.\n", config)

        written = output_file.read_text()
        assert "Human content." in written
        assert "Footer." in written
        assert "New generated block." in written
        assert "Old generated block." not in written

    def test_write_output_file_managed_section_missing_markers(self, tmp_path):
        """When mode=managed_section and markers absent, error is raised."""
        from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig
        from apm_cli.compilation.managed_section import ManagedSectionError

        start = "<!-- apm:start -->"
        end = "<!-- apm:end -->"
        output_file = tmp_path / "AGENTS.md"
        output_file.write_text("# Repo guidance\n\nHuman content only.\n")

        config = CompilationConfig(
            output_path=str(output_file),
            agents_md_mode="managed_section",
            agents_md_start_marker=start,
            agents_md_end_marker=end,
            dry_run=False,
        )

        compiler = AgentsCompiler(str(tmp_path))
        compiler.config = config
        with pytest.raises(ManagedSectionError):
            compiler._write_output_file_with_config(str(output_file), "New content.\n", config)

    def test_write_reraise_uses_bracket_format(self, tmp_path):
        """Re-raised ManagedSectionError must wrap filename in [brackets]."""
        from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig
        from apm_cli.compilation.managed_section import ManagedSectionError

        start = "<!-- apm:start -->"
        end = "<!-- apm:end -->"
        output_file = tmp_path / "AGENTS.md"
        output_file.write_text("# Repo guidance\n\nHuman content only.\n")

        config = CompilationConfig(
            output_path=str(output_file),
            agents_md_mode="managed_section",
            agents_md_start_marker=start,
            agents_md_end_marker=end,
            dry_run=False,
        )

        compiler = AgentsCompiler(str(tmp_path))
        with pytest.raises(ManagedSectionError) as exc_info:
            compiler._write_output_file_with_config(str(output_file), "New content.\n", config)
        msg = str(exc_info.value)
        # filename must be wrapped in square brackets: [AGENTS.md] ...
        assert msg.startswith("[")
        assert "] " in msg
