"""Integration tests for the 'intellij' MCP-only pseudo-target.

Covers:
  1. Parser-layer constants: intellij in VALID_TARGET_VALUES / MCP_ONLY_TARGETS,
     not in ALL_CANONICAL_TARGETS.
  2. TargetParamType accepts intellij as single and multi-target.
  3. CLI parser accepts ``--target intellij`` without error.
  4. Policy target check normalises intellij -> copilot via
     RUNTIME_TO_CANONICAL_TARGET so org allow-lists are not falsely rejected.

IntelliJ is an MCP-only pseudo-target: it has an IntelliJClientAdapter
registered in the MCP client registry but no KNOWN_TARGETS entry and no
file-level primitive deployment. It maps to 'copilot' via
RUNTIME_TO_CANONICAL_TARGET.

Ref: issue #1957, PR #2041.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.core.target_detection import (
    ALL_CANONICAL_TARGETS,
    MCP_ONLY_TARGETS,
    VALID_TARGET_VALUES,
    TargetParamType,
    normalize_target_list,
)
from apm_cli.integration.targets import RUNTIME_TO_CANONICAL_TARGET

_MINIMAL_APM_YML = "name: test\ndescription: test\nversion: 0.0.1\n"
_BASE_ENV: dict[str, str] = {"APM_E2E_TESTS": "1"}


@pytest.fixture()
def apm_command() -> str:
    """Return the APM executable used by the end-to-end test."""
    executable_name = "apm.exe" if sys.platform == "win32" else "apm"
    venv_apm = (
        Path(__file__).parents[2]
        / ".venv"
        / ("Scripts" if sys.platform == "win32" else "bin")
        / executable_name
    )
    if venv_apm.exists():
        return str(venv_apm)
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    pytest.fail("APM executable not found in the project virtualenv or PATH")


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated home directory wired into every APM config lookup."""
    home = tmp_path / "home"
    apm_dir = home / ".apm"
    apm_dir.mkdir(parents=True)
    (apm_dir / "apm.yml").write_text(_MINIMAL_APM_YML, encoding="ascii")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    import apm_cli.config as _conf

    monkeypatch.setattr(_conf, "CONFIG_DIR", str(apm_dir))
    monkeypatch.setattr(_conf, "CONFIG_FILE", str(apm_dir / "config.json"))
    monkeypatch.setattr(_conf, "_config_cache", None)
    yield home
    monkeypatch.setattr(_conf, "_config_cache", None)


# ===========================================================================
# Constant guards
# ===========================================================================


class TestIntelliJConstants:
    """Constant-split guards ensuring intellij stays MCP-only."""

    def test_intellij_in_valid_target_values(self) -> None:
        """intellij must be accepted by --target."""
        assert "intellij" in VALID_TARGET_VALUES

    def test_intellij_not_in_all_canonical_targets(self) -> None:
        """intellij must NOT bleed into ALL_CANONICAL_TARGETS / 'all'."""
        assert "intellij" not in ALL_CANONICAL_TARGETS

    def test_intellij_in_mcp_only_targets(self) -> None:
        """intellij must live in MCP_ONLY_TARGETS."""
        assert "intellij" in MCP_ONLY_TARGETS

    def test_intellij_runtime_canonical_maps_to_copilot(self) -> None:
        """RUNTIME_TO_CANONICAL_TARGET must map intellij -> copilot."""
        assert RUNTIME_TO_CANONICAL_TARGET.get("intellij") == "copilot"

    def test_all_mcp_only_targets_have_canonical_mapping(self) -> None:
        """Every MCP-only target must have a fail-closed policy mapping."""
        missing = MCP_ONLY_TARGETS - RUNTIME_TO_CANONICAL_TARGET.keys()
        assert not missing, f"MCP-only targets lack canonical mappings: {missing}"

    def test_parser_accepts_single(self) -> None:
        """TargetParamType.convert accepts 'intellij' as a single token."""
        tp = TargetParamType()
        result = tp.convert("intellij", None, None)
        assert result == "intellij"

    def test_parser_accepts_multi(self) -> None:
        """TargetParamType.convert accepts 'intellij,claude' as multi-target."""
        tp = TargetParamType()
        result = tp.convert("intellij,claude", None, None)
        assert isinstance(result, list)
        assert "intellij" in result
        assert "claude" in result

    def test_all_expansion_excludes_intellij(self) -> None:
        """normalize_target_list('all') must NOT include intellij."""
        result = normalize_target_list("all")
        assert "intellij" not in result


# ===========================================================================
# CLI E2E
# ===========================================================================


class TestIntelliJCliE2E:
    """CliRunner tests for 'apm install --target intellij'."""

    def test_cli_parser_accepts_target_intellij(self, tmp_path: Path, fake_home: Path) -> None:
        """``apm install --target intellij`` must not fail with 'Unknown target'.

        We do not assert a successful install (no package is provided),
        just that the CLI parser accepts the target token without error.
        """
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["install", "--target", "intellij"],
            env=_BASE_ENV,
            catch_exceptions=False,
        )
        # No "Unknown target" error -- the parser accepted the value.
        # The command will fail for other reasons (no package specified)
        # but that is fine -- we only care about parser acceptance.
        assert "Unknown target" not in (result.output or "")

    @pytest.mark.requires_apm_binary
    def test_mcp_install_writes_intellij_config(self, tmp_path: Path, apm_command: str) -> None:
        """The real CLI install flow writes JetBrains Copilot's MCP config."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "apm.yml").write_text(_MINIMAL_APM_YML, encoding="ascii")
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        xdg_data = fake_home / "xdg"
        local_app_data = fake_home / "local-app-data"

        env = os.environ.copy()
        env.update(
            {
                "HOME": str(fake_home),
                "XDG_DATA_HOME": str(xdg_data),
                "LOCALAPPDATA": str(local_app_data),
                "GIT_TERMINAL_PROMPT": "0",
                "APM_NON_INTERACTIVE": "1",
            }
        )

        result = subprocess.run(
            [
                apm_command,
                "install",
                "--mcp",
                "test-http-server",
                "--target",
                "intellij",
                "--transport",
                "http",
                "--url",
                "https://example.com/mcp",
            ],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, (
            f"apm install failed (rc={result.returncode}).\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
        if sys.platform == "win32":
            config_path = local_app_data / "github-copilot" / "intellij" / "mcp.json"
        elif sys.platform == "darwin":
            config_path = (
                fake_home
                / "Library"
                / "Application Support"
                / "github-copilot"
                / "intellij"
                / "mcp.json"
            )
        else:
            config_path = xdg_data / "github-copilot" / "intellij" / "mcp.json"

        assert config_path.exists(), (
            f"Expected IntelliJ MCP config at {config_path}.\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert config["servers"]["test-http-server"]["url"] == "https://example.com/mcp"


# ===========================================================================
# Policy target normalisation
# ===========================================================================


class TestIntelliJPolicyNormalisation:
    """Verify policy_target_check normalises intellij -> copilot."""

    def test_runtime_to_canonical_normalisation(self) -> None:
        """The RUNTIME_TO_CANONICAL_TARGET mapping must resolve intellij.

        This is the mapping used in policy_target_check.py to normalise
        the effective_target before passing it to _check_compilation_target.
        An org with ``allow: [copilot]`` must not reject --target intellij.
        """
        effective_target = "intellij"
        normalised = RUNTIME_TO_CANONICAL_TARGET.get(effective_target, effective_target)
        assert normalised == "copilot"
