"""A bundle's lock carries no native plugin record, and its consumer registers none (#5253).

PROMISE: P-INV-I5 -- each command writes only its declared write set: a bundle
never carries a native plugin root, so neither its lock nor its consumer's
install names one (source: docs/src/content/docs/reference/cli/pack.md).
PROMISE: P-OWN-claude-native-plugin-registration -- native_plugin records are
lockfile-derived ownership, so only an install that deployed the root writes one.

CELL: OP=pack then OP=bundle-install, a mixed dependency row: one loose file
recorded in ``deployed_files`` beside a ``native_plugin`` record.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.native_plugins.record import NativePluginRecord
from apm_cli.utils.content_hash import compute_file_hash
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.promise("P-INV-I5", "P-OWN-claude-native-plugin-registration"),
]

_SKILL = ".claude/skills/demo/SKILL.md"


def _mixed_project(root: Path) -> Path:
    """A project whose one dependency deployed a loose skill and recorded a whole plugin."""
    skill = root / _SKILL
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: demo\n---\n# demo\n", encoding="utf-8")
    (root / "apm.yml").write_text(
        "name: producer\nversion: 1.0.0\nlicense: MIT\ntargets: [claude]\n"
        "dependencies:\n  apm: []\n",
        encoding="utf-8",
    )
    record = NativePluginRecord("demo", "sha256:" + "0" * 64, "apm-p-0", "./.agents/plugins")
    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(
            repo_url="org/demo",
            resolved_commit="0" * 40,
            version="1.0.0",
            deployed_files=[_SKILL],
            deployed_file_hashes={_SKILL: compute_file_hash(skill)},
            native_plugin=record,
            native_declared=["claude"],
        )
    )
    (root / "apm.lock.yaml").write_text(lockfile.to_yaml(), encoding="utf-8")
    return root


def test_a_mixed_row_packs_its_loose_files_and_no_native_plugin_record(
    tmp_path: Path, apm_engine_command: tuple[str, ...]
) -> None:
    producer = IsolatedApmEnvironment.create(tmp_path / "producer", base_env=dict(os.environ))
    project = _mixed_project(producer.work_root)
    runner = ApmLifecycleRunner(apm_engine_command)

    packed = runner.run(("pack",), cwd=project, env=producer.subprocess_env())

    assert packed.returncode == 0, packed.stdout + packed.stderr
    (bundle,) = [path for path in (project / "build").iterdir() if path.is_dir()]
    (row,) = yaml.safe_load((bundle / "apm.lock.yaml").read_text(encoding="utf-8"))["dependencies"]
    assert "native_plugin" not in row and "native_declared" not in row, row
    assert row["deployed_files"] == [_SKILL], row
    assert (bundle / "skills" / "demo" / "SKILL.md").is_file()

    consumer = IsolatedApmEnvironment.create(tmp_path / "consumer", base_env=dict(os.environ))
    target = consumer.work_root
    (target / "apm.yml").write_text(
        "name: consumer\nversion: 1.0.0\ntargets: [claude]\ndependencies:\n  apm: []\n",
        encoding="utf-8",
    )

    installed = runner.run(("install", str(bundle)), cwd=target, env=consumer.subprocess_env())

    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert (target / ".claude" / "skills" / "demo" / "SKILL.md").is_file()
    assert not (target / ".agents").exists(), sorted(map(str, target.rglob("*")))
    lock = target / "apm.lock.yaml"
    assert "native_plugin" not in (lock.read_text(encoding="utf-8") if lock.exists() else "")
    settings = target / ".claude" / "settings.json"
    assert "enabledPlugins" not in (
        settings.read_text(encoding="utf-8") if settings.exists() else ""
    )
