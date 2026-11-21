"""APM keeps a user-formatted MCP config byte-for-byte across its lifecycle (#4526).

Real ``apm`` binary, local path packages, no network. A user formats the
project ``.mcp.json`` and the user-scope ``~/.claude.json`` with 4-space
indentation, a final newline and their own nested server. APM adds its
server on install, rewrites it on update and removes it on uninstall; each
write keeps the user's style, so after uninstall each file is byte-identical
to what the user wrote. ``apm audit --ci`` and ``apm install --frozen`` stay
clean after every step, and a repeat install leaves the bytes alone.

Only indentation, the final newline, line endings and non-ASCII text are kept;
other layout (an inline array) is normalized, pinned by its own cell.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.integration._mcp_chain_kit import Chain

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.windows_compat,
    pytest.mark.promise("P-OWN-mcp-json-config-writer"),
]

_SERVER = "style-srv"

# Hand-written user files: 4-space indentation, a nested user server, and a
# final newline. Never produced by the writer under test.
_PROJECT_MCP = (
    b'{\n    "mcpServers": {\n        "mine": {\n            "command": "my-tool",\n'
    b'            "args": [\n                "--flag"\n            ],\n'
    b'            "env": {\n                "MY_LEVEL": "2"\n            }\n'
    b"        }\n    }\n}\n"
)
_CLAUDE_JSON = (
    b'{\n    "theme": "dark",\n    "mcpServers": {\n        "mine": {\n'
    b'            "command": "my-tool",\n            "env": {\n'
    b'                "MY_LEVEL": "2"\n            }\n        }\n    }\n}\n'
)


def _package(root: Path, version: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "style-pkg",
                "version": "1.0.0",
                "dependencies": {
                    "mcp": [
                        {
                            "name": _SERVER,
                            "registry": False,
                            "transport": "stdio",
                            "command": "npx",
                            "args": ["-y", f"style-srv@{version}"],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )


def _assert_user_style_with_apm_entry(path: Path, original: bytes, version: str) -> None:
    """The user's bytes stay; APM's entry is appended in 4-space style."""
    data = path.read_bytes()
    assert data.endswith(b"\n    }\n}\n"), data
    assert b'\n        "mine": {\n            "command": "my-tool",' in data, data
    entry = data[data.index(f'\n        "{_SERVER}": {{'.encode()) :]
    assert (
        f'\n            "args": [\n                "-y",\n                "style-srv@{version}"\n'.encode()
        in entry
    ), data
    assert b'\n  "' not in data, data
    servers = json.loads(data)["mcpServers"]
    assert next(iter(servers)) == "mine" and list(servers)[-1] == _SERVER, servers
    without_apm = json.loads(data)
    del without_apm["mcpServers"][_SERVER]
    assert without_apm == json.loads(original)


class _Scope:
    def __init__(self, chain: Chain, user: bool) -> None:
        self.chain = chain
        self.user = user
        self.flags = ("-g",) if user else ()
        home = chain.isolated.home
        self.config = home / ".claude.json" if user else chain.project / ".mcp.json"
        self.audit_cwd = chain.isolated.config_root if user else chain.project
        self.cwd = chain.isolated.work_root if user else chain.project

    def run(self, *args: str, cwd: Path | None = None):
        return self.chain.runner.run(
            args, cwd=cwd or self.cwd, env=self.chain.env, scenario_id=" ".join(args)
        )

    def ok(self, *args: str, cwd: Path | None = None):
        result = self.run(*args, cwd=cwd)
        assert result.returncode == 0, f"{args}\n{result.stdout}\n{result.stderr}"
        return result

    def assert_clean(self) -> None:
        self.ok("audit", "--ci", "--no-policy", cwd=self.audit_cwd)
        before = self.config.read_bytes()
        self.ok("install", *self.flags, "--frozen", "--no-policy")
        assert self.config.read_bytes() == before


def _world(tmp_path: Path, binary: Path, *, user: bool) -> tuple[_Scope, Path, bytes]:
    chain = Chain(tmp_path / ("user" if user else "project"), binary, dirs=(".claude",))
    for variable in ("CLAUDE_CONFIG_DIR", "COPILOT_HOME", "CODEX_HOME"):
        chain.env.pop(variable, None)
    scope = _Scope(chain, user)
    package = chain.isolated.package_root / "style-pkg"
    _package(package, "1")
    manifest = {"name": "style", "version": "1.0.0", "targets": ["claude"]}
    manifest["dependencies"] = {"apm": [str(package)], "mcp": []}
    if user:
        (chain.isolated.home / ".claude").mkdir(parents=True, exist_ok=True)
        chain.isolated.config_root.mkdir(parents=True, exist_ok=True)
        (chain.isolated.config_root / "apm.yml").write_text(
            yaml.safe_dump(manifest), encoding="utf-8"
        )
        original = _CLAUDE_JSON
    else:
        (chain.project / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        original = _PROJECT_MCP
    scope.config.write_bytes(original)
    return scope, package, original


@pytest.mark.parametrize("user", [False, True], ids=["project-mcp-json", "user-claude-json"])
def test_a_user_formatted_mcp_config_survives_install_update_and_uninstall(
    tmp_path: Path, apm_binary_path: Path, user: bool
) -> None:
    scope, package, original = _world(tmp_path, apm_binary_path, user=user)

    scope.ok("install", *scope.flags, "--no-policy")
    _assert_user_style_with_apm_entry(scope.config, original, "1")
    scope.assert_clean()
    installed = scope.config.read_bytes()
    scope.ok("install", *scope.flags, "--no-policy")
    assert scope.config.read_bytes() == installed, "a repeat install rewrote the file"

    _package(package, "2")
    scope.ok("install", *scope.flags, "--no-policy")
    _assert_user_style_with_apm_entry(scope.config, original, "2")
    scope.assert_clean()

    scope.ok("uninstall", *scope.flags, str(package))
    assert scope.config.read_bytes() == original
    scope.assert_clean()


# An inline array is user layout APM does not keep: the file is JSON
# round-tripped in the user's indentation, so the array gets one item per line.
_INLINE_ARRAY_MCP = (
    b'{\n    "mcpServers": {\n        "mine": {\n            "command": "my-tool",\n'
    b'            "args": ["--a", "--b"]\n        }\n    }\n}\n'
)
_INLINE_ARRAY_NORMALIZED = (
    b'{\n    "mcpServers": {\n        "mine": {\n            "command": "my-tool",\n'
    b'            "args": [\n                "--a",\n                "--b"\n'
    b"            ]\n        }\n    }\n}\n"
)


def test_other_user_layout_is_normalized_in_the_users_indentation(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scope, package, _original = _world(tmp_path, apm_binary_path, user=False)
    scope.config.write_bytes(_INLINE_ARRAY_MCP)

    scope.ok("install", "--no-policy")
    scope.ok("uninstall", str(package))

    assert scope.config.read_bytes() == _INLINE_ARRAY_NORMALIZED
