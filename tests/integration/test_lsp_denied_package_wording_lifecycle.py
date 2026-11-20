"""APMLifecycle: a denied package's LSP server is filtered as denied (#4237).

PROMISE: I4 -- a deny never increases what deploys, and the warning names the
decision: a denied package is "denied", not "not trusted yet", and is never
told to approve itself (source: .agents/skills/apm-lifecycle-tests).
CELL: OP=install SRC=local PRIM=lsp SCOPE=project GATE=deny-after-approve TGT=copilot
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I4"),
]

_PKG = "../lsp-kit"


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_denied_package_lsp_filter_names_the_deny(tmp_path: Path, apm_binary_path: Path) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    LocalPackageFactory(isolated.work_root).create(
        "lsp-kit",
        lsp_dependencies=(
            {
                "name": "kit-lsp",
                "command": "inert-language-server",
                "extensionToLanguage": {".kit": "kit"},
            },
        ),
    )
    project = isolated.work_root / "consumer"
    (project / ".github").mkdir(parents=True)
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": [_PKG]},
            "executables": {"allow": {}},
        },
        project / "apm.yml",
    )
    env = isolated.subprocess_env()
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    lsp_config = project / ".github" / "lsp.json"

    def apm(*args: str) -> str:
        result = runner.run(args, cwd=project, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        return _flat(result.stdout + result.stderr)

    apm("install", "--no-policy")
    apm("approve", _PKG)
    apm("install", "--no-policy")
    assert "kit-lsp" in json.loads(lsp_config.read_text(encoding="utf-8")).get("lspServers", {})

    apm("deny", _PKG)
    denied = apm("install", "--no-policy")

    assert f"Filtered 1 LSP server from '{_PKG}': declaring package is denied." in denied, denied
    assert f"apm policy explain {_PKG}" in denied, denied
    assert "not trusted yet" not in denied, denied
    assert f"apm approve {_PKG}" not in denied, denied
    servers = (
        json.loads(lsp_config.read_text(encoding="utf-8")).get("lspServers", {})
        if lsp_config.exists()
        else {}
    )
    assert "kit-lsp" not in servers
