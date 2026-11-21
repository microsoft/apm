"""APMLifecycle: a deny retires an installed local bundle's LSP server (RA-041).

PROMISE: P-INV-I4 -- deny, revoke and tightening never widen what is deployed.
The LSP twin of RA-025: after ``apm deny <bundle key>``, the ``apm install``
the deny names retires the bundle's LSP server, and ``install --frozen`` and
``apm audit --ci`` agree on the converged state (I3).
"""

from __future__ import annotations

import json
import re

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.world import World
from tests.lifecycle.world.fixtures import (  # noqa: F401
    _world_dir,
    lifecycle_template,
    lifecycle_world,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I4"),
    pytest.mark.promise("P-INV-I3"),
]

_SERVER = "p-lsp-pyright"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _apm(world: World, *args: str) -> CommandResult:
    return driver.run(world, list(args), entry_point=driver.driver_command("binary"))


def _ok(world: World, *args: str) -> CommandResult:
    result = _apm(world, *args)
    assert result.returncode == 0, _evidence(result)
    return result


def _lsp_bundle(world: World) -> str:
    """Pack a local bundle whose only executable is one LSP server."""
    author = world.root / "authored" / "p-lsp"
    author.mkdir(parents=True)
    (author / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "p-lsp",
                "version": "1.0.0",
                "description": "lsp bundle",
                "license": "MIT",
                "targets": ["claude"],
                "dependencies": {},
            }
        ),
        encoding="utf-8",
    )
    server = {"command": "pyright-langserver", "args": ["--stdio"]}
    server["extensionToLanguage"] = {".py": "python"}
    (author / ".lsp.json").write_text(json.dumps({_SERVER: server}) + "\n", encoding="ascii")
    runner = ApmLifecycleRunner(driver.driver_command("binary"), timeout_seconds=180)
    packed = runner.run(("pack",), cwd=author, env=world.env, scenario_id="lsp-bundle-pack")
    assert packed.returncode == 0, _evidence(packed)
    (bundle,) = [path for path in (author / "build").iterdir() if path.is_dir()]
    return bundle.as_posix()


def _lsp_deployed(world: World) -> list[str]:
    root = world.project
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and not {"apm_modules", ".git", ".apm"} & set(path.relative_to(root).parts)
        and path.name not in ("apm.yml", "apm.lock.yaml")
        and _SERVER in path.read_text(encoding="utf-8", errors="replace")
    )


def test_deny_then_install_retires_the_bundle_lsp_server(
    lifecycle_world: World,  # noqa: F811
) -> None:
    world = lifecycle_world
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["targets"] = ["claude"]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    bundle = _lsp_bundle(world)

    first = _apm(world, "install", bundle)
    key = re.search(r"apm approve '(p-lsp#[^']+@sha256:[0-9a-f]+)'", first.stdout + first.stderr)
    assert key, _evidence(first)
    _ok(world, "approve", key.group(1))
    _ok(world, "install", bundle)
    assert _lsp_deployed(world), "the approved bundle LSP server must deploy"

    denied = _ok(world, "deny", key.group(1))
    # The deny names the command that retires the bundle's executables; run it.
    hint = " ".join(f"{denied.stdout} {denied.stderr}".split())
    assert "Run 'apm install'" in hint, _evidence(denied)
    _ok(world, "install")

    assert _lsp_deployed(world) == [], _evidence(denied)
    _ok(world, "install", "--frozen")
    _ok(world, "audit", "--ci")
