"""End-to-end regression guard for #1152: Copilot CLI's project mcp.json
must contain ``${VAR}`` runtime placeholders for env-var references in
apm.yml -- never the literal value resolved at install time.

This exercises the full pipeline:
    apm.yml  ->  apm install --runtime copilot  ->  .github/mcp.json

The unit tests in tests/unit/test_copilot_adapter.py cover translation in
isolation; this test pins the integration boundary so plaintext secrets
cannot regress back onto disk.

Also covers Cursor's native ``${env:NAME}`` syntax and guards against
writing resolved secret values to its project-local configuration.
"""

import json
import os
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import pytest
import yaml

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_mcp_registry import LocalMcpRegistryFactory

CursorScenario = tuple[Path, dict[str, str], IsolatedApmEnvironment]

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    # Mutates os.environ["HOME"]; must be serialized on a single xdist worker.
    # Requires --dist loadgroup in the xdist invocation (the only
    # scheduler that honors xdist_group); without it the marker is
    # silently ignored and tests would race on global env state.
    pytest.mark.xdist_group(name="home_env"),
]


def _write_apm_yml(project_dir, mcp_servers):
    config = {
        "name": "mcp-env-vars-copilot-e2e",
        "version": "1.0.0",
        "dependencies": {"apm": [], "mcp": mcp_servers},
    }
    (project_dir / "apm.yml").write_text(
        yaml.dump(config, default_flow_style=False), encoding="utf-8"
    )


class TestMcpEnvVarHeadersCopilot:
    """#1152 regression: Copilot project mcp.json must contain ``${VAR}``
    runtime placeholders for env-var references in apm.yml. The literal
    values from the installer's environment must NEVER appear on disk.
    """

    pytestmark = pytest.mark.requires_runtime_copilot

    def test_self_defined_http_server_translates_env_vars_not_resolves(
        self, tmp_path, apm_binary_path
    ):
        """``${VAR}`` and ``${env:VAR}`` syntaxes in apm.yml headers must
        land in .github/mcp.json as ``${VAR}`` (Copilot CLI's native runtime
        substitution syntax). No host env values may leak into the file.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        # Copilot target signal: presence of .github/ activates the
        # copilot/vscode runtime detection chain.
        (project_dir / ".github").mkdir()

        # Isolated HOME guards against accidental writes outside project scope.
        fake_home = tmp_path / "home"
        fake_home.mkdir()

        _write_apm_yml(
            project_dir,
            [
                {
                    "name": "test-http-server",
                    "registry": False,
                    "transport": "http",
                    "url": "https://example.com/mcp",
                    "headers": {
                        "Authorization": "Bearer ${MY_BEARER_TOKEN}",
                        "X-Api-Key": "${env:MY_API_KEY}",
                    },
                }
            ],
        )

        env = os.environ.copy()
        env["HOME"] = str(fake_home)
        # Sentinel values: if these strings ever appear in the rendered
        # config, the security contract has regressed.
        env["MY_BEARER_TOKEN"] = "should-not-appear-in-copilot-json"
        env["MY_API_KEY"] = "should-not-appear-in-copilot-json"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["APM_NON_INTERACTIVE"] = "1"

        result = subprocess.run(
            [apm_binary_path, "install", "--runtime", "copilot"],
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

        mcp_config = project_dir / ".github" / "mcp.json"
        assert mcp_config.exists(), (
            f"Expected .github/mcp.json to exist after install.\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        config = json.loads(mcp_config.read_text(encoding="utf-8"))
        servers = config.get("mcpServers") or {}
        assert len(servers) == 1, f"Expected 1 server in mcp.json, got: {list(servers.keys())}"
        server = next(iter(servers.values()))
        headers = server.get("headers") or {}

        # ``${VAR}`` already Copilot-native: pass through unchanged.
        assert headers.get("Authorization") == "Bearer ${MY_BEARER_TOKEN}", (
            f"Bare ${{VAR}} must remain ${{VAR}} for Copilot CLI.\nGot: {headers!r}"
        )
        # ``${env:VAR}`` translated to ``${VAR}`` (env: prefix stripped).
        assert headers.get("X-Api-Key") == "${MY_API_KEY}", (
            f"${{env:VAR}} must translate to ${{VAR}} for Copilot CLI.\nGot: {headers!r}"
        )

        # CRITICAL: no plaintext secret may appear anywhere in the file.
        full_text = mcp_config.read_text(encoding="utf-8")
        assert "should-not-appear-in-copilot-json" not in full_text, (
            "Copilot mcp.json leaked the literal env value -- "
            "the install-time translation regressed and secrets are now "
            "baked to disk.\n"
            f"File contents:\n{full_text}"
        )

    def test_self_defined_stdio_server_translates_env_vars_in_args(self, tmp_path, apm_binary_path):
        """Self-defined stdio server with env-var placeholders in BOTH the
        ``env`` block and ``args`` list must land in .github/mcp.json with
        ``${VAR}`` runtime placeholders. Closes the integration-tier gap
        flagged by test-coverage review for the ``_raw_stdio`` branch of
        ``_format_server_config`` and the supply-chain regression where
        the dict-shaped ``env`` block was silently dropped to ``{}``.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".github").mkdir()
        fake_home = tmp_path / "home"
        fake_home.mkdir()

        _write_apm_yml(
            project_dir,
            [
                {
                    "name": "test-stdio-server",
                    "registry": False,
                    "transport": "stdio",
                    "command": "echo",
                    "args": [
                        "--token=${env:MY_STDIO_TOKEN}",
                        "--bearer=${MY_STDIO_TOKEN}",
                        "--legacy=<MY_LEGACY_VAR>",
                    ],
                    "env": {
                        "PRIMARY_TOKEN": "${MY_STDIO_TOKEN}",
                        "PREFIXED_TOKEN": "${env:MY_STDIO_TOKEN}",
                        "LEGACY_TOKEN": "<MY_LEGACY_VAR>",
                    },
                }
            ],
        )

        env = os.environ.copy()
        env["HOME"] = str(fake_home)
        env["MY_STDIO_TOKEN"] = "stdio-secret-must-not-leak"
        env["MY_LEGACY_VAR"] = "legacy-secret-must-not-leak"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["APM_NON_INTERACTIVE"] = "1"

        result = subprocess.run(
            [apm_binary_path, "install", "--runtime", "copilot"],
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

        mcp_config = project_dir / ".github" / "mcp.json"
        assert mcp_config.exists(), (
            f"Expected .github/mcp.json to exist after install.\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        config = json.loads(mcp_config.read_text(encoding="utf-8"))
        servers = config.get("mcpServers") or {}
        assert len(servers) == 1, f"Expected 1 server in mcp.json, got: {list(servers.keys())}"
        server = next(iter(servers.values()))

        # env block: all three syntaxes translate to ${VAR}.
        env_block = server.get("env") or {}
        assert env_block.get("PRIMARY_TOKEN") == "${MY_STDIO_TOKEN}", (
            f"Bare ${{VAR}} in stdio env must remain ${{VAR}}.\nGot: {env_block!r}"
        )
        assert env_block.get("PREFIXED_TOKEN") == "${MY_STDIO_TOKEN}", (
            f"${{env:VAR}} in stdio env must translate to ${{VAR}}.\nGot: {env_block!r}"
        )
        assert env_block.get("LEGACY_TOKEN") == "${MY_LEGACY_VAR}", (
            f"Legacy <VAR> in stdio env must translate to ${{VAR}}.\nGot: {env_block!r}"
        )

        # args list: all three syntaxes translate to ${VAR}.
        args = server.get("args") or []
        assert "--token=${MY_STDIO_TOKEN}" in args, (
            f"${{env:VAR}} in stdio args must translate to ${{VAR}}.\nGot: {args!r}"
        )
        assert "--bearer=${MY_STDIO_TOKEN}" in args, (
            f"Bare ${{VAR}} in stdio args must remain ${{VAR}}.\nGot: {args!r}"
        )
        assert "--legacy=${MY_LEGACY_VAR}" in args, (
            f"Legacy <VAR> in stdio args must translate to ${{VAR}}.\nGot: {args!r}"
        )

        # CRITICAL: neither secret may appear as a literal anywhere.
        full_text = mcp_config.read_text(encoding="utf-8")
        assert "stdio-secret-must-not-leak" not in full_text, (
            f"Copilot stdio config leaked MY_STDIO_TOKEN as plaintext.\nFile contents:\n{full_text}"
        )
        assert "legacy-secret-must-not-leak" not in full_text, (
            f"Copilot stdio config leaked MY_LEGACY_VAR as plaintext.\nFile contents:\n{full_text}"
        )


@pytest.fixture
def cursor_scenario(tmp_path: Path) -> CursorScenario:
    """Bound every Cursor install to a scrubbed environment and local project."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "cursor", base_env=dict(os.environ))
    project = isolated.work_root / "project"
    project.mkdir()
    (project / ".cursor").mkdir()
    return (
        project,
        isolated.subprocess_env(),
        isolated,
    )


class TestMcpEnvVarHeadersCursor:
    """Cursor's native runtime references keep secrets out of project config."""

    @pytest.mark.parametrize("secret_value", [None, "literal-cursor-value"])
    def test_cursor_preserves_runtime_references_without_writing_secret(
        self, cursor_scenario: CursorScenario, apm_binary_path: Path, secret_value: str | None
    ) -> None:
        project_dir, env, _isolated = cursor_scenario
        runner = ApmLifecycleRunner((str(apm_binary_path),))
        _write_apm_yml(
            project_dir,
            [
                {
                    "name": "test-http-server",
                    "registry": False,
                    "transport": "http",
                    "url": "https://example.com/mcp",
                    "headers": {
                        "Authorization": "Bearer ${MY_BEARER_TOKEN}",
                        "x-mixed": "a=${env:MY_BEARER_TOKEN};b=<MY_BEARER_TOKEN>",
                        "x-static": "authored-header",
                    },
                }
            ],
        )

        env.pop("MY_BEARER_TOKEN", None)
        if secret_value is not None:
            env["MY_BEARER_TOKEN"] = secret_value
        result = runner.run(
            ("install", "--target", "cursor", "--no-policy"),
            cwd=project_dir,
            env=env,
        )

        assert result.returncode == 0, (
            f"apm install failed (rc={result.returncode}).\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        cursor_config = project_dir / ".cursor" / "mcp.json"
        assert cursor_config.exists(), (
            f"Expected .cursor/mcp.json to exist after install.\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        config = json.loads(cursor_config.read_text(encoding="utf-8"))
        servers = config.get("mcpServers") or {}
        assert len(servers) == 1, (
            f"Expected 1 server in cursor mcp.json, got: {list(servers.keys())}"
        )
        server = next(iter(servers.values()))
        headers = server.get("headers") or {}

        assert headers.get("Authorization") == "Bearer ${env:MY_BEARER_TOKEN}", (
            f"Cursor did not preserve its native runtime reference.\nGot: {headers!r}"
        )
        assert headers["x-mixed"] == "a=${env:MY_BEARER_TOKEN};b=${env:MY_BEARER_TOKEN}"
        assert headers["x-static"] == "authored-header"
        full_text = cursor_config.read_text(encoding="utf-8")
        assert "literal-cursor-value" not in full_text
        assert "literal-cursor-value" not in result.stdout + result.stderr

    def test_cursor_stdio_env_normalization_and_reinstall(
        self, cursor_scenario: CursorScenario, apm_binary_path: Path
    ) -> None:
        project, env, _isolated = cursor_scenario
        runner = ApmLifecycleRunner((str(apm_binary_path),))
        cursor_config = project / ".cursor" / "mcp.json"
        user_server = {"command": "printf", "args": ["user-owned"]}
        cursor_config.write_text(
            json.dumps({"mcpServers": {"user-kept": user_server}, "user-setting": True}),
            encoding="utf-8",
        )
        _write_apm_yml(
            project,
            [
                {
                    "name": "stdio-probe",
                    "registry": False,
                    "transport": "stdio",
                    "command": "printf",
                    "args": ["--token=${CURSOR_TOKEN}", "--native=${env:CURSOR_TOKEN}"],
                    "env": {
                        "STATIC": "authored-value",
                        "EMPTY": "",
                        "PORT": 3000,
                        "DEBUG": False,
                        "RATE": 0.5,
                        "OPTIONAL": None,
                        "REFERENCE": "${CURSOR_TOKEN}",
                        "ENVPREFIX": "${env:CURSOR_TOKEN}",
                        "ANGLE": "<CURSOR_TOKEN>",
                    },
                }
            ],
        )
        env["CURSOR_TOKEN"] = "cursor-first-sentinel"
        env["STATIC"] = "not-the-authored-value"
        first = runner.run(("install", "--target", "cursor", "--no-policy"), cwd=project, env=env)
        assert first.returncode == 0, first.stdout + first.stderr
        document = json.loads(cursor_config.read_text(encoding="utf-8"))
        assert document["mcpServers"]["stdio-probe"]["env"] == {
            "STATIC": "authored-value",
            "EMPTY": "",
            "PORT": "3000",
            "DEBUG": "false",
            "RATE": "0.5",
            "REFERENCE": "${env:CURSOR_TOKEN}",
            "ENVPREFIX": "${env:CURSOR_TOKEN}",
            "ANGLE": "${env:CURSOR_TOKEN}",
        }
        assert document["mcpServers"]["stdio-probe"]["args"] == [
            "--token=${env:CURSOR_TOKEN}",
            "--native=${env:CURSOR_TOKEN}",
        ]
        assert document["mcpServers"]["user-kept"] == user_server
        assert document["user-setting"] is True
        capture = {"config_paths": (PurePosixPath(".cursor/mcp.json"),)}
        before = LifecycleStateSnapshot.capture(project, **capture)
        env["CURSOR_TOKEN"] = "cursor-second-sentinel"
        results = runner.run_sequence(
            (
                ("install", "--target", "cursor", "--no-policy"),
                ("install", "--target", "cursor", "--force", "--only", "mcp", "--no-policy"),
            ),
            expected_returncodes=(0, 0),
            scenario_id="cursor-repeat",
            cwd=project,
            env=env,
        )
        after = LifecycleStateSnapshot.capture(project, **capture)
        assert before.file(".cursor/mcp.json").content == after.file(".cursor/mcp.json").content
        assert before.mcp_state_bytes == after.mcp_state_bytes
        manifest = yaml.safe_load((project / "apm.yml").read_text(encoding="utf-8"))
        manifest["dependencies"]["mcp"][0]["env"]["STATIC"] = "changed-manifest-value"
        _write_apm_yml(project, manifest["dependencies"]["mcp"])
        changed = runner.run(("install", "--target", "cursor", "--no-policy"), cwd=project, env=env)
        assert changed.returncode == 0, changed.stdout + changed.stderr
        document["mcpServers"]["stdio-probe"]["env"]["STATIC"] = "changed-manifest-value"
        assert json.loads(cursor_config.read_text(encoding="utf-8")) == document
        output = first.stdout + first.stderr + "".join(r.stdout + r.stderr for r in results)
        output += changed.stdout + changed.stderr
        stored = cursor_config.read_text(encoding="utf-8")
        for sentinel in ("cursor-first-sentinel", "cursor-second-sentinel"):
            assert sentinel not in stored
            assert sentinel not in output

    def test_cursor_targeted_repair_preserves_unrelated_configuration(
        self, cursor_scenario: CursorScenario, apm_binary_path: Path
    ) -> None:
        project, env, _isolated = cursor_scenario
        runner = ApmLifecycleRunner((str(apm_binary_path),))
        _write_apm_yml(
            project,
            [
                {
                    "name": "repair-probe",
                    "registry": False,
                    "transport": "http",
                    "url": "https://example.invalid/mcp",
                    "headers": {
                        "x-probe": "${CURSOR_TOKEN}",
                        "Authorization": "Bearer ${CURSOR_TOKEN}",
                    },
                }
            ],
        )
        cursor_config = project / ".cursor" / "mcp.json"
        unrelated = {"command": "printf", "args": ["user-owned"]}
        document = {
            "mcpServers": {
                "repair-probe": {
                    "type": "http",
                    "url": "https://example.invalid/mcp",
                    "headers": {
                        "x-probe": "old-baked-sentinel",
                        "Authorization": "Bearer old-baked-sentinel",
                    },
                    "user-setting": "retain",
                },
                "unrelated": unrelated,
            },
            "user-setting": {"keep": True},
        }
        cursor_config.write_text(json.dumps(document), encoding="utf-8")
        before = cursor_config.read_bytes()
        env["CURSOR_TOKEN"] = "current-cursor-sentinel"
        result = runner.run(("install", "--target", "cursor", "--no-policy"), cwd=project, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert cursor_config.read_bytes() == before
        document["mcpServers"]["repair-probe"]["headers"]["x-probe"] = "${env:CURSOR_TOKEN}"
        document["mcpServers"]["repair-probe"]["headers"]["Authorization"] = (
            "Bearer ${env:CURSOR_TOKEN}"
        )
        cursor_config.write_text(json.dumps(document), encoding="utf-8")
        repaired = cursor_config.read_bytes()
        repeated = runner.run(
            ("install", "--target", "cursor", "--no-policy"), cwd=project, env=env
        )
        assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        assert cursor_config.read_bytes() == repaired
        stored = json.loads(repaired)
        assert stored["mcpServers"]["unrelated"] == unrelated
        assert stored["mcpServers"]["repair-probe"]["user-setting"] == "retain"
        assert stored["user-setting"] == {"keep": True}
        assert b"old-baked-sentinel" not in repaired
        assert b"current-cursor-sentinel" not in repaired
        assert "current-cursor-sentinel" not in repeated.stdout + repeated.stderr

        del document["mcpServers"]["repair-probe"]
        cursor_config.write_text(json.dumps(document), encoding="utf-8")
        recreated = runner.run(
            ("install", "--target", "cursor", "--no-policy"), cwd=project, env=env
        )
        assert recreated.returncode == 0, recreated.stdout + recreated.stderr
        regenerated = cursor_config.read_text(encoding="utf-8")
        stored = json.loads(regenerated)
        assert stored["mcpServers"]["repair-probe"]["headers"] == {
            "x-probe": "${env:CURSOR_TOKEN}",
            "Authorization": "Bearer ${env:CURSOR_TOKEN}",
        }
        assert stored["mcpServers"]["unrelated"] == unrelated
        assert stored["user-setting"] == {"keep": True}
        for sentinel in ("old-baked-sentinel", "current-cursor-sentinel"):
            assert sentinel not in regenerated
            assert sentinel not in recreated.stdout + recreated.stderr
        stored["mcpServers"]["repair-probe"]["user-setting"] = "retain"
        cursor_config.write_text(json.dumps(stored), encoding="utf-8")
        restored = cursor_config.read_bytes()
        final = runner.run(("install", "--target", "cursor", "--no-policy"), cwd=project, env=env)
        assert final.returncode == 0, final.stdout + final.stderr
        assert cursor_config.read_bytes() == restored
        assert "current-cursor-sentinel" not in final.stdout + final.stderr

    @pytest.mark.parametrize(
        ("required_value", "optional_value"),
        [
            (None, None),
            ("required-registry-sentinel", None),
            ("required-registry-sentinel", ""),
            ("required-registry-sentinel", "optional-registry-sentinel"),
        ],
    )
    def test_cursor_registry_env_references_keep_optional_semantics(
        self,
        cursor_scenario: CursorScenario,
        apm_binary_path: Path,
        required_value: str | None,
        optional_value: str | None,
    ) -> None:
        project, env, isolated = cursor_scenario
        runner = ApmLifecycleRunner((str(apm_binary_path),))
        document = {
            "name": "io.github.apm/cursor-env-probe",
            "description": "Cursor environment fixture",
            "version": "1.0.0",
            "packages": [
                {
                    "registryType": "npm",
                    "identifier": "@apm/cursor-env-probe",
                    "transport": {"type": "stdio"},
                    "environmentVariables": [
                        {"name": "CURSOR_REQUIRED", "required": True},
                        {"name": "CURSOR_OPTIONAL", "required": False},
                    ],
                }
            ],
        }
        env.pop("CURSOR_REQUIRED", None)
        if required_value is not None:
            env["CURSOR_REQUIRED"] = required_value
        env.pop("CURSOR_OPTIONAL", None)
        if optional_value is not None:
            env["CURSOR_OPTIONAL"] = optional_value
        factory = LocalMcpRegistryFactory(isolated.root / "registries")
        with factory.start(document) as registry:
            port = urlparse(registry.url).port
            assert port is not None
            env["APM_TEST_LOOPBACK_PORTS"] = str(port)
            env["MCP_REGISTRY_ALLOW_HTTP"] = "1"
            _write_apm_yml(project, [{"name": document["name"], "registry": registry.url}])
            result = runner.run(
                ("install", "--target", "cursor", "--no-policy"), cwd=project, env=env
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert registry.request_paths
        stored = (project / ".cursor" / "mcp.json").read_text(encoding="utf-8")
        server = json.loads(stored)["mcpServers"]["cursor-env-probe"]
        expected = {"CURSOR_REQUIRED": "${env:CURSOR_REQUIRED}"}
        if optional_value:
            expected["CURSOR_OPTIONAL"] = "${env:CURSOR_OPTIONAL}"
        assert server["env"] == expected
        for sentinel in ("required-registry-sentinel", "optional-registry-sentinel"):
            assert sentinel not in stored
            assert sentinel not in result.stdout + result.stderr

    def test_other_target_does_not_opt_into_cursor(
        self, cursor_scenario: CursorScenario, apm_binary_path: Path
    ) -> None:
        project, env, _isolated = cursor_scenario
        runner = ApmLifecycleRunner((str(apm_binary_path),))
        (project / ".cursor").rmdir()
        _write_apm_yml(
            project,
            [{"name": "probe", "registry": False, "transport": "stdio", "command": "printf"}],
        )
        result = runner.run(("install", "--target", "claude", "--no-policy"), cwd=project, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (project / ".cursor").exists()

    def test_global_cursor_install_does_not_write_workspace_config(
        self, cursor_scenario: CursorScenario, apm_binary_path: Path
    ) -> None:
        project, env, isolated = cursor_scenario
        runner = ApmLifecycleRunner((str(apm_binary_path),))
        cursor_config = project / ".cursor" / "mcp.json"
        cursor_config.write_text('{"mcpServers":{"user-kept":{"command":"printf"}}}\n')
        before = cursor_config.read_bytes()
        _write_apm_yml(
            isolated.config_root,
            [
                {
                    "name": "global-probe",
                    "registry": False,
                    "transport": "stdio",
                    "command": "printf",
                }
            ],
        )
        result = runner.run(
            ("install", "--global", "--target", "cursor", "--no-policy"),
            cwd=project,
            env=env,
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert cursor_config.read_bytes() == before
        assert not (isolated.home / ".cursor" / "mcp.json").exists()
        assert not (isolated.config_root / ".cursor" / "mcp.json").exists()
        assert "workspace-only" in result.stdout + result.stderr
        assert "no effective target can accept" in result.stdout + result.stderr
