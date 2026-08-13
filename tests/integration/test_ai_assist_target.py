"""Integration tests for the experimental 'ai-assist' target.

Covers:
  1. Flag OFF  -> parser accepts ai-assist, enable-hint emitted, exits 0.
  2. Flag ON + --global -> skill deployed to ~/.ai-assist/skills/<name>/SKILL.md,
     NOT to ~/.agents/skills/<name>/SKILL.md.
  3. Flag ON + $AI_ASSIST_CONFIG_DIR -> skill deployed to custom dir.
  4. Flag ON + project scope -> skill deployed to <ws>/.agents/skills/<name>/SKILL.md.
  5. Parser-layer constants: ai-assist in VALID_TARGET_VALUES / EXPERIMENTAL_TARGETS,
     not in ALL_CANONICAL_TARGETS; TargetParamType accepts single + multi.
  6. compile -t ai-assist routes to the agents family (AGENTS.md emission).
  7. _ai_assist_runtime_opted_in double gate (flag AND presence).
  8. _CROSS_TARGET_MAPS contains an ai-assist entry.

Mirrors the hermes E2E idiom (fake_home fixture patching Path.home,
apm_cli.config.CONFIG_DIR/CONFIG_FILE, and injecting _config_cache for
experimental flag control).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli

_MINIMAL_APM_YML = "name: test\ndescription: test\nversion: 0.0.1\n"
_BASE_ENV: dict[str, str] = {"APM_E2E_TESTS": "1"}


def _write_minimal_apm_yml(apm_dir: Path) -> None:
    (apm_dir / "apm.yml").write_text(_MINIMAL_APM_YML, encoding="ascii")


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated home directory wired into every APM config lookup."""
    home = tmp_path / "home"
    apm_dir = home / ".apm"
    apm_dir.mkdir(parents=True)
    _write_minimal_apm_yml(apm_dir)

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    import apm_cli.config as _conf

    monkeypatch.setattr(_conf, "CONFIG_DIR", str(apm_dir))
    monkeypatch.setattr(_conf, "CONFIG_FILE", str(apm_dir / "config.json"))
    monkeypatch.setattr(_conf, "_config_cache", None)
    yield home
    monkeypatch.setattr(_conf, "_config_cache", None)


# ---------------------------------------------------------------------------
# Bundle helpers
# ---------------------------------------------------------------------------

_SKILL_NAME = "test-skill"
_SKILL_BODY = "# Test Skill\nA skill for ai-assist integration tests."
_PLUGIN_ID = "test-ai-assist-plugin"


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _make_plugin_bundle(tmp_path: Path) -> Path:
    """Build a minimal plugin-format bundle with one skill."""
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)

    (bundle / "plugin.json").write_text(
        json.dumps({"id": _PLUGIN_ID, "name": "Test Plugin"}), encoding="utf-8"
    )

    rel = f"skills/{_SKILL_NAME}/SKILL.md"
    skill_path = bundle / rel
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(_SKILL_BODY, encoding="utf-8")

    bundle_files = {rel: _sha256(_SKILL_BODY)}
    lock_data = {
        "pack": {
            "format": "plugin",
            "target": "ai-assist",
            "bundle_files": bundle_files,
        },
        "dependencies": [
            {
                "repo_url": f"owner/{_PLUGIN_ID}",
                "resolved_commit": "abc123",
                "deployed_files": [rel],
                "deployed_file_hashes": bundle_files,
            }
        ],
    }
    (bundle / "apm.lock.yaml").write_text(
        yaml.dump(lock_data, default_flow_style=False), encoding="utf-8"
    )
    return bundle


# ===========================================================================
# Parser E2E
# ===========================================================================


class TestAiAssistParserE2E:
    """CliRunner tests for 'apm install --target ai-assist'."""

    def test_flag_off_parser_accepts_and_emits_hint(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = fake_home / ".apm" / "config.json"
        if config_file.exists():
            config_file.unlink()

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["install", "--target", "ai-assist", "--global"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"Expected exit 0 from enable-hint path, got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )
        combined = result.output or ""
        assert "is not a valid target" not in combined, (
            f"Parser rejecting 'ai-assist' -- VALID_TARGET_VALUES may be wrong.\n"
            f"Output:\n{combined}"
        )
        normalized = " ".join(combined.split())
        assert "apm experimental enable ai_assist" in normalized or "apm experimental enable ai-assist" in normalized, (
            f"Enable hint not found -- targets phase may not have run.\nOutput:\n{combined}"
        )


# ===========================================================================
# Deploy E2E
# ===========================================================================


class TestAiAssistDeployE2E:
    """Flag-ON deploy tests exercising the real install pipeline."""

    def test_global_deploys_to_ai_assist_skills(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import apm_cli.config as _conf

        monkeypatch.setattr(_conf, "_config_cache", {"experimental": {"ai_assist": True}})

        user_apm = fake_home / ".apm"
        user_apm.mkdir(parents=True, exist_ok=True)
        _write_minimal_apm_yml(user_apm)

        bundle = _make_plugin_bundle(tmp_path / "src")

        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["install", str(bundle), "--target", "ai-assist", "--global"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\nOutput:\n{result.output}"
        )

        expected = fake_home / ".ai-assist" / "skills" / _SKILL_NAME / "SKILL.md"
        assert expected.is_file(), f"Expected skill at {expected}, output={result.output!r}"

        wrong_path = fake_home / ".agents" / "skills" / _SKILL_NAME / "SKILL.md"
        assert not wrong_path.exists(), (
            f"Skill must NOT be at {wrong_path} for ai-assist --global, output={result.output!r}"
        )

    def test_global_deploys_to_config_dir_override(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """$AI_ASSIST_CONFIG_DIR redirects the user-scope skills deploy root."""
        import apm_cli.config as _conf

        monkeypatch.setattr(_conf, "_config_cache", {"experimental": {"ai_assist": True}})

        custom = fake_home / "custom-ai-assist"
        monkeypatch.setenv("AI_ASSIST_CONFIG_DIR", str(custom))

        user_apm = fake_home / ".apm"
        user_apm.mkdir(parents=True, exist_ok=True)
        _write_minimal_apm_yml(user_apm)

        bundle = _make_plugin_bundle(tmp_path / "src")
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["install", str(bundle), "--target", "ai-assist", "--global"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\nOutput:\n{result.output}"
        )
        expected = custom / "skills" / _SKILL_NAME / "SKILL.md"
        assert expected.is_file(), f"Expected skill at {expected}, output={result.output!r}"

    def test_project_scope_deploys_to_agents_skills(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import apm_cli.config as _conf

        monkeypatch.setattr(_conf, "_config_cache", {"experimental": {"ai_assist": True}})

        bundle = _make_plugin_bundle(tmp_path / "src")

        project = tmp_path / "project"
        project.mkdir()
        (project / "apm.yml").write_text(
            yaml.dump(
                {
                    "name": "test-project",
                    "version": "1.0.0",
                    "dependencies": {"apm": []},
                },
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
        (project / ".github").mkdir()
        monkeypatch.chdir(project)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["install", str(bundle), "--target", "ai-assist"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\nOutput:\n{result.output}"
        )
        expected = project / ".agents" / "skills" / _SKILL_NAME / "SKILL.md"
        assert expected.is_file(), f"Expected skill at {expected}, output={result.output!r}"


# ===========================================================================
# Parser-layer constant guards
# ===========================================================================


class TestAiAssistConstants:
    def test_ai_assist_in_valid_target_values(self) -> None:
        from apm_cli.core.target_detection import VALID_TARGET_VALUES

        assert "ai-assist" in VALID_TARGET_VALUES

    def test_ai_assist_not_in_all_canonical_targets(self) -> None:
        from apm_cli.core.target_detection import ALL_CANONICAL_TARGETS

        assert "ai-assist" not in ALL_CANONICAL_TARGETS

    def test_ai_assist_in_experimental_targets(self) -> None:
        from apm_cli.core.target_detection import EXPERIMENTAL_TARGETS

        assert "ai-assist" in EXPERIMENTAL_TARGETS

    def test_ai_assist_parser_accepts_single(self) -> None:
        from apm_cli.core.target_detection import TargetParamType

        tp = TargetParamType()
        result = tp.convert("ai-assist", None, None)
        assert result == "ai-assist"
        assert isinstance(result, str)

    def test_ai_assist_parser_accepts_multi(self) -> None:
        from apm_cli.core.target_detection import TargetParamType

        tp = TargetParamType()
        result = tp.convert("ai-assist,claude", None, None)
        assert "ai-assist" in result
        assert "claude" in result

    def test_ai_assist_flag_registered(self) -> None:
        from apm_cli.core.experimental import FLAGS

        assert "ai_assist" in FLAGS
        assert FLAGS["ai_assist"].default is False

    def test_ai_assist_compiles_agents_md(self) -> None:
        from apm_cli.core.target_detection import should_compile_agents_md

        assert should_compile_agents_md("ai-assist") is True


# ===========================================================================
# compile -t ai-assist  -- real CLI invocation emits AGENTS.md
# ===========================================================================


class TestAiAssistCompileE2E:
    """`apm compile -t ai-assist` routes through the agents family and writes AGENTS.md."""

    def test_compile_ai_assist_emits_agents_md(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import apm_cli.config as _conf

        monkeypatch.setattr(_conf, "_config_cache", {"experimental": {"ai_assist": True}})

        project = tmp_path / "project"
        instructions = project / ".apm" / "instructions"
        instructions.mkdir(parents=True)
        (project / "apm.yml").write_text(
            "name: ai-assist-compile-e2e\nversion: 0.1.0\n", encoding="ascii"
        )
        (instructions / "demo.instructions.md").write_text(
            "---\napplyTo: '**'\n---\n\nAlways write tests first.\n", encoding="ascii"
        )
        monkeypatch.chdir(project)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["compile", "--target", "ai-assist"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"Expected exit 0 from compile -t ai-assist, got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )
        agents_md = project / "AGENTS.md"
        assert agents_md.is_file(), (
            f"compile -t ai-assist did not emit AGENTS.md at {agents_md}.\n"
            f"Output:\n{result.output}"
        )
        assert "Always write tests first." in agents_md.read_text(encoding="utf-8")


# ===========================================================================
# _ai_assist_runtime_opted_in -- double gate (flag AND presence)
# ===========================================================================


class TestAiAssistMCPOptIn:
    """MCP writes to config dir require BOTH the flag AND ai-assist presence."""

    def test_flag_off_skips_regardless_of_presence(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import apm_cli.integration.mcp_integrator_install as mod

        present_home = tmp_path / ".ai-assist"
        present_home.mkdir()
        monkeypatch.setattr("apm_cli.core.experimental.is_enabled", lambda _flag: False)
        monkeypatch.setattr(
            "apm_cli.integration.targets.resolve_ai_assist_root", lambda: present_home
        )
        assert mod._ai_assist_runtime_opted_in() is False

    def test_flag_on_no_presence_skips_mcp_write(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import apm_cli.integration.mcp_integrator_install as mod

        absent_home = tmp_path / "does-not-exist" / ".ai-assist"
        monkeypatch.setattr("apm_cli.core.experimental.is_enabled", lambda _flag: True)
        monkeypatch.setattr(
            "apm_cli.integration.targets.resolve_ai_assist_root", lambda: absent_home
        )
        monkeypatch.setattr(mod, "find_runtime_binary", lambda _name: None)
        assert mod._ai_assist_runtime_opted_in() is False

    def test_flag_on_with_config_dir_opts_in(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import apm_cli.integration.mcp_integrator_install as mod

        present_home = tmp_path / ".ai-assist"
        present_home.mkdir()
        monkeypatch.setattr("apm_cli.core.experimental.is_enabled", lambda _flag: True)
        monkeypatch.setattr(
            "apm_cli.integration.targets.resolve_ai_assist_root", lambda: present_home
        )
        monkeypatch.setattr(mod, "find_runtime_binary", lambda _name: None)
        assert mod._ai_assist_runtime_opted_in() is True

    def test_flag_on_with_binary_only_opts_in(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import apm_cli.integration.mcp_integrator_install as mod

        absent_home = tmp_path / "does-not-exist" / ".ai-assist"
        monkeypatch.setattr("apm_cli.core.experimental.is_enabled", lambda _flag: True)
        monkeypatch.setattr(
            "apm_cli.integration.targets.resolve_ai_assist_root", lambda: absent_home
        )
        monkeypatch.setattr(mod, "find_runtime_binary", lambda _name: "/usr/bin/ai-assist")
        assert mod._ai_assist_runtime_opted_in() is True


# ===========================================================================
# _CROSS_TARGET_MAPS -- lockfile enrichment
# ===========================================================================


class TestAiAssistCrossTargetMap:
    """_CROSS_TARGET_MAPS contains an ai-assist entry remapping github skills."""

    def test_cross_target_map_present(self) -> None:
        from apm_cli.bundle.lockfile_enrichment import _CROSS_TARGET_MAPS

        assert "ai-assist" in _CROSS_TARGET_MAPS

    def test_cross_target_map_remaps_github_skills(self) -> None:
        from apm_cli.bundle.lockfile_enrichment import _CROSS_TARGET_MAPS

        mapping = _CROSS_TARGET_MAPS["ai-assist"]
        assert ".github/skills/" in mapping
        assert mapping[".github/skills/"] == ".agents/skills/"
