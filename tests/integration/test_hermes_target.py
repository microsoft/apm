"""Integration tests for the experimental 'hermes' target.

Covers:
  1. Flag OFF  -> parser accepts hermes, enable-hint emitted, exits 0.
  2. Flag ON + --global -> skill deployed to ~/.hermes/skills/<name>/SKILL.md,
     NOT to ~/.agents/skills/<name>/SKILL.md.
  3. Flag ON + project scope -> skill deployed to <ws>/.agents/skills/<name>/SKILL.md.
  4. Parser-layer constants: hermes in VALID_TARGET_VALUES / EXPERIMENTAL_TARGETS,
     not in ALL_CANONICAL_TARGETS; TargetParamType accepts single + multi.
  5. compile -t hermes routes to the agents family (AGENTS.md emission).

Mirrors the openclaw E2E idiom (fake_home fixture patching Path.home,
apm_cli.config.CONFIG_DIR/CONFIG_FILE, and injecting _config_cache for
experimental flag control).  See tests/integration/test_openclaw_target.py.
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
_SKILL_BODY = "# Test Skill\nA skill for hermes integration tests."
_PLUGIN_ID = "test-hermes-plugin"


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
            "target": "hermes",
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


class TestHermesParserE2E:
    """CliRunner tests for 'apm install --target hermes'."""

    def test_flag_off_parser_accepts_and_emits_hint(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = fake_home / ".apm" / "config.json"
        if config_file.exists():
            config_file.unlink()

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["install", "--target", "hermes", "--global"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"Expected exit 0 from enable-hint path, got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )
        combined = result.output or ""
        assert "is not a valid target" not in combined, (
            f"Parser rejecting 'hermes' -- VALID_TARGET_VALUES may be wrong.\nOutput:\n{combined}"
        )
        normalized = " ".join(combined.split())
        assert "apm experimental enable hermes" in normalized, (
            f"Enable hint not found -- targets phase may not have run.\nOutput:\n{combined}"
        )


# ===========================================================================
# Deploy E2E
# ===========================================================================


class TestHermesDeployE2E:
    """Flag-ON deploy tests exercising the real install pipeline."""

    def test_global_deploys_to_hermes_skills(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import apm_cli.config as _conf

        monkeypatch.setattr(_conf, "_config_cache", {"experimental": {"hermes": True}})

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
            ["install", str(bundle), "--target", "hermes", "--global"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\nOutput:\n{result.output}"
        )

        expected = fake_home / ".hermes" / "skills" / _SKILL_NAME / "SKILL.md"
        assert expected.is_file(), f"Expected skill at {expected}, output={result.output!r}"

        wrong_path = fake_home / ".agents" / "skills" / _SKILL_NAME / "SKILL.md"
        assert not wrong_path.exists(), (
            f"Skill must NOT be at {wrong_path} for hermes --global, output={result.output!r}"
        )

    def test_global_deploys_to_hermes_home_override(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """$HERMES_HOME redirects the user-scope skills deploy root."""
        import apm_cli.config as _conf

        monkeypatch.setattr(_conf, "_config_cache", {"experimental": {"hermes": True}})

        custom = fake_home / "custom-hermes"
        monkeypatch.setenv("HERMES_HOME", str(custom))

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
            ["install", str(bundle), "--target", "hermes", "--global"],
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

        monkeypatch.setattr(_conf, "_config_cache", {"experimental": {"hermes": True}})

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
            ["install", str(bundle), "--target", "hermes"],
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


class TestHermesConstants:
    def test_hermes_in_valid_target_values(self) -> None:
        from apm_cli.core.target_detection import VALID_TARGET_VALUES

        assert "hermes" in VALID_TARGET_VALUES

    def test_hermes_not_in_all_canonical_targets(self) -> None:
        from apm_cli.core.target_detection import ALL_CANONICAL_TARGETS

        assert "hermes" not in ALL_CANONICAL_TARGETS

    def test_hermes_in_experimental_targets(self) -> None:
        from apm_cli.core.target_detection import EXPERIMENTAL_TARGETS

        assert "hermes" in EXPERIMENTAL_TARGETS

    def test_hermes_parser_accepts_single(self) -> None:
        from apm_cli.core.target_detection import TargetParamType

        tp = TargetParamType()
        result = tp.convert("hermes", None, None)
        assert result == "hermes"
        assert isinstance(result, str)

    def test_hermes_parser_accepts_multi(self) -> None:
        from apm_cli.core.target_detection import TargetParamType

        tp = TargetParamType()
        result = tp.convert("hermes,claude", None, None)
        assert "hermes" in result
        assert "claude" in result

    def test_hermes_flag_registered(self) -> None:
        from apm_cli.core.experimental import FLAGS

        assert "hermes" in FLAGS
        assert FLAGS["hermes"].default is False

    def test_hermes_compiles_agents_md(self) -> None:
        from apm_cli.core.target_detection import should_compile_agents_md

        assert should_compile_agents_md("hermes") is True
