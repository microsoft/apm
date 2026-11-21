"""A user's own LSP entry is never taken over by install (#4628).

PROMISE: P-LAW-ownership.preserve_unowned -- an LSP server entry APM did not
write survives an install that declares the same name; the install names it,
configures every other server and fails like the MCP native-conflict path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-LAW-ownership.preserve_unowned"),
]

_LSP = ".github/lsp.json"
_MINE = {"command": "mine"}


def _servers(project: Path) -> dict:
    return json.loads((project / _LSP).read_text(encoding="utf-8"))["lspServers"]


def _install(runner, project: Path, env: dict, *args: str):
    return runner.run(("install", "--no-policy", *args), cwd=project, env=env)


def test_install_preserves_a_same_name_user_lsp_entry_and_configures_the_rest(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "lsp-user", base_env=dict(os.environ))
    project = (
        LocalPackageFactory(isolated.work_root)
        .create(
            "consumer",
            lsp_dependencies=(
                {"name": "p-lsp", "command": "pkg-lsp", "extensionToLanguage": {".p": "p"}},
                {"name": "other-lsp", "command": "other", "extensionToLanguage": {".o": "o"}},
            ),
            targets=("copilot",),
        )
        .root
    )
    (project / ".github").mkdir()
    (project / _LSP).write_text(
        json.dumps({"lspServers": {"p-lsp": _MINE, "user-lsp": {"command": "u"}}}) + "\n",
        encoding="ascii",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    env = isolated.subprocess_env()

    for args in ((), ("--force",)):
        result = _install(runner, project, env, *args)
        output = " ".join((result.stdout + result.stderr).split())
        assert result.returncode == 1, output
        assert "LSP server 'p-lsp' conflicts with the existing copilot entry" in output
        assert "Preserved that entry" in output
        servers = _servers(project)
        assert servers["p-lsp"] == _MINE
        assert servers["user-lsp"] == {"command": "u"}
        assert servers["other-lsp"]["command"] == "other"

    # Control: once the user removes their entry, APM writes it and later updates it.
    servers = _servers(project)
    del servers["p-lsp"]
    (project / _LSP).write_text(json.dumps({"lspServers": servers}) + "\n", encoding="ascii")
    assert _install(runner, project, env).returncode == 0
    assert _servers(project)["p-lsp"]["command"] == "pkg-lsp"
    manifest = load_yaml(project / "apm.yml")
    for entry in manifest["dependencies"]["lsp"]:
        if entry["name"] == "p-lsp":
            entry["command"] = "pkg-lsp-v2"
    dump_yaml(manifest, project / "apm.yml")
    result = _install(runner, project, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _servers(project)["p-lsp"]["command"] == "pkg-lsp-v2"
    assert _servers(project)["user-lsp"] == {"command": "u"}
