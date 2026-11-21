"""APMLifecycle: a plugin from a local marketplace replays under --frozen (#4639).

PROMISE: P-INV-I3 -- ``apm install --frozen`` and ``apm audit --ci`` agree with
the state ``apm install`` converged (source: issue #4639 scope record).
CELL: OP=install SRC=marketplace-local|marketplace-file PRIM=instruction
SCOPE=project FROZEN=on

A local catalog plugin is locked by its content, not a commit: a converged
project passes ``--frozen`` with no writes; an edited catalog entry or changed
plugin content still fails ``--frozen`` before any write, and the printed
``apm install`` hint re-locks it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise(
        "P-INV-I3", "P-LAW-lockfile.frozen_preserves_bytes", "P-OWN-marketplace-locked-selection"
    ),
]

_TARGET = "copilot"
_PLUGIN = "p-cat"
_RULE = ".github/instructions/rule.instructions.md"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _assert_output_oracles(result: CommandResult) -> None:
    """I6 ASCII and I9 no duplicate status lines per stream."""
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), _evidence(result)
        lines = [line.strip() for line in stream.splitlines() if line.lstrip().startswith("[")]
        assert len(lines) == len(set(lines)), _evidence(result)


def _rule(body: str) -> str:
    return f'---\napplyTo: "**"\n---\n{body}\n'


def _plugin(plugins: LocalPackageFactory, name: str, body: str) -> LocalPackage:
    plugin = plugins.create(name, version="1.0.0")
    plugins.add_instruction(plugin, "rule", _rule(body))
    return plugin


def _write_catalog(catalog: Path, name: str, plugin_dir: str) -> None:
    document = {
        "name": name,
        "owner": {"name": "APM Test"},
        "plugins": [{"name": _PLUGIN, "description": "Local plugin", "source": plugin_dir}],
    }
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(json.dumps(document, indent=2) + "\n", encoding="ascii")


@pytest.mark.parametrize("as_file", [False, True], ids=["local-mkt", "file-mkt"])
def test_local_catalog_plugin_passes_frozen_and_refuses_changes_without_writes(
    tmp_path: Path, apm_binary_path: Path, as_file: bool
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    root = isolated.work_root / "catalog"
    plugins = LocalPackageFactory(root / "plugins")
    plugin = _plugin(plugins, _PLUGIN, "locked rule")
    _plugin(plugins, "p-moved", "moved rule")
    marketplace = "lifecycle-file" if as_file else "lifecycle-local"
    catalog = root / ("marketplace.json" if as_file else ".claude-plugin/marketplace.json")
    _write_catalog(catalog, marketplace, f"./plugins/{_PLUGIN}")

    project = isolated.work_root / "project"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "0.1.0",
            "targets": [_TARGET],
            "dependencies": {"apm": [{"name": _PLUGIN, "marketplace": marketplace}]},
        },
        project / "apm.yml",
    )

    def apm(*args: str, expected: int = 0) -> CommandResult:
        result = runner.run(args, cwd=project, env=env)
        _assert_output_oracles(result)
        assert result.returncode == expected, _evidence(result)  # I8
        return result

    def snap() -> LifecycleStateSnapshot:
        return LifecycleStateSnapshot.capture(project, targets=(_TARGET,))

    apm("marketplace", "add", str(catalog if as_file else root))
    apm("install")
    converged = snap()
    assert (project / _RULE).read_text(encoding="ascii") == _rule("locked rule")
    apm("install")
    assert snap() == converged, "I2: a converged install changed durable state"

    # Decisive (#4639): the converged project replays under --frozen, writing nothing.
    apm("install", "--frozen")
    assert snap() == converged, "--frozen changed a converged project"
    apm("audit", "--ci")  # I3: the audit agrees with --frozen.

    # An edited catalog entry no longer selects the lock row: refused before any write.
    _write_catalog(catalog, marketplace, "./plugins/p-moved")
    refused = apm("install", "--frozen", expected=1)
    assert "declared source differs from apm.lock.yaml" in refused.stdout + refused.stderr
    assert snap() == converged, "a refused --frozen install wrote state"
    # The printed hint, run: a plain install locks the catalog's new selection.
    apm("install")
    assert (project / _RULE).read_text(encoding="ascii") == _rule("moved rule")
    relocked = snap()
    apm("install", "--frozen")
    assert snap() == relocked

    # Changed plugin content fails --frozen and audit --ci alike, writing nothing.
    _write_catalog(catalog, marketplace, f"./plugins/{_PLUGIN}")
    apm("install")
    relocked = snap()
    plugins.add_instruction(plugin, "rule", _rule("edited rule"))
    refused = apm("install", "--frozen", expected=1)
    assert "content differs from its locked snapshot" in refused.stdout + refused.stderr
    assert snap() == relocked, "a refused --frozen install wrote state"
    apm("audit", "--ci", expected=1)
