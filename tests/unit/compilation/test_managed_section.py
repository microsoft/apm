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

    def test_write_output_file_managed_section_file_missing(self, tmp_path):
        """When mode=managed_section and target file does not exist, error says file missing.

        This tests issue #1593: when the file doesn't exist yet, the error must
        clearly say 'does not exist' rather than the confusing 'markers not found'.
        """
        from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig
        from apm_cli.compilation.managed_section import ManagedSectionError

        start = "<!-- apm:start -->"
        end = "<!-- apm:end -->"
        output_file = tmp_path / "AGENTS.md"
        # File is intentionally NOT created

        config = CompilationConfig(
            output_path=str(output_file),
            agents_md_mode="managed_section",
            agents_md_start_marker=start,
            agents_md_end_marker=end,
            dry_run=False,
        )

        compiler = AgentsCompiler(str(tmp_path))
        with pytest.raises(ManagedSectionError, match=r"(?i)does not exist|not exist|create it"):
            compiler._write_output_file_with_config(str(output_file), "New content.\n", config)

    def test_write_output_file_managed_section_directory_at_path(self, tmp_path):
        """When mode=managed_section and a directory occupies the target path, raise ManagedSectionError.

        Regression trap for the is_file() guard: a directory at the output path must
        produce a clear ManagedSectionError, not an opaque IsADirectoryError/OSError.
        """
        from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig
        from apm_cli.compilation.managed_section import ManagedSectionError

        start = "<!-- apm:start -->"
        end = "<!-- apm:end -->"
        output_file = tmp_path / "AGENTS.md"
        output_file.mkdir()  # directory at the target path, not a regular file

        config = CompilationConfig(
            output_path=str(output_file),
            agents_md_mode="managed_section",
            agents_md_start_marker=start,
            agents_md_end_marker=end,
            dry_run=False,
        )

        compiler = AgentsCompiler(str(tmp_path))
        with pytest.raises(ManagedSectionError, match=r"(?i)does not exist|not exist|create it"):
            compiler._write_output_file_with_config(str(output_file), "New content.\n", config)


class TestManagedSectionDistributed:
    """Regression tests for issue #1764: managed_section honoured on the
    distributed (default) and --single-agents write paths, not just the
    legacy single-file path."""

    def test_distributed_root_agents_md_preserves_human_content(self, tmp_path):
        """AC-1: root AGENTS.md in managed_section mode preserves surrounding content."""
        from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig

        intro = "Hand-written intro that must survive."
        footer = "Hand-written footer that must survive."
        agents_md = tmp_path / "AGENTS.md"
        agents_md.write_text(
            f"{intro}\n\n{DEFAULT_START}\nOld generated block.\n{DEFAULT_END}\n\n{footer}\n"
        )
        config = CompilationConfig(
            agents_md_mode="managed_section", with_constitution=False, dry_run=False
        )
        compiler = AgentsCompiler(str(tmp_path))
        compiler._write_distributed_file(agents_md, "New generated block.", config)

        written = agents_md.read_text()
        assert intro in written
        assert footer in written
        assert "New generated block." in written
        assert "Old generated block." not in written
        assert written.count(DEFAULT_START) == 1
        assert written.count(DEFAULT_END) == 1

    def test_distributed_subdir_agents_md_overwritten_in_managed_mode(self, tmp_path):
        """AC-2: subdirectory AGENTS.md is fully overwritten even in managed_section mode."""
        from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig

        src = tmp_path / "src"
        src.mkdir()
        subdir_agents = src / "AGENTS.md"
        subdir_agents.write_text("Arbitrary subdir content with no markers.\n")
        config = CompilationConfig(
            agents_md_mode="managed_section", with_constitution=False, dry_run=False
        )
        compiler = AgentsCompiler(str(tmp_path))
        compiler._write_distributed_file(subdir_agents, "Fresh subdir content.", config)

        written = subdir_agents.read_text()
        assert "Arbitrary subdir content with no markers." not in written
        assert "Fresh subdir content." in written

    def test_distributed_root_agents_md_full_mode_still_overwrites(self, tmp_path):
        """AC-3: in full mode the root AGENTS.md is byte-for-byte replaced."""
        from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig

        agents_md = tmp_path / "AGENTS.md"
        agents_md.write_text("# Old content with no markers.\nSome prose.\n")
        config = CompilationConfig(agents_md_mode="full", with_constitution=False, dry_run=False)
        compiler = AgentsCompiler(str(tmp_path))
        compiler._write_distributed_file(agents_md, "Brand new content.", config)

        written = agents_md.read_text()
        assert written == "Brand new content."
        assert "# Old content with no markers." not in written

    def test_distributed_root_managed_section_missing_file_errors(self, tmp_path):
        """AC-4: missing root AGENTS.md in managed_section mode surfaces via errors, not a traceback."""
        from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig

        instr = tmp_path / ".apm" / "instructions"
        instr.mkdir(parents=True)
        (instr / "example.instructions.md").write_text(
            '---\ndescription: example\napplyTo: ["**/*.py"]\n---\nExample instruction body.\n'
        )
        config = CompilationConfig(
            target="agents",
            strategy="distributed",
            agents_md_mode="managed_section",
            with_constitution=False,
            dry_run=False,
            no_dedup=True,
        )
        compiler = AgentsCompiler(str(tmp_path))
        result = compiler.compile(config)

        assert not result.success
        joined = " ".join(result.errors)
        assert "Failed to write" in joined
        assert "AGENTS.md" in joined

    def test_distributed_managed_section_write_error_is_reported(self, tmp_path, monkeypatch):
        """Managed-section root write OSError must fail distributed stats/result."""
        from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig
        from apm_cli.compilation.output_writer import CompiledOutputWriter

        agents_md = tmp_path / "AGENTS.md"
        agents_md.write_text(f"Intro.\n{DEFAULT_START}\nOld.\n{DEFAULT_END}\nFooter.\n")
        instr = tmp_path / ".apm" / "instructions"
        instr.mkdir(parents=True)
        (instr / "example.instructions.md").write_text(
            '---\ndescription: example\napplyTo: ["**/*.py"]\n---\nExample instruction body.\n'
        )

        def fail_write(self, path, content):
            raise OSError("disk full")

        monkeypatch.setattr(CompiledOutputWriter, "write", fail_write)
        config = CompilationConfig(
            target="agents",
            strategy="distributed",
            agents_md_mode="managed_section",
            with_constitution=False,
            dry_run=False,
            no_dedup=True,
        )
        result = AgentsCompiler(str(tmp_path)).compile(config)

        assert not result.success
        assert result.stats["agents_files_generated"] == 0
        joined = " ".join(result.errors)
        assert "Failed to write" in joined
        assert "disk full" in joined

    def test_single_agents_cli_managed_section_write_error_exits_nonzero(self, monkeypatch):
        """--single-agents managed-section write OSError must exit non-zero."""
        from pathlib import Path

        from click.testing import CliRunner

        from apm_cli.cli import cli
        from apm_cli.compilation.output_writer import CompiledOutputWriter

        def fail_write(self, path, content):
            raise OSError("disk full")

        monkeypatch.setattr(CompiledOutputWriter, "write", fail_write)
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: test\nversion: 0.1.0\n"
                "compilation:\n  agents_md:\n    mode: managed_section\n"
            )
            Path("AGENTS.md").write_text(f"Intro.\n{DEFAULT_START}\nOld.\n{DEFAULT_END}\nFooter.\n")
            apm_dir = Path(".apm") / "instructions"
            apm_dir.mkdir(parents=True)
            (apm_dir / "test.instructions.md").write_text(
                "---\ndescription: Test\napplyTo: '**/*.py'\n---\nNew generated instruction body.\n"
            )
            result = runner.invoke(cli, ["compile", "--single-agents", "--target", "agents"])

        assert result.exit_code == 1
        assert "Failed to write output file" in result.output
        assert "disk full" in result.output

    def test_single_agents_cli_managed_section_routes_through_config_writer(self):
        """AC-5: --single-agents in managed_section mode preserves surrounding content."""
        from pathlib import Path

        from click.testing import CliRunner

        from apm_cli.cli import cli

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: test\nversion: 0.1.0\n"
                "compilation:\n  agents_md:\n    mode: managed_section\n"
            )
            Path("AGENTS.md").write_text(
                "# Repo guidance\n\n"
                "Hand-written intro that must survive.\n\n"
                f"{DEFAULT_START}\n"
                "Old generated block.\n"
                f"{DEFAULT_END}\n\n"
                "Hand-written footer that must survive.\n"
            )
            apm_dir = Path(".apm") / "instructions"
            apm_dir.mkdir(parents=True)
            (apm_dir / "test.instructions.md").write_text(
                "---\ndescription: Test\napplyTo: '**/*.py'\n---\nNew generated instruction body.\n"
            )
            result = runner.invoke(cli, ["compile", "--single-agents", "--target", "agents"])

            out = Path("AGENTS.md").read_text()
            assert "Hand-written intro that must survive." in out
            assert "Hand-written footer that must survive." in out
            assert "New generated instruction body." in out
            assert "Old generated block." not in out
            assert result.exit_code == 0

    def test_distributed_root_detection_normalizes_symlinks(self, tmp_path):
        """NFR-3: root detection via Path.resolve() normalizes symlinks."""
        from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig

        real_root = tmp_path / "real"
        real_root.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(real_root, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable in this environment: {exc}")
        agents_md = link / "AGENTS.md"
        agents_md.write_text(f"Intro.\n\n{DEFAULT_START}\nOld block.\n{DEFAULT_END}\n\nFooter.\n")
        config = CompilationConfig(
            agents_md_mode="managed_section", with_constitution=False, dry_run=False
        )
        compiler = AgentsCompiler(str(link))
        compiler._write_distributed_file(agents_md, "New body.", config)

        written = (real_root / "AGENTS.md").read_text()
        assert "Intro." in written
        assert "Footer." in written
        assert "New body." in written
