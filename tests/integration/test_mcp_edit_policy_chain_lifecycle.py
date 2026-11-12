"""Multi-step MCP chains keep native configs, the lockfile and policy in agreement (#3649).

Each chain asserts exit codes, native config files and ``apm.lock.yaml``
after every step. Values are synthetic sentinels; every secret check asserts
the sentinel reaches no reference-preserving project file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration._mcp_chain_kit import (
    JSON_CONFIGS,
    SECRET,
    Chain,
    http_server,
    stdio_server,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]


def _project_texts(chain: Chain, *relatives: str) -> list[str]:
    return [
        (chain.project / relative).read_text(encoding="utf-8")
        for relative in relatives
        if (chain.project / relative).exists()
    ]


def test_secret_references_survive_reinstall_across_targets(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """References stay references in reference-preserving project configs; reinstall is a no-op."""
    chain = Chain(
        tmp_path / "secrets",
        apm_binary_path,
        dirs=(".claude", ".vscode", ".cursor", ".github", ".codex", ".opencode"),
    )
    chain.write_manifest(
        [
            stdio_server(
                "sec-stdio", "srv", env={"MCP_CHAIN_TOKEN": "${MCP_CHAIN_TOKEN}", "LEVEL": "debug"}
            ),
            http_server(
                "sec-http",
                "https://mcp.example.invalid/mcp",
                headers={"Authorization": "Bearer ${MCP_CHAIN_TOKEN}"},
            ),
        ]
    )
    targets = "claude,vscode,cursor,copilot,codex,opencode"
    first = chain.ok("install", "--no-policy", "--target", targets)
    preserving = (
        ".mcp.json",
        ".vscode/mcp.json",
        ".cursor/mcp.json",
        ".github/mcp.json",
        "opencode.json",
        ".codex/config.toml",
        "apm.lock.yaml",
    )
    for text in [*_project_texts(chain, *preserving), first.stdout, first.stderr]:
        assert SECRET not in text
    codex = (chain.project / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'env_vars = ["MCP_CHAIN_TOKEN"]' in codex
    snapshot = {relative: (chain.project / relative).read_bytes() for relative in preserving}

    chain.ok("install", "--no-policy", "--target", targets)
    for relative, content in snapshot.items():
        assert (chain.project / relative).read_bytes() == content, relative


def test_user_servers_survive_reinstall_removal_and_frozen(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """install -> user adds servers -> reinstall -> drop one -> frozen -> audit --ci."""
    chain = Chain(
        tmp_path / "user-servers",
        apm_binary_path,
        dirs=(".claude", ".vscode", ".cursor", ".github", ".codex", ".opencode"),
    )
    (chain.project / "opencode.json").write_text(
        json.dumps({"$schema": "https://opencode.ai/config.json", "theme": "dark"}), "utf-8"
    )
    chain.write_manifest(
        [stdio_server("owned-a", "a"), http_server("owned-b", "https://b.example.invalid")]
    )
    targets = "claude,vscode,cursor,copilot,codex,opencode"
    chain.ok("install", "--no-policy", "--target", targets)
    for target in JSON_CONFIGS:
        assert set(chain.servers(target)) == {"owned-a", "owned-b"}, target
    assert chain.lock()["mcp_servers"] == ["owned-a", "owned-b"]

    user_entries = {
        "claude": {"command": "mine"},
        "vscode": {"type": "stdio", "command": "mine"},
        "cursor": {"command": "mine"},
        "copilot": {"type": "local", "command": "mine", "tools": ["*"]},
        "opencode": {"type": "local", "command": ["mine"]},
    }
    for target, entry in user_entries.items():
        relative, key = JSON_CONFIGS[target]
        path = chain.project / relative
        document = json.loads(path.read_text(encoding="utf-8"))
        document[key]["mine"] = entry
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    with (chain.project / ".codex" / "config.toml").open("a", encoding="utf-8") as handle:
        handle.write('\n[mcp_servers.mine]\ncommand = "mine"\n')

    chain.ok("install", "--no-policy", "--target", targets)
    chain.ok("audit", "--ci", "--no-policy")
    chain.write_manifest([http_server("owned-b", "https://b.example.invalid")])
    chain.ok("install", "--no-policy", "--target", targets)
    for target, entry in user_entries.items():
        assert chain.servers(target).get("mine") == entry, target
        assert "owned-a" not in chain.servers(target), target
    assert sorted(chain.codex_servers()) == ["mine", "owned-b"]
    assert chain.lock()["mcp_servers"] == ["owned-b"]
    lock_bytes = (chain.project / "apm.lock.yaml").read_bytes()

    chain.ok("install", "--frozen", "--no-policy", "--target", targets)
    chain.ok("audit", "--ci", "--no-policy")
    assert (chain.project / "apm.lock.yaml").read_bytes() == lock_bytes
    assert (chain.project / "opencode.json").read_text("utf-8").count('"theme": "dark"') == 1


def test_policy_denied_mcp_fails_closed_through_install_mcp(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A denied server is never written, apm.yml is untouched, and the exit code is non-zero."""
    chain = Chain(tmp_path / "install-mcp", apm_binary_path, dirs=(".claude", ".vscode"))
    chain.seed_org_policy(
        "name: acme\nversion: 1.0.0\nenforcement: block\n"
        "mcp:\n  deny: ['evil-*']\n  transport:\n    allow: [stdio]\n  self_defined: allow\n"
    )
    chain.write_manifest([stdio_server("good-a", "good")])
    chain.ok("install", "--target", "claude,vscode")
    manifest = (chain.project / "apm.yml").read_bytes()
    lock = (chain.project / "apm.lock.yaml").read_bytes()
    prefix = ("install", "--mcp")

    denied_name = chain.run(*prefix, "evil-y", "--target", "claude,vscode", "--", "npx", "evil")
    denied_transport = chain.run(
        *prefix, "http-z", "--target", "claude,vscode", "--url", "https://z.example.invalid/mcp"
    )
    for result in (denied_name, denied_transport):
        output = result.stdout + result.stderr
        assert "Policy violation" in output, output
        assert result.returncode != 0, output
        assert (chain.project / "apm.yml").read_bytes() == manifest
        assert (chain.project / "apm.lock.yaml").read_bytes() == lock
        assert set(chain.servers("claude")) == {"good-a"}
        assert set(chain.servers("vscode")) == {"good-a"}
    chain.ok("audit", "--ci")


@pytest.mark.parametrize("layout", ["v1", "v2"])
def test_opencode_layout_is_stable_across_reinstall_and_removal(
    tmp_path: Path, apm_binary_path: Path, layout: str
) -> None:
    chain = Chain(tmp_path / layout, apm_binary_path, dirs=(".opencode",))
    user = {"type": "local", "command": ["mine"]}
    mcp: dict = {"codemode": True, "servers": {"mine": user}} if layout == "v2" else {"mine": user}
    document = {"$schema": "https://opencode.ai/config.json", "theme": "dark", "mcp": mcp}
    (chain.project / "opencode.json").write_text(json.dumps(document), encoding="utf-8")
    chain.write_manifest(
        [
            stdio_server("oc-a", "a", env={"MCP_CHAIN_TOKEN": "${MCP_CHAIN_TOKEN}"}),
            http_server("oc-b", "https://b.invalid"),
        ],
        targets=["opencode"],
    )

    def servers() -> dict:
        data = json.loads((chain.project / "opencode.json").read_text(encoding="utf-8"))
        assert data["theme"] == "dark"
        if layout == "v2":
            assert data["mcp"]["codemode"] is True
            assert set(data["mcp"]) == {"codemode", "servers"}
            return data["mcp"]["servers"]
        return data["mcp"]

    chain.ok("install", "--no-policy")
    assert set(servers()) == {"mine", "oc-a", "oc-b"}
    assert servers()["oc-a"]["environment"] == {"MCP_CHAIN_TOKEN": "{env:MCP_CHAIN_TOKEN}"}
    snapshot = (chain.project / "opencode.json").read_bytes()
    chain.ok("install", "--no-policy")
    assert (chain.project / "opencode.json").read_bytes() == snapshot
    chain.write_manifest([stdio_server("oc-a", "a", env={"MCP_CHAIN_TOKEN": "${MCP_CHAIN_TOKEN}"})])
    chain.ok("install", "--no-policy", "--target", "opencode")
    assert set(servers()) == {"mine", "oc-a"}
    assert servers()["mine"] == user
    chain.ok("install", "--frozen", "--no-policy", "--target", "opencode")
    chain.ok("audit", "--ci", "--no-policy")
    assert SECRET not in (chain.project / "opencode.json").read_text(encoding="utf-8")


def test_mcp_install_alias_writes_the_same_files_as_install_mcp(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3633: forwarded options produce byte-identical project and user configs."""
    trees: list[dict[str, bytes]] = []
    for entry in ("mcp-install", "install-mcp"):
        chain = Chain(tmp_path / entry, apm_binary_path, dirs=(".claude", ".vscode", ".cursor"))
        (chain.isolated.home / ".copilot").mkdir(parents=True, exist_ok=True)
        chain.write_manifest([])
        prefix = ("install", "--mcp")
        targets = ("--target", "claude,vscode,cursor", "--no-policy")
        manifest = (chain.project / "apm.yml").read_bytes()
        chain.ok(*prefix, "srv-dry", *targets, "--dry-run", "--", "npx", "dry")
        assert (chain.project / "apm.yml").read_bytes() == manifest
        assert not (chain.project / "apm.lock.yaml").exists()
        assert chain.servers("claude") == {}
        chain.ok(
            *prefix,
            "srv-env",
            *targets,
            "--env",
            "MCP_CHAIN_TOKEN=${MCP_CHAIN_TOKEN}",
            "--",
            "npx",
            "e",
        )
        chain.ok(
            *prefix,
            "srv-hdr",
            *targets,
            "--transport",
            "http",
            "--url",
            "https://h.example.invalid/mcp",
            "--header",
            "Authorization=Bearer ${MCP_CHAIN_TOKEN}",
        )
        chain.ok(*prefix, "srv-g", "--global", "--target", "copilot", "--no-policy", "--", "npx")
        chain.ok(*prefix, "srv-env", *targets, "--force", "--env", "LEVEL=changed", "--", "npx")
        tree = {
            relative: (chain.project / relative).read_bytes()
            for relative in ("apm.yml", "apm.lock.yaml", ".mcp.json", ".vscode/mcp.json")
        }
        tree["copilot-user"] = (chain.isolated.home / ".copilot" / "mcp-config.json").read_bytes()
        for relative, content in tree.items():
            assert SECRET.encode() not in content, relative
        trees.append(tree)
    assert trees[0] == trees[1]
