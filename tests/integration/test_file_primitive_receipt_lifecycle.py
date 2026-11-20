"""Installed-CLI contract: every file primitive APM writes gets a local receipt (#3701).

Local deployment receipts used to cover only skills, so cleanup had no
evidence of deployment for instructions, prompts, agents and hooks beyond
a lockfile hash. Each final-rendered write now records a receipt in the
installation-bound session under ``APM_HOME``. A file that was already
byte-identical on disk and managed is adopted without a write and still gets a
receipt: its bytes are exactly APM's render. A differing file is never receipted.
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


def _drop_receipts(isolated: IsolatedApmEnvironment, project: Path) -> Path:
    """Emulate a fresh machine for committed output: no receipts, no checkout identity."""
    receipts_dir = isolated.home / ".apm" / "deployment-receipts"
    robust_rmtree(receipts_dir)
    robust_rmtree(project / ".apm-local")
    assert not receipts_dir.exists()
    return receipts_dir


def test_adopted_identical_files_get_a_receipt(tmp_path: Path, apm_binary_path: Path) -> None:
    """Committed, byte-identical managed output is adopted AND receipted (#4184, #3701).

    The bytes equal what APM renders, so the receipt states a fact; it restores
    the uninstall evidence a fresh clone otherwise lacks.
    """
    isolated, project, apm = _setup(tmp_path, apm_binary_path)
    apm("install", "--no-policy")
    _drop_receipts(isolated, project)

    adopted = apm("install", "--no-policy")

    assert "Kept " not in adopted, adopted
    receipts = _receipts(isolated)
    for relative in _ADOPTABLE:
        path = project / relative
        assert receipts.get(path.resolve().as_posix()) == compute_file_hash(path), (
            relative,
            receipts,
        )
    apm("audit", "--ci", "--no-policy")
    removed = apm("uninstall", "../prims")
    assert "Uninstall refused" not in removed, removed
    for relative in _ADOPTABLE:
        assert not (project / relative).exists(), relative


def _receipt_store(isolated: IsolatedApmEnvironment) -> dict[str, bytes]:
    store = isolated.home / ".apm" / "deployment-receipts"
    return {path.name: path.read_bytes() for path in sorted(store.glob("*.json"))}


def test_a_repeat_install_is_a_quiet_no_op(tmp_path: Path, apm_binary_path: Path) -> None:
    """Cell (a): after a 0.37 install, a repeat prints no adoption and changes nothing (I2)."""
    isolated, project, apm = _setup(tmp_path, apm_binary_path)
    apm("install", "--no-policy")
    store, lock = _receipt_store(isolated), (project / "apm.lock.yaml").read_bytes()

    repeat = apm("install", "--no-policy")

    assert "adopted" not in repeat, repeat
    assert _receipt_store(isolated) == store
    assert (project / "apm.lock.yaml").read_bytes() == lock


def test_a_receiptless_clone_install_receipts_every_file_quietly(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Cell (b): committed output with receipts evicted installs quietly and receipts all."""
    isolated, project, apm = _setup(tmp_path, apm_binary_path)
    apm("install", "--no-policy")
    _drop_receipts(isolated, project)

    installed = apm("install", "--no-policy")

    assert "adopted" not in installed, installed
    receipts = _receipts(isolated)
    for relative in _FILE_PRIMITIVES:
        path = project / relative
        assert receipts.get(path.resolve().as_posix()) == compute_file_hash(path), (
            relative,
            receipts,
        )
    apm("audit", "--ci", "--no-policy")


def test_a_repeat_install_shows_no_adoption_even_verbose(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """RA-007: receipted, unchanged files are unchanged, not adopted, in --verbose too."""
    isolated, _project, apm = _setup(tmp_path, apm_binary_path)
    apm("install", "--no-policy")
    store = _receipt_store(isolated)

    repeat = apm("install", "--no-policy", "--verbose")

    assert "adopted" not in repeat, repeat
    assert _receipt_store(isolated) == store


_EDITED = {
    "copilot": ".github/instructions/style.instructions.md",
    "cursor": ".cursor/rules/style.mdc",
    "skill": ".agents/skills/demo/SKILL.md",
}


@pytest.mark.parametrize("kind", sorted(_EDITED))
def test_an_edited_receipted_file_is_kept_with_exit_zero(
    tmp_path: Path, apm_binary_path: Path, kind: str
) -> None:
    """RA-012: an edit a receipt proves is kept and named, exit 0; parity with skills."""
    from apm_cli.integration.base_integrator import kept_edit_message

    isolated, project, _apm = _setup(tmp_path, apm_binary_path)
    _write(
        isolated.work_root / "prims/.apm/skills/demo/SKILL.md",
        "---\nname: demo\ndescription: Demo skill\n---\nDemo.\n",
    )
    targets = ["copilot", "cursor"]
    for name in targets:
        (project / (".github" if name == "copilot" else ".cursor")).mkdir(exist_ok=True)
    dump_yaml(
        {
            "name": "consumer",
            "version": "0.1.0",
            "targets": targets,
            "dependencies": {"apm": ["../prims"]},
        },
        project / "apm.yml",
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    env = isolated.subprocess_env()

    def install():
        return runner.run(("install", "--no-policy"), cwd=project, env=env)

    first = install()
    assert first.returncode == 0, first.stdout + first.stderr
    relative = _EDITED[kind]
    edited = project / relative
    edited.write_bytes(edited.read_bytes() + b"EDIT\n")
    edited_bytes = edited.read_bytes()

    kept = install()

    assert kept.returncode == 0, kept.stdout + kept.stderr
    message = kept_edit_message(relative, user_scope=False, proven_edit=True)
    assert message in " ".join(kept.stdout.split()), kept.stdout
    assert "Kept " not in kept.stderr, kept.stderr
    assert "no local deployment receipt" not in kept.stdout + kept.stderr
    assert edited.read_bytes() == edited_bytes
    store, lock = _receipt_store(isolated), (project / "apm.lock.yaml").read_bytes()
    repeat = install()
    assert repeat.returncode == 0, repeat.stdout + repeat.stderr
    assert edited.read_bytes() == edited_bytes
    assert _receipt_store(isolated) == store
    assert (project / "apm.lock.yaml").read_bytes() == lock


def test_a_differing_file_is_never_receipted_by_adoption(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Bytes that differ from the render are kept, never adopted into a receipt (#4360)."""
    isolated, project, apm = _setup(tmp_path, apm_binary_path)
    apm("install", "--no-policy")
    _drop_receipts(isolated, project)
    edited = project / _ADOPTABLE[0]
    edited.write_bytes(edited.read_bytes() + b"local edit\n")
    edited_bytes = edited.read_bytes()

    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    kept = runner.run(("install", "--no-policy"), cwd=project, env=isolated.subprocess_env())

    assert kept.returncode == 1, kept.stdout + kept.stderr
    assert f"Kept {_ADOPTABLE[0]}" in " ".join(kept.stderr.split()), kept.stderr
    assert edited.read_bytes() == edited_bytes
    assert edited.resolve().as_posix() not in _receipts(isolated)


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
