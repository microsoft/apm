"""APMLifecycle: vendor-extension target ids and dependency keys parse on the CLI.

PROMISE: req-tg-004 / req-mf-005 (#4251) -- an ``x-<vendor>-<name>`` target
parses; with no registered vendor handler the CLI names it and deploys only the
targets it can. req-ext-001 (#4260) -- an ``x-`` key inside an object-form
``dependencies.apm[]`` entry is ignored, never interpreted, and survives a
manifest rewrite (source: docs/src/content/docs/specs/openapm-v0.1.md).
CELL: OP=install SRC=local PRIM=instruction SCOPE=project TGT=copilot+x-vendor
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-LAW-routing.authorized_targets_only"),
]

_TARGET = "copilot"
_VENDOR = "x-acme-foo"
_INSTRUCTION = "---\napplyTo: '**'\ndescription: d\n---\nBody\n"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _flat(result: CommandResult) -> str:
    return " ".join((result.stdout + "\n" + result.stderr).split())


@pytest.mark.req("req-tg-004")
def test_vendor_target_is_named_and_not_deployed(tmp_path: Path, apm_binary_path: Path) -> None:
    """req-tg-004: the vendor target parses, is named once, and deploys nothing."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.work_root)
    alpha = packages.create("alpha")
    packages.add_instruction(alpha, "alpha", _INSTRUCTION)
    project = packages.create("consumer", targets=(_TARGET,))
    packages.add_relative_dependency(project, alpha)
    manifest = project.root / "apm.yml"
    data = load_yaml(manifest)
    data["targets"] = [_TARGET, _VENDOR]
    dump_yaml(data, manifest)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    result = runner.run(("install",), cwd=project.root, env=env)

    assert result.returncode == 0, _evidence(result)
    flat = _flat(result)
    assert flat.isascii(), _evidence(result)
    warning = (
        f"[!] apm.yml declares vendor target '{_VENDOR}', which has no registered handler "
        "in this APM version; nothing is deployed for it."
    )
    assert flat.count(warning) == 1, _evidence(result)
    assert "Targets: copilot  (source: apm.yml)" in result.stdout + result.stderr
    assert (project.root / ".github/instructions/alpha.instructions.md").is_file()
    assert not (project.root / _VENDOR).exists()

    before = LifecycleStateSnapshot.capture(project.root, targets=(_TARGET,))
    flag = runner.run(("install", "--target", _VENDOR), cwd=project.root, env=env)
    assert flag.returncode == 2, _evidence(flag)
    assert f"vendor target '{_VENDOR}' has no registered handler" in _flat(flag), _evidence(flag)
    assert LifecycleStateSnapshot.capture(project.root, targets=(_TARGET,)) == before


@pytest.mark.req("req-ext-001")
def test_dependency_entry_vendor_key_is_ignored_and_preserved(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """req-ext-001: an x- key in a dependencies.apm[] mapping parses and is kept on rewrite."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.work_root)
    alpha = packages.create("alpha")
    packages.add_instruction(alpha, "alpha", _INSTRUCTION)
    beta = packages.create("beta")
    packages.add_instruction(beta, "beta", _INSTRUCTION)
    project = packages.create("consumer", targets=(_TARGET,))
    manifest = project.root / "apm.yml"
    data = load_yaml(manifest)
    data["dependencies"] = {"apm": [{"path": "../alpha", "x-acme-pin": "keep"}]}
    dump_yaml(data, manifest)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    installed = runner.run(("install",), cwd=project.root, env=env)
    assert installed.returncode == 0, _evidence(installed)
    assert (project.root / ".github/instructions/alpha.instructions.md").is_file()

    added = runner.run(("install", "../beta"), cwd=project.root, env=env)
    assert added.returncode == 0, _evidence(added)
    entries = load_yaml(manifest)["dependencies"]["apm"]
    assert {"path": "../alpha", "x-acme-pin": "keep"} in entries, entries
    assert (project.root / ".github/instructions/beta.instructions.md").is_file()
