"""RT3 v0.39 red team: deploy and uninstall through junctions on the real CLI.

A deploy target or a deployed directory swapped for a junction to a directory
outside the project must never receive APM writes or lose files to APM
deletes. Directory symlinks stand in for junctions off Windows.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I1"),
]

_SKILL = "---\nname: a-skill\ndescription: Skill of a\n---\n# a\n"


def _link_dir(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _world(tmp_path: Path, apm_binary_path: Path) -> tuple[ReplayWorld, Path]:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    pkg = world.project / "vendor" / "a"
    (pkg / ".apm" / "skills" / "a-skill").mkdir(parents=True)
    (pkg / "apm.yml").write_text("name: a\nversion: 1.0.0\n", encoding="utf-8")
    (pkg / ".apm" / "skills" / "a-skill" / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./vendor/a"]},
        },
        world.project / "apm.yml",
    )
    outside = tmp_path / "outside-victim"
    outside.mkdir()
    return world, outside


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.mark.parametrize("hop", [".claude", ".claude/skills"])
def test_rt3_deploy_through_a_junction_never_writes_outside(
    tmp_path: Path, apm_binary_path: Path, hop: str
) -> None:
    world, outside = _world(tmp_path, apm_binary_path)
    link = world.project / hop
    link.parent.mkdir(parents=True, exist_ok=True)
    _link_dir(link, outside)

    result = world.apm("install", expect=None)

    assert _snapshot(outside) == {}, (
        f"apm install wrote through {hop} -> outside (exit {result.returncode}):\n"
        f"{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
    )


@pytest.mark.parametrize("command", ["uninstall", "prune"])
def test_rt3_removal_through_a_swapped_junction_never_deletes_outside(
    tmp_path: Path, apm_binary_path: Path, command: str
) -> None:
    world, outside = _world(tmp_path, apm_binary_path)
    world.apm("install")
    deployed = world.project / ".claude" / "skills" / "a-skill"
    assert (deployed / "SKILL.md").is_file(), "fixture: the skill must deploy"
    # The attacker (or a user tool) swaps the deployed dir for a junction to a
    # directory holding byte-identical "deployed" content plus a bystander.
    shutil.copytree(deployed, outside, dirs_exist_ok=True)
    (outside / "bystander.txt").write_text("keep me", encoding="utf-8")
    shutil.rmtree(deployed)
    _link_dir(deployed, outside)
    before = _snapshot(outside)

    if command == "uninstall":
        result = world.apm("uninstall", "./vendor/a", expect=None)
    else:
        dump_yaml(
            {
                "name": "proj",
                "version": "1.0.0",
                "targets": ["claude"],
                "dependencies": {"apm": []},
            },
            world.project / "apm.yml",
        )
        result = world.apm("prune", expect=None)

    assert _snapshot(outside) == before, (
        f"apm {command} deleted through a junction (exit {result.returncode}):\n"
        f"{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
    )


def test_rt3_case_only_rename_of_a_deployed_dir_then_uninstall(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, _outside = _world(tmp_path, apm_binary_path)
    world.apm("install")
    skills = world.project / ".claude" / "skills"
    if not (skills / "A-SKILL").exists():
        pytest.skip("case-sensitive filesystem")
    (skills / "a-skill").rename(skills / "tmp-case")
    (skills / "tmp-case").rename(skills / "A-SKILL")

    result = world.apm("uninstall", "./vendor/a", expect=None)

    leftovers = sorted(p.name for p in skills.iterdir()) if skills.is_dir() else []
    assert result.returncode == 0, f"{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
    assert leftovers == [], f"left behind after a case-only rename: {leftovers}"


_WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing modes")

_HOLDER = """
import ctypes, sys, time
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.CreateFileW.restype = ctypes.c_void_p
h = k.CreateFileW(sys.argv[1], 0x80000000, 0x1, None, 3, 0x80, None)  # GENERIC_READ, SHARE_READ
print("held" if h not in (None, ctypes.c_void_p(-1).value) else "fail", flush=True)
time.sleep(float(sys.argv[2]))
"""


def _hold(path: Path, seconds: float = 20):
    """Another process (an editor, an AV scanner) holds *path* without delete sharing."""
    import subprocess

    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path), str(seconds)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
    return proc


@_WINDOWS
def test_rt3_uninstall_with_a_locked_deployed_file_recovers(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, _outside = _world(tmp_path, apm_binary_path)
    world.apm("install")
    skill = world.project / ".claude" / "skills" / "a-skill" / "SKILL.md"
    holder = _hold(skill)
    try:
        first = world.apm("uninstall", "./vendor/a", expect=None)
    finally:
        holder.kill()
        holder.wait()
    manifest = (world.project / "apm.yml").read_text(encoding="utf-8")
    lock = (world.project / "apm.lock.yaml").read_text(encoding="utf-8")
    print(
        f"first uninstall exit {first.returncode}\n{first.stdout[-2500:]}\n{first.stderr[-1500:]}"
    )
    print(
        f"after: skill exists={skill.exists()} manifest lists a={'vendor/a' in manifest} "
        f"lock lists a={'vendor/a' in lock}"
    )
    if skill.exists():
        # The file survived the lock: whatever owns it must still say so, or
        # nothing can ever clean it up (a silent orphan).
        assert "vendor/a" in manifest or "vendor/a" in lock, "locked file orphaned"
    again = world.apm("uninstall", "./vendor/a", expect=None) if skill.exists() else first
    assert not skill.exists(), (
        f"retry did not recover: exit {again.returncode}\n{again.stdout[-1500:]}\n"
        f"{again.stderr[-1500:]}"
    )


@_WINDOWS
def test_rt3_reinstall_with_a_locked_module_file_keeps_a_consistent_state(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, _outside = _world(tmp_path, apm_binary_path)
    world.apm("install")
    source = world.project / "vendor" / "a" / ".apm" / "skills" / "a-skill" / "SKILL.md"
    source.write_text(_SKILL + "v2\n", encoding="utf-8")
    module_files = [
        p for p in (world.project / "apm_modules").rglob("SKILL.md") if p.is_file()
    ] or [world.project / ".claude" / "skills" / "a-skill" / "SKILL.md"]
    holder = _hold(module_files[0])
    try:
        first = world.apm("install", expect=None)
    finally:
        holder.kill()
        holder.wait()
    deployed = world.project / ".claude" / "skills" / "a-skill" / "SKILL.md"
    second = world.apm("install", expect=None)
    assert second.returncode == 0, f"{second.stdout[-1500:]}\n{second.stderr[-1500:]}"
    assert deployed.read_text(encoding="utf-8").endswith("v2\n"), (
        f"stale deploy after the lock cleared (first exit {first.returncode}):\n"
        f"{first.stdout[-1500:]}\n{first.stderr[-1500:]}"
    )


@_WINDOWS
def test_rt3_long_deployed_paths_install_and_uninstall(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world, _outside = _world(tmp_path, apm_binary_path)
    bundle = world.project / "vendor" / "a" / ".apm" / "skills" / "a-skill"
    deep = bundle
    while (
        len(str(world.project / ".claude" / "skills" / "a-skill"))
        + len(str(deep.relative_to(bundle)))
        < 300
    ):
        deep = deep / "long-component-name"
    try:
        deep.mkdir(parents=True)
        (deep / "ref.md").write_text("deep\n", encoding="utf-8")
    except OSError as exc:
        pytest.skip(f"long paths unavailable on this runner: {exc}")

    first = world.apm("install", expect=None)
    deployed = world.project / ".claude" / "skills" / "a-skill" / deep.relative_to(bundle)
    if first.returncode == 0:
        assert (deployed / "ref.md").is_file(), "install passed without deploying the deep file"
    removal = world.apm("uninstall", "./vendor/a", expect=None)
    assert removal.returncode == 0, f"{removal.stdout[-1500:]}\n{removal.stderr[-1500:]}"
    assert not (world.project / ".claude" / "skills" / "a-skill").exists(), (
        f"uninstall left the long tree (install exit {first.returncode}):\n"
        f"{first.stdout[-800:]}\n{removal.stdout[-800:]}"
    )


@_WINDOWS
def test_rt3_short_name_cwd_then_long_name_cwd_agree(tmp_path: Path, apm_binary_path: Path) -> None:
    import ctypes

    world, _outside = _world(tmp_path, apm_binary_path)
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(world.project), buffer, 1024)
    short = Path(buffer.value)
    if "~" not in str(short):
        pytest.skip("no 8.3 alias on this volume")

    world.apm("install", cwd=short)
    lock = (world.project / "apm.lock.yaml").read_bytes()
    again = world.apm("install", cwd=world.project)
    assert again.returncode == 0
    assert (world.project / "apm.lock.yaml").read_bytes() == lock, (
        "lockfile churned by cwd spelling"
    )
    world.apm("uninstall", "./vendor/a", cwd=world.project)
    assert not (world.project / ".claude" / "skills" / "a-skill").exists()
