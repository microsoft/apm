"""Installed-CLI contract: a refused ``--frozen`` install changes nothing (#4517).

When ``apm.yml`` drops a target whose files ``apm.lock.yaml`` still records,
retiring them deletes deployed files and rewrites the lock, which
``--frozen`` may not do. It must refuse before any write, name the target
and its files, and print the catalog remedy; running that remedy retires the
target, after which ``--frozen`` passes. ``apm audit --ci`` agrees at each
step (I3).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._marketplace_pack_kit import tree_bytes
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise(
        "P-LAW-transaction.failed_command_preserves_state",
        "P-LAW-lockfile.frozen_preserves_bytes",
        "P-INV-I3",
    ),
]

_COPILOT = ".github/instructions/one.instructions.md"
_CLAUDE = ".claude/rules/one.md"
_REMEDY = (
    "Run 'apm install' to retire the dropped target and update apm.lock.yaml "
    "(without --frozen, keeping --root DIR or --global if specified), then commit apm.lock.yaml."
)


class _Project:
    def __init__(self, tmp_path: Path, apm_binary_path: Path) -> None:
        isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
        self.env = isolated.subprocess_env()
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
        self.root = isolated.work_root / "consumer"
        source = self.root / "pkgs" / "a"
        (source / ".apm" / "instructions").mkdir(parents=True)
        dump_yaml({"name": "a", "version": "1.0.0"}, source / "apm.yml")
        (source / ".apm" / "instructions" / "one.instructions.md").write_bytes(
            b"---\napplyTo: '**'\n---\n# One\n"
        )
        dump_yaml(
            {
                "name": "consumer",
                "version": "1.0.0",
                "targets": ["copilot", "claude"],
                "dependencies": {"apm": ["./pkgs/a"]},
            },
            self.root / "apm.yml",
        )

    def apm(self, *args: str, expect: int) -> str:
        result = self.runner.run(args, cwd=self.root, env=self.env)
        text = result.stdout + result.stderr
        assert result.returncode == expect, f"apm {' '.join(args)} -> {result.returncode}\n{text}"
        assert text.isascii(), text  # I6
        return text

    def lock_files(self) -> set[str]:
        lock = load_yaml(self.root / "apm.lock.yaml")
        return {path for dep in lock["dependencies"] for path in dep.get("deployed_files", [])}

    def converged(self) -> None:
        """I2 + I3: frozen and audit --ci both pass and change no bytes."""
        before = tree_bytes(self.root)
        self.apm("install", "--frozen", expect=0)
        self.apm("audit", "--ci", expect=0)
        assert tree_bytes(self.root) == before


def test_frozen_refuses_a_dropped_target_with_zero_writes_and_its_remedy_runs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project = _Project(tmp_path, apm_binary_path)
    project.apm("install", expect=0)
    assert (project.root / _COPILOT).is_file()
    assert (project.root / _CLAUDE).is_file()
    assert {_COPILOT, _CLAUDE} <= project.lock_files()
    project.converged()

    manifest = load_yaml(project.root / "apm.yml")
    dump_yaml({**manifest, "targets": ["copilot"]}, project.root / "apm.yml")
    before = tree_bytes(project.root)

    # I7: the refused frozen install writes nothing -- lock, deployed files, apm_modules.
    refused = project.apm("install", "--frozen", expect=1)
    assert tree_bytes(project.root) == before, "a refused --frozen install changed files"
    squeezed = " ".join(refused.split())
    assert "no longer declares target(s) claude" in squeezed, refused
    assert f"{_CLAUDE} (target claude)" in squeezed, refused
    assert _REMEDY in squeezed, refused
    assert len(re.findall(re.escape(_REMEDY), squeezed)) == 1, refused  # I9

    # I3: audit --ci also refuses this state, and writes nothing.
    project.apm("audit", "--ci", expect=1)
    assert tree_bytes(project.root) == before

    # Execute the printed remedy: plain install retires the dropped target.
    project.apm("install", expect=0)
    assert not (project.root / _CLAUDE).exists()
    assert (project.root / _COPILOT).is_file()
    assert _CLAUDE not in project.lock_files()
    assert _COPILOT in project.lock_files()
    project.converged()
