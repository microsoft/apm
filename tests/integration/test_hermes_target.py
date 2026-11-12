"""Integration tests for the stable explicit-only 'hermes' target.

Covers:
  1. Parser accepts Hermes without an experimental flag.
  2. --global -> skill deployed to ~/.hermes/skills/<name>/SKILL.md,
     NOT to ~/.agents/skills/<name>/SKILL.md.
  3. Flag ON + project scope -> skill deployed to <ws>/.agents/skills/<name>/SKILL.md.
  4. Parser-layer constants: hermes in VALID_TARGET_VALUES / EXPLICIT_ONLY_TARGETS,
     not in ALL_CANONICAL_TARGETS; TargetParamType accepts single + multi.
  5. compile -t hermes routes to the agents family (AGENTS.md emission).

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

    def test_parser_accepts_without_experimental_hint(
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
        assert "apm experimental enable hermes" not in normalized


# ===========================================================================
# Deploy E2E
# ===========================================================================


class TestHermesDeployE2E:
    """Flag-ON deploy tests exercising the real install pipeline."""

    def test_global_deploys_to_hermes_skills(
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

    def test_hermes_is_stable_explicit_only(self) -> None:
        from apm_cli.core.target_detection import EXPERIMENTAL_TARGETS, EXPLICIT_ONLY_TARGETS

        assert "hermes" not in EXPERIMENTAL_TARGETS
        assert "hermes" in EXPLICIT_ONLY_TARGETS

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

    def test_hermes_flag_removed(self) -> None:
        from apm_cli.core.experimental import FLAGS

        assert "hermes" not in FLAGS

    def test_hermes_compiles_agents_md(self) -> None:
        from apm_cli.core.target_detection import should_compile_agents_md

        assert should_compile_agents_md("hermes") is True


# ===========================================================================
# compile -t hermes  -- real CLI invocation emits AGENTS.md
# ===========================================================================


class TestHermesCompileE2E:
    """`apm compile -t hermes` routes through the agents family and writes AGENTS.md."""

    def test_compile_hermes_emits_agents_md(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        project = tmp_path / "project"
        instructions = project / ".apm" / "instructions"
        instructions.mkdir(parents=True)
        (project / "apm.yml").write_text(
            "name: hermes-compile-e2e\nversion: 0.1.0\n", encoding="ascii"
        )
        (instructions / "demo.instructions.md").write_text(
            "---\napplyTo: '**'\n---\n\nAlways write tests first.\n", encoding="ascii"
        )
        monkeypatch.chdir(project)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["compile", "--target", "hermes"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"Expected exit 0 from compile -t hermes, got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )
        agents_md = project / "AGENTS.md"
        assert agents_md.is_file(), (
            f"compile -t hermes did not emit AGENTS.md at {agents_md}.\nOutput:\n{result.output}"
        )
        assert "Always write tests first." in agents_md.read_text(encoding="utf-8")


# ===========================================================================
# _hermes_runtime_present -- presence gate
# ===========================================================================


class TestHermesMCPSelection:
    """Hermes MCP is explicit-only across discovery and deployment."""

    def test_home_directory_does_not_enable_auto_discovery(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import apm_cli.integration.mcp_integrator_install as mod

        (tmp_path / ".hermes").mkdir()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        monkeypatch.setattr(mod, "find_runtime_binary", lambda _name: None)
        monkeypatch.setattr(
            "apm_cli.runtime.manager.RuntimeManager.is_runtime_available",
            lambda _self, _name: False,
        )
        monkeypatch.setattr(
            "apm_cli.integration.mcp_integrator._is_vscode_available",
            lambda project_root=None: False,
        )

        assert "hermes" not in mod._discover_installed_runtimes(tmp_path, user_scope=False)

    def test_binary_does_not_enable_fallback_discovery(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import apm_cli.integration.mcp_integrator_install as mod

        monkeypatch.setattr(
            mod,
            "find_runtime_binary",
            lambda name: "/usr/bin/hermes" if name == "hermes" else None,
        )

        assert "hermes" not in mod._discover_installed_runtimes_fallback(
            tmp_path,
            lambda project_root=None: False,
            user_scope=False,
        )

    def test_explicit_target_deploys_mcp(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        assert not (fake_home / ".hermes").exists()
        monkeypatch.setattr(
            "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
            lambda _name: None,
        )
        manifest = {
            "name": "hermes-explicit-mcp",
            "version": "0.0.1",
            "dependencies": {
                "mcp": [
                    {
                        "name": "explicit-server",
                        "registry": False,
                        "transport": "stdio",
                        "command": "echo",
                        "args": ["hello"],
                    }
                ]
            },
        }
        (fake_home / ".apm" / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(
            cli,
            ["install", "--target", "hermes", "--global", "--no-policy"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, result.output
        config = yaml.safe_load((fake_home / ".hermes" / "config.yaml").read_text(encoding="utf-8"))
        assert config["mcp_servers"]["explicit-server"] == {
            "command": "echo",
            "args": ["hello"],
            "enabled": True,
        }


# ===========================================================================
# _CROSS_TARGET_MAPS -- lockfile enrichment
# ===========================================================================


class TestHermesCrossTargetMap:
    """_CROSS_TARGET_MAPS contains a hermes entry remapping github skills."""

    def test_cross_target_map_present(self) -> None:
        from apm_cli.bundle.lockfile_enrichment import _CROSS_TARGET_MAPS

        assert "hermes" in _CROSS_TARGET_MAPS

    def test_cross_target_map_remaps_github_skills(self) -> None:
        from apm_cli.bundle.lockfile_enrichment import _CROSS_TARGET_MAPS

        mapping = _CROSS_TARGET_MAPS["hermes"]
        assert ".github/skills/" in mapping
        assert mapping[".github/skills/"] == ".agents/skills/"
