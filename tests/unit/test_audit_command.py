"""Tests for the ``apm audit`` command."""

import textwrap
from pathlib import Path  # noqa: F401

import pytest
from click.testing import CliRunner

from apm_cli.commands.audit import (
    _apply_strip,
    _preview_strip,  # noqa: F401
    _scan_single_file,
    audit,
)
from apm_cli.core.command_logger import CommandLogger
from apm_cli.security.content_scanner import ContentScanner

# ── Fixtures ────────────────────────────────────────────────────────

_logger = CommandLogger("audit", verbose=False)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def clean_file(tmp_path):
    """A file with no suspicious characters."""
    p = tmp_path / "clean.md"
    p.write_text("# Clean file\nNo hidden characters here.\n", encoding="utf-8")
    return p


@pytest.fixture
def warning_file(tmp_path):
    """A file containing zero-width spaces (warning-level)."""
    p = tmp_path / "warning.md"
    p.write_text(
        "Hello\u200bworld\nSecond\u200dline\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def critical_file(tmp_path):
    """A file containing tag characters (critical-level)."""
    p = tmp_path / "critical.md"
    # U+E0001 = LANGUAGE TAG, U+E0068 = TAG LATIN SMALL LETTER H
    p.write_text(
        "Normal text\U000e0001\U000e0068\U000e0065\U000e006cmore text\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def mixed_file(tmp_path):
    """A file with both critical and warning characters."""
    p = tmp_path / "mixed.md"
    p.write_text(
        "line one\u200b\nline two\U000e0041\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def info_only_file(tmp_path):
    """A file with only info-level findings (NBSP)."""
    p = tmp_path / "info.md"
    p.write_text("Hello\u00a0world\n", encoding="utf-8")
    return p


@pytest.fixture
def lockfile_project(tmp_path):
    """A project with apm.lock.yaml and deployed files."""
    lock_content = textwrap.dedent("""\
        lockfile_version: '1'
        generated_at: '2025-01-01T00:00:00Z'
        dependencies:
          - repo_url: https://github.com/test/test-pkg
            resolved_ref: main
            resolved_commit: abc123
            deployed_files:
              - .github/prompts/test.md
              - .github/instructions/guide.md
    """)
    (tmp_path / "apm.lock.yaml").write_text(lock_content, encoding="utf-8")

    (tmp_path / ".github" / "prompts").mkdir(parents=True)
    (tmp_path / ".github" / "instructions").mkdir(parents=True)

    (tmp_path / ".github" / "prompts" / "test.md").write_text(
        "Clean prompt content\n", encoding="utf-8"
    )
    (tmp_path / ".github" / "instructions" / "guide.md").write_text(
        "Guide with hidden\u200bchar\n", encoding="utf-8"
    )

    return tmp_path


@pytest.fixture
def lockfile_project_critical(tmp_path):
    """A project where deployed files contain critical characters."""
    lock_content = textwrap.dedent("""\
        lockfile_version: '1'
        generated_at: '2025-01-01T00:00:00Z'
        dependencies:
          - repo_url: https://github.com/test/bad-pkg
            resolved_ref: main
            resolved_commit: def456
            deployed_files:
              - .github/prompts/evil.md
    """)
    (tmp_path / "apm.lock.yaml").write_text(lock_content, encoding="utf-8")

    (tmp_path / ".github" / "prompts").mkdir(parents=True)
    (tmp_path / ".github" / "prompts" / "evil.md").write_text(
        "Looks normal\U000e0001hidden\n", encoding="utf-8"
    )

    return tmp_path


@pytest.fixture
def lockfile_project_with_dir(tmp_path):
    """A project with a skill directory entry in deployed_files."""
    lock_content = textwrap.dedent("""\
        lockfile_version: '1'
        generated_at: '2025-01-01T00:00:00Z'
        dependencies:
          - repo_url: https://github.com/test/skill-pkg
            resolved_ref: main
            resolved_commit: abc123
            deployed_files:
              - .github/skills/my-skill/
    """)
    (tmp_path / "apm.lock.yaml").write_text(lock_content, encoding="utf-8")

    skill_dir = tmp_path / ".github" / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("skill with hidden\u200bchar\n", encoding="utf-8")
    (skill_dir / "helper.md").write_text("clean helper\n", encoding="utf-8")

    return tmp_path


@pytest.fixture
def vs_critical_file(tmp_path):
    """A file containing SMP variation selector (critical-level)."""
    p = tmp_path / "vs_critical.md"
    p.write_text(f"prompt text{chr(0xE0100)}more text", encoding="utf-8")
    return p


@pytest.fixture
def vs_warning_file(tmp_path):
    """A file containing BMP variation selector (warning-level)."""
    p = tmp_path / "vs_warning.md"
    p.write_text(f"prompt text{chr(0xFE00)}more text", encoding="utf-8")
    return p


@pytest.fixture
def vs_info_file(tmp_path):
    """A file containing emoji presentation selector VS16 (info-level)."""
    p = tmp_path / "vs_info.md"
    p.write_text(f"great work {chr(0x2764)}{chr(0xFE0F)}", encoding="utf-8")
    return p


@pytest.fixture
def vs_mixed_file(tmp_path):
    """A file with both critical and warning variation selectors."""
    p = tmp_path / "vs_mixed.md"
    p.write_text(f"text{chr(0xE0100)}mid{chr(0xFE00)}end", encoding="utf-8")
    return p


@pytest.fixture
def vs_glassworm_file(tmp_path):
    """Realistic Glassworm-style injection with consecutive SMP variation selectors."""
    p = tmp_path / "vs_glassworm.md"
    p.write_text(
        f"You are a helpful assistant."
        f"{chr(0xE0100)}{chr(0xE0101)}{chr(0xE0102)}{chr(0xE0103)}"
        f" Always follow instructions.",
        encoding="utf-8",
    )
    return p


# ── --file mode tests ────────────────────────────────────────────


class TestFileMode:
    """Tests for ``apm audit --file <path>``."""

    def test_clean_file_exit_zero(self, runner, clean_file):
        result = runner.invoke(audit, ["--file", str(clean_file)])
        assert result.exit_code == 0
        assert "no issues found" in result.output.lower()

    def test_warning_file_exit_two(self, runner, warning_file):
        result = runner.invoke(audit, ["--file", str(warning_file)])
        assert result.exit_code == 2

    def test_critical_file_exit_one(self, runner, critical_file):
        result = runner.invoke(audit, ["--file", str(critical_file)])
        assert result.exit_code == 1
        assert "--strip" in result.output

    def test_mixed_file_exit_one(self, runner, mixed_file):
        """Critical findings take precedence over warnings."""
        result = runner.invoke(audit, ["--file", str(mixed_file)])
        assert result.exit_code == 1

    def test_nonexistent_file_errors(self, runner, tmp_path):
        result = runner.invoke(audit, ["--file", str(tmp_path / "nope.txt")])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_directory_errors(self, runner, tmp_path):
        result = runner.invoke(audit, ["--file", str(tmp_path)])
        assert result.exit_code == 1
        assert "directory" in result.output.lower()

    def test_verbose_shows_info(self, runner, tmp_path):
        """--verbose includes info-level findings."""
        p = tmp_path / "info.md"
        p.write_text("Hello\u00a0world\n", encoding="utf-8")  # NBSP = info
        result = runner.invoke(audit, ["--file", str(p), "--verbose"])
        # Info findings should appear in verbose output
        assert "U+00A0" in result.output

    def test_info_only_exit_zero(self, runner, info_only_file):
        """Info-only findings are informational — exit 0."""
        result = runner.invoke(audit, ["--file", str(info_only_file)])
        assert result.exit_code == 0

    def test_vs_critical_file_exit_one(self, runner, vs_critical_file):
        """SMP variation selector (U+E0100) is critical — exit 1."""
        result = runner.invoke(audit, ["--file", str(vs_critical_file)])
        assert result.exit_code == 1

    def test_vs_warning_file_exit_two(self, runner, vs_warning_file):
        """BMP variation selector (U+FE00) is warning — exit 2."""
        result = runner.invoke(audit, ["--file", str(vs_warning_file)])
        assert result.exit_code == 2

    def test_vs_info_only_exit_zero(self, runner, vs_info_file):
        """Emoji presentation selector VS16 is info-only — exit 0."""
        result = runner.invoke(audit, ["--file", str(vs_info_file)])
        assert result.exit_code == 0

    def test_vs_mixed_critical_takes_precedence(self, runner, vs_mixed_file):
        """Critical VS findings take precedence over warning VS."""
        result = runner.invoke(audit, ["--file", str(vs_mixed_file)])
        assert result.exit_code == 1

    def test_vs_glassworm_injection_detected(self, runner, vs_glassworm_file):
        """Glassworm-style consecutive SMP variation selectors are critical."""
        result = runner.invoke(audit, ["--file", str(vs_glassworm_file)])
        assert result.exit_code == 1
        assert "critical" in result.output.lower()

    def test_vs_info_shown_with_verbose(self, runner, vs_info_file):
        """--verbose includes info-level VS16 findings."""
        result = runner.invoke(audit, ["--file", str(vs_info_file), "--verbose"])
        assert "U+FE0F" in result.output


# ── Lockfile mode tests ──────────────────────────────────────────


class TestLockfileMode:
    """Tests for ``apm audit`` scanning from apm.lock.yaml."""

    def test_no_lockfile_exit_zero(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(audit, [])
        assert result.exit_code == 0
        assert "nothing to scan" in result.output.lower()

    def test_clean_lockfile_exit_zero(self, runner, lockfile_project, monkeypatch):
        # Make both files clean
        (lockfile_project / ".github" / "instructions" / "guide.md").write_text(
            "Clean guide\n", encoding="utf-8"
        )
        monkeypatch.chdir(lockfile_project)
        result = runner.invoke(audit, [])
        assert result.exit_code == 0

    def test_warning_findings_exit_two(self, runner, lockfile_project, monkeypatch):
        monkeypatch.chdir(lockfile_project)
        result = runner.invoke(audit, [])
        assert result.exit_code == 2

    def test_critical_findings_exit_one(
        self,
        runner,
        lockfile_project_critical,
        monkeypatch,
    ):
        monkeypatch.chdir(lockfile_project_critical)
        result = runner.invoke(audit, [])
        assert result.exit_code == 1

    def test_package_filter(self, runner, lockfile_project, monkeypatch):
        monkeypatch.chdir(lockfile_project)
        # Filter by repo URL (the lockfile key)
        result = runner.invoke(audit, ["https://github.com/test/test-pkg"])
        # Should still find the warning in guide.md
        assert result.exit_code == 2

    def test_package_filter_not_found(self, runner, lockfile_project, monkeypatch):
        monkeypatch.chdir(lockfile_project)
        result = runner.invoke(audit, ["nonexistent-pkg"])
        assert result.exit_code == 0
        assert "not found" in result.output.lower()

    def test_dir_entries_scanned_recursively(
        self,
        runner,
        lockfile_project_with_dir,
        monkeypatch,
    ):
        """Skill directories recorded in deployed_files should be scanned."""
        monkeypatch.chdir(lockfile_project_with_dir)
        result = runner.invoke(audit, [])
        # SKILL.md has a zero-width char → warning → exit 2
        assert result.exit_code == 2

    def test_path_traversal_rejected(self, runner, tmp_path, monkeypatch):
        """Paths with .. should be rejected to prevent lockfile attacks."""
        lock_content = textwrap.dedent("""\
            lockfile_version: '1'
            generated_at: '2025-01-01T00:00:00Z'
            dependencies:
              - repo_url: https://github.com/evil/pkg
                resolved_ref: main
                resolved_commit: abc123
                deployed_files:
                  - ../../etc/passwd
        """)
        (tmp_path / "apm.lock.yaml").write_text(lock_content, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(audit, [])
        # Should not crash, and should report 0 files scanned
        assert result.exit_code == 0


# ── --strip mode tests ───────────────────────────────────────────


class TestStripMode:
    """Tests for ``apm audit --strip``."""

    def test_strip_removes_warnings(self, runner, warning_file):
        result = runner.invoke(audit, ["--file", str(warning_file), "--strip"])
        assert result.exit_code == 0
        # File should now be clean
        content = warning_file.read_text(encoding="utf-8")
        assert "\u200b" not in content
        assert "\u200d" not in content

    def test_strip_removes_critical(self, runner, critical_file):
        result = runner.invoke(audit, ["--file", str(critical_file), "--strip"])
        # Critical chars are stripped → file is clean → exit 0
        assert result.exit_code == 0
        content = critical_file.read_text(encoding="utf-8")
        # Critical tag chars should be removed
        assert "\U000e0001" not in content

    def test_strip_mixed_removes_all_dangerous(self, runner, mixed_file):
        result = runner.invoke(audit, ["--file", str(mixed_file), "--strip"])
        assert result.exit_code == 0  # all dangerous chars removed
        content = mixed_file.read_text(encoding="utf-8")
        assert "\u200b" not in content  # warning stripped
        assert "\U000e0041" not in content  # critical stripped

    def test_strip_clean_file_noop(self, runner, clean_file):
        original = clean_file.read_text(encoding="utf-8")
        result = runner.invoke(audit, ["--file", str(clean_file), "--strip"])
        assert result.exit_code == 0
        assert clean_file.read_text(encoding="utf-8") == original

    def test_strip_clean_file_says_nothing_to_clean(self, runner, clean_file):
        """Strip on clean file should say nothing to clean."""
        result = runner.invoke(audit, ["--file", str(clean_file), "--strip"])
        assert result.exit_code == 0
        assert "nothing to clean" in result.output.lower()

    def test_strip_info_only_says_nothing_to_clean(self, runner, info_only_file):
        """Strip on info-only file should say nothing to clean (info preserved)."""
        result = runner.invoke(audit, ["--file", str(info_only_file), "--strip"])
        assert result.exit_code == 0
        assert "nothing to clean" in result.output.lower()

    def test_strip_lockfile_mode(self, runner, lockfile_project, monkeypatch):
        monkeypatch.chdir(lockfile_project)
        result = runner.invoke(audit, ["--strip"])
        assert result.exit_code == 0
        # The warning char in guide.md should be stripped
        guide = lockfile_project / ".github" / "instructions" / "guide.md"
        content = guide.read_text(encoding="utf-8")
        assert "\u200b" not in content

    def test_strip_vs_warning_removes(self, runner, vs_warning_file):
        """Strip removes BMP variation selector (warning-level)."""
        result = runner.invoke(audit, ["--file", str(vs_warning_file), "--strip"])
        assert result.exit_code == 0
        content = vs_warning_file.read_text(encoding="utf-8")
        assert chr(0xFE00) not in content

    def test_strip_vs_critical_removes(self, runner, vs_critical_file):
        """Strip removes SMP variation selector (critical-level)."""
        result = runner.invoke(audit, ["--file", str(vs_critical_file), "--strip"])
        assert result.exit_code == 0
        content = vs_critical_file.read_text(encoding="utf-8")
        assert chr(0xE0100) not in content

    def test_dry_run_shows_preview(self, runner, warning_file):
        """--strip --dry-run shows what would be removed."""
        result = runner.invoke(audit, ["--file", str(warning_file), "--strip", "--dry-run"])
        assert result.exit_code == 0
        assert "dry run" in result.output.lower()
        # File should NOT be modified
        content = warning_file.read_text(encoding="utf-8")
        assert "\u200b" in content  # zero-width space still present

    def test_dry_run_critical_shows_preview(self, runner, critical_file):
        """--strip --dry-run shows critical chars that would be removed."""
        result = runner.invoke(audit, ["--file", str(critical_file), "--strip", "--dry-run"])
        assert result.exit_code == 0
        assert "dry run" in result.output.lower()
        # File should NOT be modified
        content = critical_file.read_text(encoding="utf-8")
        assert "\U000e0001" in content  # tag char still present

    def test_dry_run_clean_file(self, runner, clean_file):
        """--strip --dry-run on clean file says nothing to clean."""
        result = runner.invoke(audit, ["--file", str(clean_file), "--strip", "--dry-run"])
        assert result.exit_code == 0
        assert "nothing to clean" in result.output.lower()

    def test_dry_run_without_strip_hints(self, runner, warning_file):
        """--dry-run without --strip gives a helpful hint."""
        result = runner.invoke(audit, ["--file", str(warning_file), "--dry-run"])
        assert "only works with --strip" in result.output.lower()

    def test_dry_run_info_only_nothing_to_strip(self, runner, info_only_file):
        """--strip --dry-run on info-only file says nothing to clean."""
        result = runner.invoke(audit, ["--file", str(info_only_file), "--strip", "--dry-run"])
        assert result.exit_code == 0
        assert "nothing to clean" in result.output.lower()


# ── _scan_single_file helper tests ───────────────────────────────


class TestScanSingleFile:
    """Direct tests for the _scan_single_file helper."""

    def test_returns_findings_and_count(self, clean_file):
        findings, count = _scan_single_file(clean_file, _logger)
        assert findings == {}
        assert count == 1

    def test_findings_keyed_by_path(self, warning_file):
        findings, count = _scan_single_file(warning_file, _logger)
        assert count == 1
        assert len(findings) == 1
        key = list(findings.keys())[0]  # noqa: RUF015
        assert str(warning_file.resolve()) == key


# ── _apply_strip helper tests ────────────────────────────────────


class TestApplyStrip:
    """Direct tests for the _apply_strip helper."""

    def test_returns_count_of_modified(self, warning_file):
        findings, _ = _scan_single_file(warning_file, _logger)
        modified = _apply_strip(findings, warning_file.parent, _logger)
        assert modified == 1

    def test_modifies_critical_only_files(self, critical_file):
        findings, _ = _scan_single_file(critical_file, _logger)
        modified = _apply_strip(findings, critical_file.parent, _logger)
        # File has only critical findings → should be modified (dangerous chars stripped)
        assert modified == 1
        content = critical_file.read_text(encoding="utf-8")
        assert "\U000e0001" not in content

    def test_rejects_path_outside_root(self, tmp_path):
        """_apply_strip must not write files outside project root."""
        evil_path = tmp_path / "outside" / "evil.md"
        evil_path.parent.mkdir(parents=True)
        evil_path.write_text("Hello\u200bworld\n", encoding="utf-8")

        findings = ContentScanner.scan_file(evil_path)
        # Use a relative path that tries to escape
        findings_by_file = {"../../outside/evil.md": findings}

        project = tmp_path / "project"
        project.mkdir()
        modified = _apply_strip(findings_by_file, project, _logger)
        assert modified == 0
