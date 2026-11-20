"""Remediation after ``apm deny`` names the deny and actually works (#3957).

``apm install --frozen`` names the executables gate, not a missing
declaration. ``apm approve <pkg>`` lifts a deny in the store it writes, while
``--all`` skips denied packages, and a deny in another layer makes approve
exit 1 naming it. ``apm deny --user`` and ``apm approve --user`` work outside
a project and write only the user store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.integration._mcp_chain_kit import Chain, stdio_server

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_PACKAGE = "./pkgs/tools"


def _chain(tmp_path: Path, binary: Path) -> Chain:
    chain = Chain(tmp_path / "world", binary, dirs=(".claude",))
    package = chain.project / "pkgs" / "tools"
    package.mkdir(parents=True)
    manifest = {
        "name": "tools",
        "version": "1.0.0",
        "dependencies": {"mcp": [stdio_server("tools-server", "x")]},
    }
    (package / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    chain.write_manifest([], apm=[_PACKAGE], targets=["claude"])
    chain.ok("install", "--no-policy")
    return chain


def _text(result) -> str:
    return " ".join(f"{result.stdout}\n{result.stderr}".split())


def _user_store(chain: Chain) -> dict:
    config = chain.isolated.home / ".apm" / "config.json"
    return json.loads(config.read_text("utf-8")).get("executables", {})


def test_frozen_names_the_gate_and_approve_lifts_a_project_deny(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain = _chain(tmp_path, apm_binary_path)
    chain.ok("deny", _PACKAGE)

    frozen = chain.run("install", "--frozen", "--no-policy")
    assert frozen.returncode == 1
    assert "executables gate now parks it (denied)" in _text(frozen)
    assert "no longer declared" not in _text(frozen)

    bulk = chain.ok("approve", "--all")
    assert "Skipped ./pkgs/tools: denied in apm.yml" in _text(bulk)
    assert "Approved" not in _text(bulk)

    approved = chain.ok("approve", "tools")
    assert "Cleared deny for ./pkgs/tools" in _text(approved)
    executables = yaml.safe_load((chain.project / "apm.yml").read_text("utf-8"))["executables"]
    assert not executables.get("deny")

    chain.ok("install", "--no-policy")
    assert "tools-server" in chain.servers("claude")


def test_approve_exits_1_when_another_layer_still_denies(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain = _chain(tmp_path, apm_binary_path)
    chain.ok("deny", "--user", _PACKAGE)

    result = chain.run("approve", "tools")

    assert result.returncode == 1
    text = _text(result)
    assert "still denied (user-deny)" in text
    assert "apm approve --user ./pkgs/tools" in text
    assert "[+] Approved" not in text


def test_user_store_deny_and_approve_work_outside_a_project(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain = _chain(tmp_path, apm_binary_path)
    home = chain.isolated.home
    (home / ".claude").mkdir(exist_ok=True)
    user_manifest = {
        "name": "home",
        "version": "1.0.0",
        "targets": ["claude"],
        "dependencies": {"apm": [str(chain.project / "pkgs" / "tools")]},
    }
    (home / ".apm" / "apm.yml").write_text(yaml.safe_dump(user_manifest), encoding="utf-8")
    outside = home / "elsewhere"
    outside.mkdir()

    def run(*args: str):
        return chain.runner.run(args, cwd=outside, env=chain.env, scenario_id=" ".join(args))

    installed = run("install", "-g", "--no-policy")
    assert installed.returncode == 0, _text(installed)

    denied = run("deny", "--user", "tools")
    assert denied.returncode == 0, _text(denied)
    assert _user_store(chain)["deny"]

    approved = run("approve", "--user", "tools")
    assert approved.returncode == 0, _text(approved)
    assert "No apm.yml found" not in _text(approved)
    store = _user_store(chain)
    assert not store.get("deny")
    assert store["allow"]
    assert not (outside / "apm.yml").exists()
