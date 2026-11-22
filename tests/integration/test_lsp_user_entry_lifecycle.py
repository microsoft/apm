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
        lock = load_yaml(project / "apm.lock.yaml")
        assert "p-lsp" not in lock.get("lsp_servers", []), "lock claims the user's entry"
        assert "p-lsp" not in lock.get("lsp_configs", {}), "lock claims the user's entry"

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


def _project(isolated, name: str, *, lsp=(), targets=("copilot",)) -> Path:
    return (
        LocalPackageFactory(isolated.work_root)
        .create(name, lsp_dependencies=tuple(lsp), targets=targets)
        .root
    )


def _seed(project: Path, servers: dict) -> None:
    (project / ".github").mkdir(exist_ok=True)
    (project / _LSP).write_text(json.dumps({"lspServers": servers}) + "\n", encoding="ascii")


def test_an_identical_user_lsp_entry_is_kept_but_never_owned(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Equality alone never makes APM the owner: removal later keeps the entry."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "lsp-same", base_env=dict(os.environ))
    declared = {"name": "p-lsp", "command": "pkg-lsp", "extensionToLanguage": {".p": "p"}}
    project = _project(isolated, "consumer", lsp=(declared,))
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    env = isolated.subprocess_env()
    # The user's own file already holds exactly the entry install would write.
    assert _install(runner, project, env).returncode == 0
    (project / "apm.lock.yaml").unlink()
    seeded = (project / _LSP).read_bytes()

    for args in ((), ("--frozen",)):
        result = _install(runner, project, env, *args)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (project / _LSP).read_bytes() == seeded, args
        lock = load_yaml(project / "apm.lock.yaml")
        assert "p-lsp" not in lock.get("lsp_servers", []), "lock claims the user's entry"

    manifest = load_yaml(project / "apm.yml")
    manifest["dependencies"]["lsp"] = []
    dump_yaml(manifest, project / "apm.yml")
    result = _install(runner, project, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (project / _LSP).read_bytes() == seeded, "removal deleted the user's entry"


def test_a_bundle_records_what_it_wrote_before_failing_on_a_user_lsp_entry(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "lsp-bundle", base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.work_root)
    producer = factory.create(
        "lsp-bundler",
        version="1.0.0",
        lsp_dependencies=(
            {"name": "p-lsp", "command": "pkg-lsp", "extensionToLanguage": {".p": "p"}},
            {"name": "other-lsp", "command": "other", "extensionToLanguage": {".o": "o"}},
        ),
        targets=("copilot",),
    )
    factory.add_skill(producer, "b-skill", "---\nname: b-skill\ndescription: b.\n---\n\nUse b.\n")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    env = isolated.subprocess_env()
    out = tmp_path / "out"
    packed = runner.run(("pack", "-o", str(out)), cwd=producer.root, env=env)
    assert packed.returncode == 0, packed.stdout + packed.stderr
    (bundle,) = [path for path in out.iterdir() if path.is_dir()]
    project = _project(isolated, "consumer")
    _seed(project, {"p-lsp": _MINE})

    result = _install(runner, project, env, str(bundle))
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 1, output
    assert "LSP server 'p-lsp' conflicts with the existing copilot entry" in output
    servers = _servers(project)
    assert servers["p-lsp"] == _MINE
    assert servers["other-lsp"]["command"] == "other"
    lock = load_yaml(project / "apm.lock.yaml")
    assert lock["lsp_servers"] == ["other-lsp"], lock.get("lsp_servers")
    assert lock["lsp_target_servers"] == {"copilot": ["other-lsp"]}
    deployed = json.dumps(lock.get("local_deployed_files") or lock)
    assert "b-skill" in deployed, "the bundle's deployed files went unrecorded"


def test_an_mcp_write_stays_recorded_when_a_user_lsp_entry_conflicts(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4697: the LSP refusal must not discard the record of an MCP write it follows."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "lsp-mcp", base_env=dict(os.environ))
    project = (
        LocalPackageFactory(isolated.work_root)
        .create(
            "consumer",
            mcp_dependencies=(
                {
                    "name": "my-mcp",
                    "registry": False,
                    "transport": "stdio",
                    "command": "my-mcp-server",
                    "args": [],
                },
            ),
            lsp_dependencies=(
                {"name": "p-lsp", "command": "pkg-lsp", "extensionToLanguage": {".p": "p"}},
            ),
            targets=("copilot",),
        )
        .root
    )
    _seed(project, {"p-lsp": _MINE})
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    result = _install(runner, project, isolated.subprocess_env())
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 1, output
    assert "LSP server 'p-lsp' conflicts with the existing copilot entry" in output
    assert _servers(project)["p-lsp"] == _MINE
    written = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "my-mcp" in written, output
    lock = load_yaml(project / "apm.lock.yaml")
    assert "my-mcp" in lock.get("mcp_servers", []), "the MCP write went unrecorded"
