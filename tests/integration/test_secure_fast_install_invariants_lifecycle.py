"""APMLifecycle: invariants the secure-fast fresh install (#4939) must keep.

The 2x fresh-install design (W3d, "secure-and-fast fresh install", section 0)
replaces today's per-path containment proof and per-command checkout re-read
with cheaper enforcement. These cells pin the two invariants a naive speed-up
would break:

* PROMISE: P-INV-I5 -- an install writes only inside its deploy root, even
  when a concurrent process swaps a planned parent directory for a symbolic
  link after every check and before the write (write-time containment; a
  proof made only at plan time loses this race). Main proves containment
  before the write and then writes through the path, so this cell is an
  expected failure until the build lands (``xfail(strict=True)``: the build
  turns it green, and a green run before that fails the suite).
* PROMISE: P-OWN-package-content-hash -- a cached checkout byte changed in
  place, with its size kept and its modification time restored, is never
  deployed: every command re-hashes what it deploys against the lock pin
  (R-NTC), so a speed-up that trusts a stat fingerprint (size and mtime)
  fails this cell. Green on main; it must stay green.

The race is deterministic: ``tests/integration/_deploy_swap/sitecustomize.py``
swaps the parent right after the write seam records the path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.integration._mcp_chain_kit import Chain
from tests.integration.test_virtual_skill_audit_e2e import _install_virtual_skill_lifecycle
from tests.lifecycle import driver
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I5", "P-OWN-package-content-hash"),
]

_SWAP_HOOK = Path(__file__).with_name("_deploy_swap")
_AGENT = ".github/agents/pkg-agent.agent.md"
_PINNED = b"# Grilling"  # no newline: the fixture is CRLF on Windows
_TAMPERED = b"# Grillinx"
_VICTIM = b"# not APM's file\n"


def _evidence(result: CommandResult | subprocess.CompletedProcess) -> str:
    command = getattr(result, "command", None) or getattr(result, "args", None)
    return (
        f"command={command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _files_under(root: Path) -> list[str]:
    """Every entry below *root*, relative and sorted (links are not followed)."""
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def _write_local_package(project: Path) -> None:
    agents = project / "pkg" / ".apm" / "agents"
    agents.mkdir(parents=True)
    manifest = {"name": "pkg", "version": "1.0.0", "description": "containment fixture"}
    (project / "pkg" / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (agents / "pkg-agent.agent.md").write_text(
        '---\ndescription: "containment fixture agent"\n---\n# pkg agent\n', encoding="utf-8"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink race")
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#5461 secure-fast install not built yet (#4939 closed): write-time containment "
    "(design section 0.3 cell 4)",
)
def test_a_parent_swapped_for_a_link_before_the_write_receives_nothing_outside(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """P-INV-I5: a planned parent that becomes a link before the write is never followed."""
    chain = Chain(tmp_path / "swap", apm_binary_path)
    chain.write_manifest([], apm=["./pkg"])
    _write_local_package(chain.project)
    user_dir = chain.project / Path(_AGENT).parent
    user_dir.mkdir(parents=True)
    (user_dir / "user.agent.md").write_text("# a user's own agent\n", encoding="utf-8")
    outside = chain.isolated.root / "outside"
    outside.mkdir()
    # What the link exposes: a file of the deployed name that is not APM's.
    victim = outside / Path(_AGENT).name
    victim.write_bytes(_VICTIM)

    env = {**chain.env, "APM_TEST_SWAP_PATH": _AGENT, "APM_TEST_SWAP_TO": str(outside)}
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(_SWAP_HOOK), env.get("PYTHONPATH", "")) if part
    )
    command = (*driver.driver_command("engine"), "install", "--target", "copilot")
    result = subprocess.run(
        command, cwd=chain.project, env=env, capture_output=True, text=True, timeout=180
    )

    if not user_dir.is_symlink():
        # Not an assertion: a fixture that never raced must not pass as the expected failure.
        raise RuntimeError(f"the swap never ran; is {_AGENT} still deployed?\n{_evidence(result)}")
    assert _files_under(outside) == [victim.name], _evidence(result)
    assert victim.read_bytes() == _VICTIM, _evidence(result)
    moved = user_dir.with_name(user_dir.name + ".moved")
    assert (moved / "user.agent.md").read_text(encoding="utf-8") == "# a user's own agent\n"
    # I7: a write that is not refused lands in the directory the install proved.
    assert result.returncode != 0 or (moved / Path(_AGENT).name).is_file(), _evidence(result)


def test_a_cached_checkout_byte_changed_with_its_mtime_restored_is_never_deployed(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """P-OWN-package-content-hash: install re-hashes cached bytes; a stat match proves nothing."""
    lifecycle = _install_virtual_skill_lifecycle(tmp_path, apm_binary_path)
    cached = [
        path
        for path in lifecycle.isolated.cache_root.rglob("SKILL.md")
        if path.is_file() and not path.is_symlink() and _PINNED in path.read_bytes()
    ]
    assert cached, f"no cached checkout of the skill under {lifecycle.isolated.cache_root}"
    for path in cached:
        before = path.stat()
        path.chmod(before.st_mode | 0o200)
        path.write_bytes(path.read_bytes().replace(_PINNED, _TAMPERED))
        path.chmod(before.st_mode)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    deployed = [
        path
        for path in lifecycle.project_root.rglob("SKILL.md")
        if "apm_modules" not in path.relative_to(lifecycle.project_root).parts
    ]
    assert deployed, "the skill was not deployed"
    for path in deployed:
        path.unlink()
    shutil.rmtree(lifecycle.project_root / "apm_modules")

    result = lifecycle.runner.run_sequence(
        (("install", "--target", "copilot", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="stat-preserving-cache-tamper-install",
        cwd=lifecycle.project_root,
        env=lifecycle.environment,
    )[0]

    for path in [*deployed, lifecycle.module_root / "SKILL.md"]:
        assert path.is_file(), _evidence(result)
        assert _PINNED in path.read_bytes(), _evidence(result)
    leaked = [
        path.relative_to(lifecycle.project_root).as_posix()
        for path in lifecycle.project_root.rglob("*")
        if path.is_file() and _TAMPERED in path.read_bytes()
    ]
    assert leaked == [], _evidence(result)
