"""Real-CLI lifecycle: uninstall names the executable kinds it retires (#4567).

When the executables gate turns on after a deploy with no approvals, uninstall
redeploys each surviving package under the gate and warns once per package.
The warning used to say "hooks" for every package, including one that ships
only an LSP server or a canvas extension; it now names the kinds the gate
withholds, with install's labels, and says "hooks" only for packages that
ship hooks. Removal itself is unchanged.
"""

from __future__ import annotations

import os
import re
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
    pytest.mark.promise("P-OWN-user-facing-output-diagnostics"),
]

_HOOK = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}
_CANVAS = "export default function render() {\n  return 'p-canvas';\n}\n"


def _lsp(name: str) -> dict[str, object]:
    return {
        "name": f"{name}-server",
        "command": "apm-lifecycle-inert-server",
        "args": ["--stdio"],
        "extensionToLanguage": {f".{name}": name},
    }


def _world(root: Path) -> Path:
    factory = LocalPackageFactory(root)
    for name in ("p-exec", "p-hook"):
        factory.add_hook(factory.create(name, version="1.0.0"), "session", _HOOK)
    factory.create("p-lsp", version="1.0.0", lsp_dependencies=[_lsp("plsp")])
    factory.add_canvas(factory.create("p-canvas", version="1.0.0"), "pcanvas", _CANVAS)
    multi = factory.create("p-multi", version="1.0.0", lsp_dependencies=[_lsp("pmulti")])
    factory.add_hook(multi, "session", _HOOK)
    consumer = factory.create(
        "consumer",
        dependencies=tuple(
            f"../{name}" for name in ("p-exec", "p-hook", "p-lsp", "p-canvas", "p-multi")
        ),
        targets=("copilot",),
    )
    return consumer.root


def test_uninstall_names_each_retired_executable_kind_once(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "w", base_env=dict(os.environ))
    project = _world(isolated.work_root)
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    env = isolated.subprocess_env()

    def run(*args: str) -> str:
        result = runner.run(args, scenario_id="4567", cwd=project, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout + result.stderr

    run("install", "--no-policy")
    manifest = load_yaml(project / "apm.yml")
    manifest["executables"] = {"allow": {}}
    dump_yaml(manifest, project / "apm.yml")

    output = run("uninstall", "../p-exec")

    lines = {
        match.group(1): match.group(0)
        for match in re.finditer(r"(p-[a-z]+) (.+) are no longer allowed \(not approved\)", output)
    }
    assert lines["p-lsp"].startswith("p-lsp LSP server(s) are"), output
    assert lines["p-canvas"].startswith("p-canvas canvas extension(s) are"), output
    assert lines["p-hook"] == "p-hook hooks are no longer allowed (not approved)", output
    assert lines["p-multi"].startswith("p-multi hooks and LSP server(s) are"), output
    assert "p-hook hooks are no longer allowed (not approved); removing them" in output
    hook_lines = {package for package, line in lines.items() if "hooks" in line}
    assert hook_lines == {"p-hook", "p-multi"}, output
