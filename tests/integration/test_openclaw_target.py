"""Integration tests for 'apm install --target openclaw'.

Covers:
  1. Flag OFF  -> parser accepts openclaw, enable-hint emitted, exits 0.
  2. Flag ON + --global -> skill deployed to ~/.openclaw/skills/<name>/SKILL.md,
     NOT to ~/.agents/skills/<name>/SKILL.md.
  3. Flag ON + project scope -> skill deployed to <ws>/.agents/skills/<name>/SKILL.md.
  4. Parser accepts 'openclaw,claude' as a multi-target list.
  5. FLAGS registry contains the 'openclaw' entry.
  6. openclaw in EXPERIMENTAL_TARGETS, not ALL_CANONICAL_TARGETS.

Uses the copilot-cowork E2E test idiom: fake_home fixture that patches
Path.home, apm_cli.config.CONFIG_DIR, CONFIG_FILE, and injects _config_cache
for experimental flag control.  See
tests/unit/install/test_install_target_copilot_cowork_e2e.py for the origin.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_MINIMAL_APM_YML = "name: test\ndescription: test\nversion: 0.0.1\n"

_BASE_ENV: dict[str, str] = {"APM_E2E_TESTS": "1"}

_SKILL_YML = """\
name: greet
description: A greeting skill
version: 0.0.1
dependencies: []
targets:
  - openclaw
skills:
  - name: greet
    path: skills/greet
"""

_SKILL_MD = """\
---
name: greet
description: Says hello
---

# greet

Hello, world!
"""


def _write_minimal_apm_yml(apm_dir: Path) -> None:
    (apm_dir / "apm.yml").write_text(_MINIMAL_APM_YML, encoding="ascii")


def _write_config_json(apm_dir: Path, cfg: dict[str, Any]) -> None:
    (apm_dir / "config.json").write_text(json.dumps(cfg), encoding="ascii")


def _write_skill_package(ws: Path) -> None:
    """Write a minimal skill package into *ws* for install to deploy."""
    apm_dir = ws / ".apm"
    apm_dir.mkdir(parents=True, exist_ok=True)
    (apm_dir / "apm.yml").write_text(_SKILL_YML, encoding="ascii")
    skill_dir = apm_dir / "skills" / "greet"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(_SKILL_MD, encoding="ascii")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated home directory wired into every APM config lookup.

    Mirrors the copilot-cowork E2E fake_home fixture.
    """
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


# ===========================================================================
# TestOpenclawParserE2E -- core regression tests
# ===========================================================================


class TestOpenclawParserE2E:
    """CliRunner tests for 'apm install --target openclaw'."""

    # ------------------------------------------------------------------ #
    # Case 1: Flag OFF -> parser accepts, enable-hint emitted, exit 0    #
    # ------------------------------------------------------------------ #

    def test_flag_off_parser_accepts_and_emits_hint(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--target openclaw with flag OFF: not rejected by parser,
        exits 0, emits 'apm experimental enable openclaw'.
        """
        config_file = fake_home / ".apm" / "config.json"
        if config_file.exists():
            config_file.unlink()

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["install", "--target", "openclaw", "--global"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"Expected exit 0 from enable-hint path, got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )

        combined = result.output or ""

        assert "is not a valid target" not in combined, (
            "Parser still rejecting 'openclaw' -- VALID_TARGET_VALUES may be wrong.\n"
            f"Output:\n{combined}"
        )

        normalized = " ".join(combined.split())
        assert "apm experimental enable openclaw" in normalized, (
            "Enable hint not found in output -- targets phase may not have run.\n"
            f"Output:\n{combined}"
        )


# ===========================================================================
# TestOpenclawDeployE2E -- flag-ON deploy path tests
# ===========================================================================

# Bundle helpers (mirrors test_agent_skills_target.py pattern)

_SKILL_NAME = "test-skill"
_SKILL_BODY = "# Test Skill\nA skill for openclaw integration tests."
_PLUGIN_ID = "test-openclaw-plugin"


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
            "target": "openclaw",
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


class TestOpenclawDeployE2E:
    """Flag-ON deploy tests exercising the real install pipeline."""

    def test_global_deploys_to_openclaw_skills(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--target openclaw --global with flag ON deploys to ~/.openclaw/skills/."""
        import apm_cli.config as _conf

        monkeypatch.setattr(_conf, "_config_cache", {"experimental": {"openclaw": True}})

        # User-scope manifest required for -g install.
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
            ["install", str(bundle), "--target", "openclaw", "--global"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\nOutput:\n{result.output}"
        )

        # Skill must land under ~/.openclaw/skills/
        expected = fake_home / ".openclaw" / "skills" / _SKILL_NAME / "SKILL.md"
        assert expected.is_file(), f"Expected skill at {expected}, output={result.output!r}"

        # Must NOT land under ~/.agents/skills/ (negative assertion)
        wrong_path = fake_home / ".agents" / "skills" / _SKILL_NAME / "SKILL.md"
        assert not wrong_path.exists(), (
            f"Skill must NOT be at {wrong_path} for openclaw --global, output={result.output!r}"
        )

    def test_project_scope_deploys_to_agents_skills(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--target openclaw (project scope) deploys to .agents/skills/."""
        import apm_cli.config as _conf

        monkeypatch.setattr(_conf, "_config_cache", {"experimental": {"openclaw": True}})

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
            ["install", str(bundle), "--target", "openclaw"],
            env={**_BASE_ENV},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\nOutput:\n{result.output}"
        )

        expected = project / ".agents" / "skills" / _SKILL_NAME / "SKILL.md"
        assert expected.is_file(), f"Expected skill at {expected}, output={result.output!r}"


# ===========================================================================
# TestOpenclawConstants -- parser-layer constant guards
# ===========================================================================


class TestOpenclawConstants:
    """Guard constants for the openclaw target at the parser layer."""

    def test_openclaw_in_valid_target_values(self) -> None:
        from apm_cli.core.target_detection import VALID_TARGET_VALUES

        assert "openclaw" in VALID_TARGET_VALUES

    def test_openclaw_not_in_all_canonical_targets(self) -> None:
        from apm_cli.core.target_detection import ALL_CANONICAL_TARGETS

        assert "openclaw" not in ALL_CANONICAL_TARGETS

    def test_openclaw_in_experimental_targets(self) -> None:
        from apm_cli.core.target_detection import EXPERIMENTAL_TARGETS

        assert "openclaw" in EXPERIMENTAL_TARGETS

    def test_openclaw_parser_accepts_single(self) -> None:
        from apm_cli.core.target_detection import TargetParamType

        tp = TargetParamType()
        result = tp.convert("openclaw", None, None)
        assert result == "openclaw"
        assert isinstance(result, str)

    def test_openclaw_parser_accepts_multi(self) -> None:
        from apm_cli.core.target_detection import TargetParamType

        tp = TargetParamType()
        result = tp.convert("openclaw,claude", None, None)
        assert isinstance(result, list)
        assert "openclaw" in result
        assert "claude" in result

    def test_openclaw_parser_multi_preserves_order(self) -> None:
        from apm_cli.core.target_detection import TargetParamType

        tp = TargetParamType()
        result = tp.convert("openclaw,claude", None, None)
        assert result == ["openclaw", "claude"]


# ===========================================================================
# TestOpenclawExperimentalFlags -- FLAGS registry
# ===========================================================================


class TestOpenclawExperimentalFlags:
    """FLAGS dict contains the openclaw entry with correct structure."""

    def test_openclaw_flag_present(self) -> None:
        from apm_cli.core.experimental import FLAGS

        assert "openclaw" in FLAGS

    def test_openclaw_flag_defaults_false(self) -> None:
        from apm_cli.core.experimental import FLAGS

        assert FLAGS["openclaw"].default is False

    def test_openclaw_flag_has_hint(self) -> None:
        from apm_cli.core.experimental import FLAGS

        assert FLAGS["openclaw"].hint, "openclaw flag must have a non-empty hint"


# ===========================================================================
# TestOpenclawTargetProfile -- registry shape
# ===========================================================================


class TestOpenclawTargetProfile:
    """KNOWN_TARGETS['openclaw'] has the expected profile shape."""

    def test_profile_exists(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        assert "openclaw" in KNOWN_TARGETS

    def test_root_dir_is_agents(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        assert KNOWN_TARGETS["openclaw"].root_dir == ".agents"

    def test_user_root_dir_is_openclaw(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        assert KNOWN_TARGETS["openclaw"].user_root_dir == ".openclaw"

    def test_user_supported(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        assert KNOWN_TARGETS["openclaw"].user_supported is True

    def test_detect_by_dir_false(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        assert KNOWN_TARGETS["openclaw"].detect_by_dir is False

    def test_requires_flag_openclaw(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        assert KNOWN_TARGETS["openclaw"].requires_flag == "openclaw"

    def test_skills_primitive_present(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        profile = KNOWN_TARGETS["openclaw"]
        assert "skills" in profile.primitives
        pm = profile.primitives["skills"]
        assert pm.subdir == "skills"
        assert pm.extension == "/SKILL.md"
        assert pm.format_id == "skill_standard"

    def test_compile_family_is_none(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        assert KNOWN_TARGETS["openclaw"].compile_family is None

    def test_auto_create_true(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        assert KNOWN_TARGETS["openclaw"].auto_create is True


# ===========================================================================
# TestOpenclawDescription -- get_target_description
# ===========================================================================


class TestOpenclawDescription:
    """get_target_description returns a known string for openclaw."""

    def test_description_not_unknown(self) -> None:
        from apm_cli.core.target_detection import get_target_description

        desc = get_target_description("openclaw")
        assert desc != "unknown target", (
            "openclaw missing from get_target_description() descriptions dict"
        )

    def test_description_mentions_openclaw_dir(self) -> None:
        from apm_cli.core.target_detection import get_target_description

        desc = get_target_description("openclaw")
        assert ".openclaw" in desc


# ===========================================================================
# TestOpenclawCrossTargetMap -- lockfile enrichment
# ===========================================================================


class TestOpenclawCrossTargetMap:
    """_CROSS_TARGET_MAPS contains an openclaw entry."""

    def test_cross_target_map_present(self) -> None:
        from apm_cli.bundle.lockfile_enrichment import _CROSS_TARGET_MAPS

        assert "openclaw" in _CROSS_TARGET_MAPS

    def test_cross_target_map_remaps_github_skills(self) -> None:
        from apm_cli.bundle.lockfile_enrichment import _CROSS_TARGET_MAPS

        mapping = _CROSS_TARGET_MAPS["openclaw"]
        assert ".github/skills/" in mapping
        assert mapping[".github/skills/"] == ".agents/skills/"
