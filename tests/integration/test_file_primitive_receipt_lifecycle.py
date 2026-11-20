"""Installed-CLI contract: every file primitive APM writes gets a local receipt (#3701).

Local deployment receipts used to cover only skills, so cleanup had no
evidence of deployment for instructions, prompts, agents and hooks beyond
a lockfile hash. Each final-rendered write now records a receipt in the
installation-bound session under ``APM_HOME``. A file that was already
byte-identical on disk is adopted without a write and gets no receipt:
receipt-less adoption stays limited to the existing skill rule.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.file_ops import robust_rmtree
from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

# Adopted unchanged when already byte-identical on disk.
_ADOPTABLE = (
    ".github/instructions/style.instructions.md",
    ".claude/rules/style.md",
    ".github/prompts/hello.prompt.md",
    ".github/agents/helper.agent.md",
    ".claude/agents/helper.md",
    ".github/hooks/scripts/prims/scripts/guard.sh",
    ".claude/hooks/prims/scripts/guard.sh",
)
# Rendered and written on every install.
_FILE_PRIMITIVES = (*_ADOPTABLE, ".claude/commands/hello.md", ".github/hooks/prims-guard.json")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("ascii"))


def _package(root: Path) -> None:
    root.mkdir(parents=True)
    dump_yaml({"name": "prims", "version": "1.0.0"}, root / "apm.yml")
    _write(root / ".apm/instructions/style.instructions.md", "---\napplyTo: '**'\n---\n# Style\n")
    _write(root / ".apm/prompts/hello.prompt.md", "---\ndescription: Hello\n---\nHello.\n")
    _write(root / ".apm/agents/helper.agent.md", "---\ndescription: Helper\n---\nHelp.\n")
    hook = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "./scripts/guard.sh"}]}
            ]
        }
    }
    _write(root / ".apm/hooks/guard.json", json.dumps(hook, indent=2) + "\n")
    _write(root / ".apm/hooks/scripts/guard.sh", "#!/bin/sh\necho guard\n")


def _receipts(isolated: IsolatedApmEnvironment) -> dict[str, str]:
    """Map each receipted physical path to its recorded content hash."""
    out: dict[str, str] = {}
    for state in (isolated.home / ".apm" / "deployment-receipts").glob("*.json"):
        data = json.loads(state.read_text(encoding="utf-8"))
        hashes = {
            f"{row['target']}|{row['runtime'] or ''}|{row['scope']}|{row['value']}": row[
                "content_hash"
            ]
            for row in data["records"]
        }
        for key, physical in data["destinations"].items():
            out[Path(physical).as_posix()] = hashes[key]
    return out


def _setup(tmp_path: Path, apm_binary_path: Path):
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    _package(isolated.work_root / "prims")
    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "0.1.0",
            "targets": ["copilot", "claude"],
            "dependencies": {"apm": ["../prims"]},
        },
        project / "apm.yml",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    def apm(*args: str) -> str:
        result = runner.run(args, cwd=project, env=env)
        output = result.stdout + result.stderr
        assert result.returncode == 0, f"apm {' '.join(args)}\n{output}"
        return output

    return isolated, project, apm


def test_install_records_a_receipt_for_every_file_primitive_write(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated, project, apm = _setup(tmp_path, apm_binary_path)

    apm("install", "--no-policy")

    receipts = _receipts(isolated)
    for relative in _FILE_PRIMITIVES:
        path = project / relative
        assert path.is_file(), sorted(p.as_posix() for p in project.rglob("*") if p.is_file())
        physical = path.resolve().as_posix()
        assert receipts.get(physical) == compute_file_hash(path), (relative, receipts)


def test_adopted_identical_files_get_no_receipt(tmp_path: Path, apm_binary_path: Path) -> None:
    """Committed output on a machine without receipts is adopted, not receipted."""
    isolated, project, apm = _setup(tmp_path, apm_binary_path)
    apm("install", "--no-policy")
    receipts_dir = isolated.home / ".apm" / "deployment-receipts"
    robust_rmtree(receipts_dir)
    robust_rmtree(project / ".apm-local")
    assert not receipts_dir.exists()

    apm("install", "--no-policy")

    receipts = _receipts(isolated)
    for relative in _ADOPTABLE:
        assert (project / relative).resolve().as_posix() not in receipts, relative


# Cursor-native hooks refuse to coexist with Claude's, so Cursor installs alone.
@pytest.mark.parametrize("cursor", [False, True], ids=["all-but-cursor", "cursor"])
def test_every_deployed_file_of_a_fresh_install_has_a_receipt(
    tmp_path: Path, apm_binary_path: Path, cursor: bool
) -> None:
    """Closed-world: no file primitive writer on any target skips its receipt."""
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.targets import KNOWN_TARGETS

    isolated, project, apm = _setup(tmp_path, apm_binary_path)
    _write(
        isolated.work_root / "prims/.apm/skills/demo/SKILL.md",
        "---\nname: demo\ndescription: Demo skill\n---\nDemo.\n",
    )
    targets = sorted(
        name
        for name, profile in KNOWN_TARGETS.items()
        if not profile.requires_flag and (name == "cursor") == cursor
    )
    for name in targets:
        (project / KNOWN_TARGETS[name].root_dir).mkdir(parents=True, exist_ok=True)
    dump_yaml(
        {
            "name": "consumer",
            "version": "0.1.0",
            "targets": targets,
            "dependencies": {"apm": ["../prims"]},
        },
        project / "apm.yml",
    )

    apm("install", "--no-policy")

    lock = LockFile.read(project / "apm.lock.yaml")
    assert lock is not None
    (dependency,) = lock.dependencies.values()
    deployed = [
        project / relative
        for relative in dependency.deployed_files
        if (project / relative).is_file() and not (project / relative).is_symlink()
    ]
    kinds = {path.relative_to(project).parts[1] for path in deployed}
    assert {"hooks", "skills"} < kinds and len(kinds) > 3, sorted(map(str, deployed))
    receipts = _receipts(isolated)
    missing = [
        path.relative_to(project).as_posix()
        for path in deployed
        if receipts.get(path.resolve().as_posix()) != compute_file_hash(path)
    ]
    assert missing == []
