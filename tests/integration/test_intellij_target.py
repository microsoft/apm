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
