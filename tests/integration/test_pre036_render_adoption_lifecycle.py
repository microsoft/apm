"""Projects written by APM 0.35 upgrade to 0.37 seamlessly (#4398).

Promise (0.37 tag gate, #4398 part 2): on the first 0.37 install of a project
whose ``apm.lock.yaml`` was written by APM 0.35 or earlier, a managed file
whose bytes equal the hash that lock recorded is re-rendered silently and
recorded under the 0.37 evidence model (local receipts plus a rewritten lock),
once. Differing bytes are a user edit: the file is kept byte-identical and
named on exactly one ``Kept`` line built by the shared kept-file template.

The fixture under ``tests/fixtures/pre036_upgrade/v0.35.1`` was produced by
the released apm-cli 0.35.1 wheel (see ``generate.py`` there and the version
recorded in ``FIXTURE.json``). Two of its files render differently in 0.37:
the Cursor rule for ``applyTo: "**"`` and the Codex agent with a relative link.
Every cell copies the project to a fresh path with no receipts and no
``apm_modules/``, as a teammate's clone of the committed project.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I1", "P-INV-I2", "P-INV-I3", "P-INV-I7"),
]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pre036_upgrade"
FIXTURE_035 = FIXTURES / "v0.35.1"
# Files whose 0.37 render differs from the bytes 0.35.1 wrote.
RE_RENDERED = (".codex/agents/editor.toml", ".cursor/rules/everywhere.mdc")
# A file whose 0.37 render equals the bytes 0.35.1 wrote.
UNCHANGED_RENDER = ".cursor/rules/python.mdc"
_IGNORED = {"apm_modules", ".git", ".apm-local"}
_USER_EDIT = "\n# local note\n"


def _kept_line(path: str) -> str:
    return (
        f"Kept {path}: it was modified since APM deployed it. Move it aside, then run "
        "'apm install' to restore it, or 'apm install --force' to overwrite it."
    )


class _World:
    def __init__(self, tmp_path: Path, apm_binary_path: Path, fixture: Path) -> None:
        self.env_root = IsolatedApmEnvironment.create(
            tmp_path / "isolated", base_env=dict(os.environ)
        )
        self.env = {**self.env_root.subprocess_env(), "COLUMNS": "400"}
        self.home = Path(self.env["HOME"])
        clone = self.env_root.work_root / "clone"
        shutil.copytree(fixture / "pkg", clone / "pkg")
        shutil.copytree(fixture / "project", clone / "project")
        self.project = clone / "project"
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=240)
        self._git("init", "-q")
        self._git("add", "-A")
        self._git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "v035")

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.project,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def run(self, *args: str) -> tuple[int, str, str]:
        result = self.runner.run(args, cwd=self.project, env=self.env)
        return result.returncode, result.stdout, result.stderr

    def ok(self, *args: str) -> str:
        code, out, err = self.run(*args)
        assert code == 0, f"apm {' '.join(args)} exited {code}\n{out}\n{err}"
        return out + err

    def snapshot(self) -> dict[str, bytes]:
        state: dict[str, bytes] = {}
        for path in sorted(self.project.rglob("*")):
            relative = path.relative_to(self.project)
            if relative.parts[0] in _IGNORED or not path.is_file():
                continue
            state[relative.as_posix()] = path.read_bytes()
        return state

    def receipts(self) -> list[Path]:
        apm_home = Path(self.env.get("APM_HOME", self.home / ".apm"))
        return sorted(p for p in apm_home.rglob("*") if p.is_file() and "receipt" in str(p))


def _fresh_render(tmp_path: Path, apm_binary_path: Path, fixture: Path) -> dict[str, bytes]:
    """What the binary under test writes for the same manifest on a blank project."""
    world = _World(tmp_path / "fresh", apm_binary_path, fixture)
    for path in world.snapshot():
        if path not in {"apm.yml", ".gitignore"}:
            (world.project / path).unlink()
    world.ok("install")
    return world.snapshot()


def _kept_lines(output: str) -> list[str]:
    lines = (" ".join(line.split()) for line in output.splitlines() if "Kept " in line)
    return [line[line.index("Kept ") :] for line in lines]


def _assert_lock_matches_disk(world: _World) -> None:
    from apm_cli.utils.content_hash import compute_file_hash

    lock = load_yaml(world.project / "apm.lock.yaml") or {}
    assert (
        lock.get("apm_version")
        != json.loads((FIXTURE_035 / "FIXTURE.json").read_text())["apm_version"]
    )
    for dependency in lock.get("dependencies") or []:
        for path, digest in (dependency.get("deployed_file_hashes") or {}).items():
            assert compute_file_hash(world.project / path) == digest, path


def test_c1_unedited_pre036_files_re_render_silently_once(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    expected = _fresh_render(tmp_path, apm_binary_path, FIXTURE_035)
    world = _World(tmp_path / "c1", apm_binary_path, FIXTURE_035)
    before = world.snapshot()
    for path in RE_RENDERED:
        assert before[path] != expected[path], f"fixture no longer exercises {path}"

    code, out, err = world.run("install")

    assert code == 0, f"{out}\n{err}"
    assert _kept_lines(out + err) == []
    for path in RE_RENDERED:
        assert path not in out + err, f"re-render of {path} was not silent"
    after = world.snapshot()
    deployed = {p for p in expected if p not in {"apm.yml", "apm.lock.yaml", ".gitignore"}}
    for path in deployed:
        assert after.get(path) == expected[path], path
    assert after["apm.lock.yaml"] != before["apm.lock.yaml"]
    _assert_lock_matches_disk(world)
    assert world.receipts(), "the 0.37 evidence model records local receipts"

    world.ok("audit", "--ci")
    world.ok("install", "--frozen")
    assert world.snapshot() == after
    world.ok("install")
    assert world.snapshot() == after


@pytest.mark.parametrize(
    ("edited", "expected_exit"),
    [(RE_RENDERED[1], 1), (UNCHANGED_RENDER, 0)],
    ids=["edit-with-new-render", "edit-with-same-render"],
)
def test_c2_one_edited_pre036_file_is_kept_and_named_once(
    tmp_path: Path, apm_binary_path: Path, edited: str, expected_exit: int
) -> None:
    expected = _fresh_render(tmp_path, apm_binary_path, FIXTURE_035)
    world = _World(tmp_path / "c2", apm_binary_path, FIXTURE_035)
    target = world.project / edited
    target.write_bytes(target.read_bytes() + _USER_EDIT.encode("ascii"))
    edited_bytes = target.read_bytes()

    code, out, err = world.run("install")

    assert code == expected_exit, f"{out}\n{err}"
    assert target.read_bytes() == edited_bytes
    assert _kept_lines(out + err) == [_kept_line(edited)]
    after = world.snapshot()
    for path in RE_RENDERED:
        if path != edited:
            assert after[path] == expected[path], f"{path} did not converge"


def test_c4_frozen_install_of_untouched_pre036_project_writes_nothing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _World(tmp_path / "c4", apm_binary_path, FIXTURE_035)
    before = world.snapshot()

    code, out, err = world.run("install", "--frozen")

    assert code == 0, f"{out}\n{err}"
    assert _kept_lines(out + err) == []
    assert world.snapshot() == before
