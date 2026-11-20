"""run mcp install flows that read the project root."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import toml

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry_dep(name: str) -> MagicMock:
    dep = MagicMock()
    dep.name = name
    dep.is_registry_resolved = True
    dep.is_self_defined = False
    dep.env = {}
    return dep


def _make_self_defined_dep(name: str, transport: str = "stdio") -> MagicMock:
    dep = MagicMock()
    dep.name = name
    dep.is_registry_resolved = False
    dep.is_self_defined = True
    dep.transport = transport
    dep.env = {}
    return dep


# ---------------------------------------------------------------------------
# Empty deps
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# scope enum overrides user_scope bool
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# single runtime mode (--runtime)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# registry ImportError
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# exclude runtime filtering
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# no target runtimes after gating
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# self-defined deps
# ---------------------------------------------------------------------------


class TestRunMcpInstallSelfDefined:
    def test_self_defined_dep_separated_correctly(self, tmp_path: Path) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        sd_dep = _make_self_defined_dep("my-server")
        logger = MagicMock()

        with patch(
            "apm_cli.integration.mcp_integrator_install._install_self_defined_deps",
            return_value=1,
        ) as install:
            result = run_mcp_install(
                [sd_dep], runtime="copilot", logger=logger, project_root=tmp_path
            )
        assert result == 1
        assert install.call_args.kwargs["self_defined_deps"] == [sd_dep]

    @pytest.mark.parametrize(
        ("runtime", "signal_dir", "config_relpath"),
        [
            ("claude", ".claude", ".mcp.json"),
            ("codex", ".codex", ".codex/config.toml"),
        ],
    )
    def test_self_defined_stdio_env_placeholders_follow_target_support(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        runtime: str,
        signal_dir: str,
        config_relpath: str,
    ) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install
        from apm_cli.models.dependency.mcp import MCPDependency

        (tmp_path / signal_dir).mkdir()
        monkeypatch.setenv("MY_TOKEN", "repro-secret-not-a-real-token")
        dep = MCPDependency(
            name="env-demo",
            registry=False,
            transport="stdio",
            command="npx",
            args=["-y", "example-mcp"],
            env={
                "MY_TOKEN": "${MY_TOKEN}",
                "LITERAL_VALUE": "literal-value",
            },
        )

        result = run_mcp_install(
            [dep],
            runtime=runtime,
            project_root=tmp_path,
            logger=MagicMock(),
        )

        assert result == 1
        config_path = tmp_path / config_relpath
        content = config_path.read_text(encoding="utf-8")
        if runtime == "claude":
            # Claude Code expands ${VAR} in .mcp.json at server start (#3252).
            env_block = json.loads(content)["mcpServers"]["env-demo"]["env"]
            assert env_block["MY_TOKEN"] == "${MY_TOKEN}"
            assert "repro-secret-not-a-real-token" not in content
            assert env_block["LITERAL_VALUE"] == "literal-value"
            return
        # Codex forwards a ``${VAR}`` written under its own key from the
        # launching environment, so the resolved secret never reaches the file.
        server = toml.loads(content)["mcp_servers"]["env-demo"]
        assert server["env_vars"] == ["MY_TOKEN"]
        assert server["env"] == {"LITERAL_VALUE": "literal-value"}
        assert "repro-secret-not-a-real-token" not in content


# ---------------------------------------------------------------------------
# plain strings treated as registry deps
# ---------------------------------------------------------------------------


# No assertion besides no-unexpected-crash during dep classification


# ---------------------------------------------------------------------------
# stored_mcp_configs defaults to empty dict
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# apm_config lazy load
# ---------------------------------------------------------------------------


class TestRunMcpInstallApmConfigLazyLoad:
    def test_lazy_load_called_when_no_apm_config(self, tmp_path) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("name: test\nscripts: {}\n", encoding="utf-8")

        with patch(
            "apm_cli.integration.mcp_integrator_install.load_yaml", return_value={}
        ) as load_yaml:
            run_mcp_install([dep], apm_config=None, project_root=tmp_path, logger=logger)

        load_yaml.assert_called_once_with(apm_yml)
        assert apm_yml.read_text(encoding="utf-8") == "name: test\nscripts: {}\n"

    def test_apm_config_provided_skips_file_load(self, tmp_path) -> None:
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = _make_registry_dep("server1")
        logger = MagicMock()

        with patch("apm_cli.integration.mcp_integrator_install.load_yaml") as mock_load:
            run_mcp_install(
                [dep],
                apm_config={"scripts": {}},
                project_root=tmp_path,
                logger=logger,
            )

        mock_load.assert_not_called()


# ---------------------------------------------------------------------------
# verbose detail logging
# ---------------------------------------------------------------------------


# verbose_detail may or may not be called depending on path taken;
# the test verifies no crash in verbose mode


# ---------------------------------------------------------------------------
# console print path (no console)
# ---------------------------------------------------------------------------
