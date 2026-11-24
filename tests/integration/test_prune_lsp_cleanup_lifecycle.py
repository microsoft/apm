"""Prune removes a pruned package's LSP registration in the same run (#5250).

PROMISE: P-OWN-lsp-lifecycle -- ``apm prune`` removes the LSP server entries
and the ``lsp_servers`` / ``lsp_configs`` lock records the lock attributes to
a package it prunes, through the reconcile uninstall uses; a surviving
package's entry and a user-authored entry are kept (source: #5250 scope
record; P-LAW-ownership.preserve_unowned).
CELL: T3|OP=prune|SRC=local|PRIM=lsp TGT=copilot SCOPE=project
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-lsp-lifecycle", "P-LAW-ownership.preserve_unowned"),
]

_LSP = ".github/lsp.json"
_USER_ENTRY = {"command": "user-lsp"}


def _lsp_package(root: Path, name: str) -> None:
    root.mkdir(parents=True)
    dump_yaml(
        {
            "name": name,
            "version": "1.0.0",
            "dependencies": {
                "lsp": [
                    {
                        "name": f"{name}-server",
                        "command": "/bin/true",
                        "args": ["--stdio"],
                        "extensionToLanguage": {f".{name}": name},
                    }
                ]
            },
        },
        root / "apm.yml",
    )
    skill = root / ".apm" / "skills" / f"{name}-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: {name}-skill\ndescription: Skill of {name}\n---\n# {name}\n",
        encoding="ascii",
    )


class _World:
    def __init__(self, tmp_path: Path, apm_binary_path: Path, packages: tuple[str, ...]) -> None:
        isolated = IsolatedApmEnvironment.create(tmp_path / "w", base_env=dict(os.environ))
        self.env = isolated.subprocess_env()
        self.project = isolated.work_root / "proj"
        self.project.mkdir()
        for name in packages:
            _lsp_package(self.project / "vendor" / name, name)
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    def manifest(self, packages: tuple[str, ...]) -> None:
        dump_yaml(
            {
                "name": "proj",
                "version": "1.0.0",
                "targets": ["copilot"],
                "dependencies": {"apm": [f"./vendor/{name}" for name in packages]},
            },
            self.project / "apm.yml",
        )

    def apm(self, *args: str) -> str:
        result = self.runner.run(args, cwd=self.project, env=self.env)
        output = " ".join((result.stdout + result.stderr).split())
        assert result.returncode == 0, f"apm {' '.join(args)} -> {result.returncode}: {output}"
        return output

    def servers(self) -> dict:
        return json.loads((self.project / _LSP).read_text(encoding="utf-8"))["lspServers"]

    def lock(self) -> dict:
        # Prune removes the lockfile once it records nothing.
        path = self.project / "apm.lock.yaml"
        return (load_yaml(path) or {}) if path.exists() else {}


def test_prune_removes_the_pruned_package_lsp_entry_and_lock_records(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _World(tmp_path, apm_binary_path, ("lsppkg",))
    world.manifest(("lsppkg",))
    assert "Configured 1 LSP server" in world.apm("install")
    assert "lsppkg-server" in world.servers()
    world.manifest(())

    pruned = world.apm("prune")

    assert "Removed _local/lsppkg" in pruned, pruned
    lsp_file = world.project / _LSP
    assert not lsp_file.exists() or "lsppkg-server" not in world.servers(), pruned
    lock = world.lock()
    assert "lsppkg-server" not in (lock.get("lsp_servers") or []), lock
    assert "lsppkg-server" not in (lock.get("lsp_configs") or {}), lock
    installed = world.apm("install")
    assert "stale LSP" not in installed, installed


def test_prune_keeps_a_surviving_package_and_a_user_lsp_entry(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _World(tmp_path, apm_binary_path, ("gone", "kept"))
    world.manifest(("gone", "kept"))
    world.apm("install")
    servers = world.servers()
    servers["user-server"] = _USER_ENTRY
    (world.project / _LSP).write_text(json.dumps({"lspServers": servers}) + "\n", encoding="ascii")
    world.manifest(("kept",))

    pruned = world.apm("prune")

    servers = world.servers()
    assert "gone-server" not in servers, pruned
    assert servers["kept-server"]["command"] == "/bin/true"
    assert servers["user-server"] == _USER_ENTRY
    lock = world.lock()
    assert "gone-server" not in (lock.get("lsp_servers") or []), lock
    assert "kept-server" in (lock.get("lsp_servers") or []), lock
    assert "user-server" not in (lock.get("lsp_servers") or []), lock
    settled = (world.project / _LSP).read_bytes()
    installed = world.apm("install")
    assert "stale LSP" not in installed, installed
    assert (world.project / _LSP).read_bytes() == settled
