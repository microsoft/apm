"""Integration tests for the stable explicit-only 'grok-bot' target.

Covers:
  1. Parser accepts grok-bot without an experimental flag.
  2. --global -> skill deployed to ~/agent-data/workflows/<name>/SKILL.md.
  3. Project scope -> skill deployed to <ws>/agent-data/workflows/<name>/SKILL.md.
  4. Parser-layer constants: grok-bot in VALID_TARGET_VALUES / EXPLICIT_ONLY_TARGETS,
     not in ALL_CANONICAL_TARGETS; TargetParamType accepts single + multi.
  5. _CROSS_TARGET_MAPS remaps .github/skills/ to agent-data/workflows/.

Uses an isolated home by patching Path.home and
apm_cli.config.CONFIG_DIR/CONFIG_FILE.
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
_SKILL_BODY = "# Test Skill\nA skill for grok-bot integration tests."
_PLUGIN_ID = "test-grok-bot-plugin"


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
            "target": "grok-bot",
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


class TestGrokBotParserE2E:
    """CliRunner tests for 'apm install --target grok-bot'."""

    def test_parser_accepts_without_experimental_hint(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = fake_home / ".apm" / "config.json"
        if config_file.exists():
            config_file.unlink()

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["install", "--target", "grok-bot", "--global"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"Expected exit 0 from enable-hint path, got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )
        combined = result.output or ""
        assert "is not a valid target" not in combined, (
            f"Parser rejecting 'grok-bot' -- VALID_TARGET_VALUES may be wrong.\nOutput:\n{combined}"
        )
        normalized = " ".join(combined.split())
        assert "apm experimental enable grok-bot" not in normalized


# ===========================================================================
# Deploy E2E
# ===========================================================================


class TestGrokBotDeployE2E:
    """Flag-ON deploy tests exercising the real install pipeline."""

    def test_global_deploys_to_agent_data_workflows(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
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
            ["install", str(bundle), "--target", "grok-bot", "--global"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\nOutput:\n{result.output}"
        )

        expected = fake_home / "agent-data" / "workflows" / _SKILL_NAME / "SKILL.md"
        assert expected.is_file(), f"Expected skill at {expected}, output={result.output!r}"

        wrong_path = fake_home / ".agents" / "skills" / _SKILL_NAME / "SKILL.md"
        assert not wrong_path.exists(), (
            f"Skill must NOT be at {wrong_path} for grok-bot --global, output={result.output!r}"
        )

    def test_project_scope_deploys_to_agent_data_workflows(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
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
            ["install", str(bundle), "--target", "grok-bot"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\nOutput:\n{result.output}"
        )
        expected = project / "agent-data" / "workflows" / _SKILL_NAME / "SKILL.md"
        assert expected.is_file(), f"Expected skill at {expected}, output={result.output!r}"


# ===========================================================================
# Parser-layer constant guards
# ===========================================================================


class TestGrokBotConstants:
    def test_grok_bot_in_valid_target_values(self) -> None:
        from apm_cli.core.target_detection import VALID_TARGET_VALUES

        assert "grok-bot" in VALID_TARGET_VALUES

    def test_grok_bot_not_in_all_canonical_targets(self) -> None:
        from apm_cli.core.target_detection import ALL_CANONICAL_TARGETS

        assert "grok-bot" not in ALL_CANONICAL_TARGETS

    def test_grok_bot_is_stable_explicit_only(self) -> None:
        from apm_cli.core.target_detection import EXPERIMENTAL_TARGETS, EXPLICIT_ONLY_TARGETS

        assert "grok-bot" not in EXPERIMENTAL_TARGETS
        assert "grok-bot" in EXPLICIT_ONLY_TARGETS

    def test_grok_bot_parser_accepts_single(self) -> None:
        from apm_cli.core.target_detection import TargetParamType

        tp = TargetParamType()
        result = tp.convert("grok-bot", None, None)
        assert result == "grok-bot"
        assert isinstance(result, str)

    def test_grok_bot_parser_accepts_multi(self) -> None:
        from apm_cli.core.target_detection import TargetParamType

        tp = TargetParamType()
        result = tp.convert("grok-bot,claude", None, None)
        assert "grok-bot" in result
        assert "claude" in result

    def test_grok_bot_flag_not_gated(self) -> None:
        from apm_cli.core.experimental import FLAGS

        assert "grok-bot" not in FLAGS


# ===========================================================================
# _CROSS_TARGET_MAPS -- lockfile enrichment
# ===========================================================================


class TestGrokBotCrossTargetMap:
    """_CROSS_TARGET_MAPS contains a grok-bot entry remapping github skills."""

    def test_cross_target_map_present(self) -> None:
        from apm_cli.bundle.lockfile_enrichment import _CROSS_TARGET_MAPS

        assert "grok-bot" in _CROSS_TARGET_MAPS

    def test_cross_target_map_remaps_github_skills(self) -> None:
        from apm_cli.bundle.lockfile_enrichment import _CROSS_TARGET_MAPS

        mapping = _CROSS_TARGET_MAPS["grok-bot"]
        assert ".github/skills/" in mapping
        assert mapping[".github/skills/"] == "agent-data/workflows/"


# ===========================================================================
# Drop-target reconcile -- ownership cleanup of agent-data/workflows
# ===========================================================================


def test_uninstall_grok_bot_cleans_agent_data_workflows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``apm uninstall`` removes ``agent-data/workflows/<name>/`` it owns.

    Modelled on the analogous ``agent-skills`` regression
    (``test_uninstall_agent_skills_cleans_dir``): the local-bundle installer
    does not mutate ``apm.yml``, so we pre-construct an ``apm.yml`` +
    ``apm.lock.yaml`` pair that advertises ownership of a grok-bot skill
    and materialise the file on disk -- mirroring the post-install state a
    real install would produce -- then assert uninstall reconciles it away.
    """
    project = tmp_path / "project"
    project.mkdir()

    pkg = "owner/test-grok-bot-plugin"
    (project / "apm.yml").write_text(
        yaml.dump(
            {
                "name": "test-project",
                "version": "1.0.0",
                "target": "grok-bot",
                "dependencies": {"apm": [f"{pkg}#main"]},
            },
            default_flow_style=False,
        ),
        encoding="utf-8",
    )

    skill_rel = f"agent-data/workflows/{_SKILL_NAME}/SKILL.md"
    deployed = project / skill_rel
    deployed.parent.mkdir(parents=True, exist_ok=True)
    deployed.write_bytes(_SKILL_BODY.encode("utf-8"))

    lock = {
        "dependencies": [
            {
                "repo_url": pkg,
                "resolved_commit": "abc123",
                "deployed_files": [skill_rel],
                "deployed_file_hashes": {skill_rel: _sha256(_SKILL_BODY)},
            }
        ],
    }
    (project / "apm.lock.yaml").write_text(
        yaml.dump(lock, default_flow_style=False), encoding="utf-8"
    )

    # Stub the modules dir so uninstall's apm_modules cleanup is a no-op.
    (project / "apm_modules").mkdir()

    monkeypatch.chdir(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["uninstall", pkg], catch_exceptions=False)

    assert result.exit_code == 0, f"output={result.output!r}"
    deployed_md = project / "agent-data" / "workflows" / _SKILL_NAME / "SKILL.md"
    assert not deployed_md.exists(), (
        f"expected {deployed_md} to be removed after uninstall (ownership "
        f"cleanup of agent-data/workflows), output={result.output!r}"
    )
