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
FIXTURE_036 = FIXTURES / "v0.36.1"
# Files whose 0.37 render differs from the bytes 0.35.1 wrote.
RE_RENDERED = (".codex/agents/editor.toml", ".cursor/rules/everywhere.mdc")
# A file whose 0.37 render equals the bytes 0.35.1 wrote.
UNCHANGED_RENDER = ".cursor/rules/python.mdc"
# An always-on rule both fixtures deployed; 0.38 routes it to AGENTS.md (#3939).
ROUTED_CLAUDE_RULE = ".claude/rules/everywhere.md"
_IGNORED = {"apm_modules", ".git", ".apm-local"}
_DEPLOY_ROOTS = (".agents/", ".claude/", ".codex/", ".cursor/")
_USER_EDIT = "\n# local note\n"


_REMEDY = (
    "Move it aside, then run 'apm install' to restore it, or 'apm export-patch' to export "
    "the local edits as upstream patches."
)


def _kept_line(path: str) -> str:
    """A file the pre-0.36 lock proves edited: its bytes differ from the recorded hash."""
    return f"Kept {path}: it was modified since APM deployed it. {_REMEDY}"


def _unproven_line(path: str) -> str:
    """A file nothing proves APM wrote (#4360): a forged lock hash is not evidence."""
    return (
        f"Kept {path}: it differs from what APM renders and there is no local deployment "
        f"receipt or locked render proving APM wrote it, so it may hold local edits. {_REMEDY}"
    )


class _World:
    def __init__(
        self, tmp_path: Path, apm_binary_path: Path, fixture: Path, git: str = "committed"
    ) -> None:
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
        if git == "none":
            return
        if git == "ignored":
            # A common setup: the deployed outputs are not committed.
            with (self.project / ".gitignore").open("a", encoding="utf-8") as handle:
                handle.write("\n".join(_DEPLOY_ROOTS) + "\n")
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


@pytest.mark.parametrize("git", ["committed", "nogit", "gitignored"])
def test_c1_unedited_pre036_files_re_render_silently_once(
    tmp_path: Path, apm_binary_path: Path, git: str
) -> None:
    """Committed, outside any repository, or with gitignored outputs: all upgrade."""
    expected = _fresh_render(tmp_path, apm_binary_path, FIXTURE_035)
    world = _World(
        tmp_path / "c1",
        apm_binary_path,
        FIXTURE_035,
        git={"committed": "committed", "nogit": "none", "gitignored": "ignored"}[git],
    )
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

    # The routed Claude rule loads from AGENTS.md only once compiled (#5344).
    world.ok("compile")
    after = world.snapshot()
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
    before = world.snapshot()

    code, out, err = world.run("install")

    assert code == expected_exit, f"{out}\n{err}"
    assert target.read_bytes() == edited_bytes
    assert _kept_lines(out + err) == [_kept_line(edited)]
    after = world.snapshot()
    if expected_exit:
        # #4381: a blocking edit refuses the whole install before any write.
        assert after == before
        target.rename(world.project.parent / "c2-aside")
        world.ok("install")
        after = world.snapshot()
    for path in RE_RENDERED:
        if path != edited:
            assert after[path] == expected[path], f"{path} did not converge"


def test_c4_frozen_install_of_untouched_pre036_project_writes_nothing(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _World(tmp_path / "c4", apm_binary_path, FIXTURE_035)
    before = world.snapshot()

    result = world.runner.run(("install", "--frozen"), cwd=world.project, env=world.env)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert _kept_lines(result.stdout + result.stderr) == []
    assert world.snapshot() == before
    # A file this version no longer renders keeps its lock claim under --frozen.
    squeezed = " ".join((result.stdout + result.stderr).split())
    assert "no longer deploys, because --frozen does not change apm.lock.yaml: " in squeezed
    assert ROUTED_CLAUDE_RULE in squeezed, squeezed


def _forge_lock_hash(world: _World, path: str) -> bytes:
    """Edit *path* and rewrite every lock hash recorded for it to the edited bytes."""
    from apm_cli.utils.content_hash import compute_file_hash

    target = world.project / path
    recorded = compute_file_hash(target)
    target.write_bytes(target.read_bytes() + _USER_EDIT.encode("ascii"))
    lock_path = world.project / "apm.lock.yaml"
    lock_text = lock_path.read_text(encoding="utf-8")
    assert lock_text.count(recorded) == 2, "expected the file hash and its ledger row"
    lock_path.write_text(lock_text.replace(recorded, compute_file_hash(target)), "utf-8")
    return target.read_bytes()


def _kept_entry(world: _World, path: str) -> tuple[str | None, list[str | None]]:
    """The lock's hash for *path* and every deployment ledger row hash naming it."""
    lock = load_yaml(world.project / "apm.lock.yaml") or {}
    (dependency,) = lock["dependencies"]
    rows = [
        row.get("content_hash") for row in lock.get("deployments") or [] if row.get("value") == path
    ]
    return dependency["deployed_file_hashes"].get(path), rows


def _assert_refused_before_write(
    world: _World,
    output: str,
    before: dict[str, bytes],
    edited_bytes: bytes,
) -> None:
    """#4381: a kept file without evidence refuses the install before any write.

    Every deployed file and the lockfile keep their bytes, nothing is said to
    be written, the two read-only verifiers agree, and either printed remedy
    converges.
    """
    target = world.project / _C3_EDITED
    assert world.snapshot() == before
    flat = " ".join(output.split())
    assert "This install wrote" not in flat, flat
    assert "No install transaction changes were committed." in flat, flat
    audit, frozen = world.run("audit", "--ci"), world.run("install", "--frozen")
    assert (audit[0], frozen[0]) == (1, 1), (audit, frozen)
    # Remedy: export the edit (RA-008: --force is no Kept remedy). This fixture's
    # package is a local path, whose source already lives on disk, so there is
    # nothing to export upstream; the command says so and keeps the edit.
    patches = world.project.parent / "kept-patches"
    exported = " ".join(world.ok("export-patch", "--out", str(patches)).split())
    assert "nothing to export upstream" in exported, exported
    assert target.read_bytes() == edited_bytes
    # Then move the kept file aside; the next install restores it and converges.
    aside = world.project.parent / "kept-aside"
    target.rename(aside)
    world.ok("install")
    _assert_lock_matches_disk(world)
    world.ok("install")
    assert aside.read_bytes() == edited_bytes


_C3_EDITED = RE_RENDERED[1]
_C3_GIT_STATES = {
    "tracked-dirty": (),
    "tracked-staged": (("add", "-A"),),
    "untracked": (
        ("rm", "-q", "--cached", "--", _C3_EDITED),
        ("commit", "-qm", "untrack"),
    ),
    "tracked-clean": (("add", "-A"), ("commit", "-qm", "edit")),
}


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("+")[0].split(".")[:3])


def _stamped_world(tmp_path: Path, apm_binary_path: Path, stamp: str) -> _World:
    """A committed project whose lock carries *stamp*: a released fixture, or the
    binary under test itself ("candidate", a lock stamped 0.37 or later)."""
    if stamp != "candidate":
        return _World(tmp_path / "c3", apm_binary_path, FIXTURES / stamp)
    world = _World(tmp_path / "c3", apm_binary_path, FIXTURE_036)
    for path in world.snapshot():
        if path not in {"apm.yml", ".gitignore"}:
            (world.project / path).unlink()
    world.ok("install")
    world._git("add", "-A")
    world._git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "v037")
    written_by = (load_yaml(world.project / "apm.lock.yaml") or {})["apm_version"]
    assert _version(str(written_by)) >= (0, 37, 0), written_by
    return world


@pytest.mark.parametrize(
    ("stamp", "git_state", "overwritten"),
    [
        ("v0.35.1", "tracked-dirty", False),
        ("v0.35.1", "tracked-staged", False),
        ("v0.35.1", "untracked", True),
        ("v0.35.1", "tracked-clean", True),
        ("v0.36.1", "tracked-dirty", False),
        ("v0.36.1", "tracked-clean", True),
        ("candidate", "tracked-dirty", False),
        ("candidate", "tracked-clean", False),
    ],
    ids=[
        "stamp035-dirty",
        "stamp035-staged",
        "stamp035-untracked",
        "stamp035-clean",
        "stamp036-dirty",
        "stamp036-clean",
        "stamp037-dirty",
        "stamp037-clean",
    ],
)
def test_c3_forged_lock_hash_overwrites_only_a_clean_tracked_pre037_file(
    tmp_path: Path, apm_binary_path: Path, stamp: str, git_state: str, overwritten: bool
) -> None:
    """A lock hash forged to match a local edit is evidence only for a lock stamped
    before 0.37, and only when git holds no uncommitted change to the file (tracked
    and clean, or untracked); a staged or unstaged change is kept."""
    fixture = FIXTURE_036 if stamp == "candidate" else FIXTURES / stamp
    expected = _fresh_render(tmp_path, apm_binary_path, fixture)
    world = _stamped_world(tmp_path, apm_binary_path, stamp)
    edited_bytes = _forge_lock_hash(world, _C3_EDITED)
    assert edited_bytes != expected[_C3_EDITED]
    for args in _C3_GIT_STATES[git_state]:
        world._git("-c", "user.email=t@example.com", "-c", "user.name=t", *args)
    git_status = world._git("status", "--porcelain", "--", _C3_EDITED)
    assert (git_status == "") is (git_state == "tracked-clean"), git_status

    before = world.snapshot()

    code, out, err = world.run("install")

    target = world.project / _C3_EDITED
    if overwritten:
        assert code == 0, f"{out}\n{err}"
        assert _kept_lines(out + err) == []
        assert target.read_bytes() == expected[_C3_EDITED]
        if git_state == "tracked-clean":
            # The overwritten edit stays recoverable from git.
            assert world._git("show", f"HEAD:{_C3_EDITED}").encode("utf-8") == edited_bytes
        _assert_lock_matches_disk(world)
    else:
        assert code == 1, f"{out}\n{err}"
        assert target.read_bytes() == edited_bytes
        # The bytes equal the (forged) lock hash, so no edit is proven; without
        # evidence the file is kept under the #4360 wording.
        assert _kept_lines(out + err) == [_unproven_line(_C3_EDITED)]
        assert world._git("status", "--porcelain", "--", _C3_EDITED) == git_status
        _assert_refused_before_write(world, out + err, before, edited_bytes)


def test_c3b_half_forged_lock_hash_follows_the_deployment_ledger(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A lock whose two hash copies disagree proves nothing past the ledger's record.

    Forging only ``deployed_file_hashes`` (not the deployment ledger row) leaves
    the lock inconsistent. The ledger owns deployment evidence, so the edited
    file has none: the install refuses before any write and leaves the lock as
    it is (#4381), and both read-only verifiers report the edit.
    """
    from apm_cli.utils.content_hash import compute_file_hash

    world = _stamped_world(tmp_path, apm_binary_path, "v0.36.1")
    target = world.project / _C3_EDITED
    recorded = compute_file_hash(target)
    target.write_bytes(target.read_bytes() + _USER_EDIT.encode("ascii"))
    edited_bytes = target.read_bytes()
    lock_path = world.project / "apm.lock.yaml"
    lock_text = lock_path.read_text(encoding="utf-8")
    pattern = f"{_C3_EDITED}: {recorded}"
    assert lock_text.count(pattern) == 1, "expected one deployed_file_hashes line"
    lock_path.write_text(
        lock_text.replace(pattern, f"{_C3_EDITED}: {compute_file_hash(target)}"), "utf-8"
    )
    _hash_before, rows_before = _kept_entry(world, _C3_EDITED)
    assert rows_before == [recorded], rows_before
    before = world.snapshot()

    code, out, err = world.run("install")

    assert code == 1, f"{out}\n{err}"
    assert target.read_bytes() == edited_bytes
    assert world.snapshot() == before
    assert _kept_lines(err) == [_unproven_line(_C3_EDITED)]
    assert _kept_lines(out) == []
    flat = " ".join((out + err).split())
    assert "This install wrote" not in flat, flat
    assert "No install transaction changes were committed." in flat, flat
    audit, frozen = world.run("audit", "--ci"), world.run("install", "--frozen")
    assert (audit[0], frozen[0]) == (1, 1), (audit, frozen)
    assert target.read_bytes() == edited_bytes
    # Move-aside remedy: the next install restores the render and converges.
    target.rename(world.project.parent / "kept-aside")
    world.ok("install")
    _assert_lock_matches_disk(world)


_SINGLE = ".github/instructions/tools.instructions.md"


def test_c3c_nothing_written_kept_line_says_what_the_lock_did(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A refused install names the kept file and changes no byte, lock included.

    The red team's upg.sh 0.36.1 world: one Copilot file, a v0.36.1 lock
    (lockfile_version 1, real release binary), edited after install. Only
    deployed_file_hashes is forged to the edit. The install refuses before any
    write (#4381): the v1 lock is not rewritten, and a repeat refuses the same
    way. (The unforged upgrade is c3d.)
    """
    from apm_cli.utils.content_hash import compute_file_hash

    world = _World(tmp_path / "c3c", apm_binary_path, FIXTURES / "v0.36.1-copilot-single")
    target = world.project / _SINGLE
    lock_path = world.project / "apm.lock.yaml"
    assert lock_path.read_text(encoding="utf-8").startswith("lockfile_version: '1'\n")
    recorded = compute_file_hash(target)
    target.write_bytes(target.read_bytes() + _USER_EDIT.encode("ascii"))
    edited_bytes = target.read_bytes()
    text = lock_path.read_text(encoding="utf-8")
    pattern = f"{_SINGLE}: {recorded}"
    assert text.count(pattern) == 1, text
    lock_path.write_text(text.replace(pattern, f"{_SINGLE}: {compute_file_hash(target)}"), "utf-8")
    lock_before = lock_path.read_bytes()
    for _ in range(2):
        code, out, err = world.run("install")
        assert code == 1, f"{out}\n{err}"
        assert target.read_bytes() == edited_bytes
        assert lock_path.read_bytes() == lock_before
        assert _kept_lines(err) == [_unproven_line(_SINGLE)]
        assert _kept_lines(out) == []
        flat = " ".join((out + err).split())
        assert "This install wrote" not in flat, flat
        assert "is rewritten in the current format" not in flat, flat
        assert flat.endswith("No install transaction changes were committed."), flat


def test_c3d_upgrade_keeps_the_edit_proof_on_every_install(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """RA-012/RA-010: the v1 -> 4 lock upgrade keeps the evidence for a kept edit.

    The red team's upg.sh 0.36.1 world, no forge. The 0.36.1 lock proves the
    user edited the file; the upgrade install keeps it as a proven drift and
    receipts the bytes 0.36.1 deployed, so every later install (lock now
    stamped 0.37) proves the same edit: the same reason line and exit code.
    Both read-only verifiers flag the drift (I3), and the printed remedies
    converge.
    """
    world = _World(tmp_path / "c3d", apm_binary_path, FIXTURES / "v0.36.1-copilot-single")
    target = world.project / _SINGLE
    lock_path = world.project / "apm.lock.yaml"
    assert lock_path.read_text(encoding="utf-8").startswith("lockfile_version: '1'\n")
    target.write_bytes(target.read_bytes() + _USER_EDIT.encode("ascii"))
    edited_bytes = target.read_bytes()

    verdicts = []
    for _ in range(3):
        code, out, err = world.run("install")
        assert target.read_bytes() == edited_bytes
        verdicts.append((code, _kept_lines(out), _kept_lines(err)))
    assert verdicts == [(0, [_kept_line(_SINGLE)], [])] * 3, verdicts
    assert lock_path.read_text(encoding="utf-8").startswith("lockfile_version: '4'\n")

    audit, frozen = world.run("audit", "--ci"), world.run("install", "--frozen")
    assert (audit[0], frozen[0]) == (1, 1), (audit, frozen)
    assert _SINGLE in audit[1] + audit[2], audit
    # RA-044: the content-integrity remedy is the Kept line's, never a delete.
    flat_audit = " ".join((audit[1] + audit[2]).split())
    assert (
        "1 file(s) with hash drift -- move the edited files aside, then run 'apm install' "
        "to restore the locked content, or 'apm export-patch' to export the local edits as "
        "upstream patches"
    ) in flat_audit, flat_audit
    assert "delete" not in flat_audit.lower(), flat_audit
    assert target.read_bytes() == edited_bytes
    # Remedy: export the edit. The package is a local path whose source already
    # lives on disk, so there is nothing to export upstream; the edit is kept.
    patches = world.project.parent / "c3d-patches"
    exported = " ".join(world.ok("export-patch", "--out", str(patches)).split())
    assert "nothing to export upstream" in exported, exported
    assert target.read_bytes() == edited_bytes
    # Then move the kept file aside; the next install restores it and converges.
    aside = world.project.parent / "c3d-aside"
    target.rename(aside)
    world.ok("install")
    _assert_lock_matches_disk(world)
    world.ok("audit", "--ci")
    assert _kept_lines(world.ok("install")) == []
    assert aside.read_bytes() == edited_bytes


def test_c5_unedited_036_project_upgrades_unchanged(tmp_path: Path, apm_binary_path: Path) -> None:
    """A project written by a released 0.36.x keeps every deployed byte on upgrade."""
    produced_by = json.loads((FIXTURE_036 / "FIXTURE.json").read_text())["apm_version"]
    assert produced_by.startswith("0.36."), produced_by
    expected = _fresh_render(tmp_path, apm_binary_path, FIXTURE_036)
    world = _World(tmp_path / "c5", apm_binary_path, FIXTURE_036)
    before = world.snapshot()
    recorded = sorted(p for p in before if p not in {"apm.yml", "apm.lock.yaml", ".gitignore"})
    # The always-on rule Claude now loads from AGENTS.md is the only file 0.38 drops (#3939).
    assert sorted(set(recorded) - set(expected)) == [ROUTED_CLAUDE_RULE]
    deployed = [p for p in recorded if p != ROUTED_CLAUDE_RULE]
    assert deployed and all(before[p] == expected[p] for p in deployed)

    code, out, err = world.run("install")

    assert code == 0, f"{out}\n{err}"
    assert _kept_lines(out + err) == []
    after = world.snapshot()
    assert {p: after.get(p) for p in deployed} == {p: before[p] for p in deployed}
    assert set(after) == set(before) - {ROUTED_CLAUDE_RULE}
    # The routed Claude rule loads from AGENTS.md only once compiled (#5344).
    world.ok("compile")
    after = world.snapshot()
    world.ok("audit", "--ci")
    world.ok("install", "--frozen")
    assert world.snapshot() == after
    world.ok("install")
    assert world.snapshot() == after


def test_c5b_036_stamped_project_whose_render_changed_converges(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A lock stamped 0.36 is migrated like any lock before 0.37: files whose bytes
    equal the recorded hash but differ from the 0.37 render re-render silently.

    No file primitive renders differently between released 0.36.1 and 0.37 (the
    v0.36.1 fixture renders identically, see C5), so the 0.35.1 renders are
    restamped 0.36.1 to put a changed render under a 0.36 lock.
    """
    expected = _fresh_render(tmp_path, apm_binary_path, FIXTURE_035)
    world = _World(tmp_path / "c5b", apm_binary_path, FIXTURE_035)
    lock_path = world.project / "apm.lock.yaml"
    lock_text = lock_path.read_text(encoding="utf-8")
    assert lock_text.count("apm_version: 0.35.1\n") == 1
    lock_path.write_text(lock_text.replace("apm_version: 0.35.1\n", "apm_version: 0.36.1\n"))
    world._git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qam", "v036")
    before = world.snapshot()
    for path in RE_RENDERED:
        assert before[path] != expected[path], f"fixture no longer exercises {path}"

    code, out, err = world.run("install")

    assert code == 0, f"{out}\n{err}"
    assert _kept_lines(out + err) == []
    after = world.snapshot()
    deployed = {p for p in expected if p not in {"apm.yml", "apm.lock.yaml", ".gitignore"}}
    assert {p: after.get(p) for p in deployed} == {p: expected[p] for p in deployed}
    _assert_lock_matches_disk(world)
    # The routed Claude rule loads from AGENTS.md only once compiled (#5344).
    world.ok("compile")
    after = world.snapshot()
    world.ok("audit", "--ci")
    world.ok("install", "--frozen")
    assert world.snapshot() == after
    world.ok("install")
    assert world.snapshot() == after


# The most common real upgrade (0.36.1 -> 0.37), from a released 0.36.1 binary:
# Copilot project MCP moves from .github/mcp.json to the root .mcp.json (#3866),
# and the package now ships a Kiro agent that names an MCP server with
# ``@server``, which 0.36.1 refused and 0.37 deploys.
FIXTURE_036_COPILOT_KIRO = FIXTURES / "v0.36.1-copilot-kiro"
_SERVER_AGENT = ".apm/agents/notes-agent.agent.md"
_SERVER_AGENT_TEXT = (
    "---\nname: notes-agent\ndescription: Uses the notes server\n"
    'tools: ["read", "@notes"]\n---\nYou take notes.\n'
)
_LEGACY_COPILOT_MCP = ".github/mcp.json"


def _add_server_agent(world: _World) -> None:
    (world.project.parent / "pkg" / _SERVER_AGENT).write_text(_SERVER_AGENT_TEXT, encoding="ascii")


def _mcp_servers(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {}


def test_c6_036_copilot_kiro_mcp_project_converges_to_the_037_layout(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    fixture = FIXTURE_036_COPILOT_KIRO
    assert json.loads((fixture / "FIXTURE.json").read_text())["apm_version"] == "0.36.1"
    assert "notes" in _mcp_servers(fixture / "project" / _LEGACY_COPILOT_MCP)

    fresh = _World(tmp_path / "fresh", apm_binary_path, fixture)
    _add_server_agent(fresh)
    for path in fresh.snapshot():
        if path not in {"apm.yml", ".gitignore"}:
            (fresh.project / path).unlink()
    fresh.ok("install")
    expected = fresh.snapshot()
    assert _LEGACY_COPILOT_MCP not in expected

    world = _World(tmp_path / "c6", apm_binary_path, fixture)
    _add_server_agent(world)

    code, out, err = world.run("install")

    assert code == 0, f"{out}\n{err}"
    assert _kept_lines(out + err) == []
    after = world.snapshot()
    for path, content in expected.items():
        if path != "apm.lock.yaml":
            assert after.get(path) == content, path
    # APM created .github/mcp.json and the move empties it, so it is deleted
    # and named in the move summary (#4524).
    assert set(after) == set(expected)
    assert not (world.project / _LEGACY_COPILOT_MCP).exists()
    summary = " ".join((out + err).split())
    assert (
        f"Removed empty {_LEGACY_COPILOT_MCP} (APM created it and no longer writes it)" in summary
    )
    assert _mcp_servers(world.project / ".mcp.json")["notes"] == {
        "type": "stdio",
        "command": "node",
        "args": ["server.js"],
    }
    assert "@notes" in (world.project / ".kiro/agents/notes-agent.md").read_text()
    _assert_lock_matches_disk(world)

    world.ok("audit", "--ci")
    world.ok("install", "--frozen")
    assert world.snapshot() == after
    world.ok("install")
    assert world.snapshot() == after


# #4524: a user-created .github/mcp.json that also holds APM's entry. The
# Copilot MCP move removes only APM's entry and keeps the file: its parsed
# content is the user's file minus that entry, the user's server is unchanged
# and is not moved to .mcp.json, and a repeat run changes no bytes.
_USER_SERVER = {"type": "local", "command": "uvx", "args": ["my-server"], "tools": ["*"]}


_USER_ONLY_MCP_JSON = (
    b'{\n    "mcpServers": {\n        "mine": {\n            "type": "local",\n'
    b'            "command": "uvx",\n            "args": [\n                "my-server"\n'
    b'            ],\n            "tools": [\n                "*"\n            ]\n'
    b"        }\n    }\n}\n"
)


def _user_mcp_json(servers: dict) -> bytes:
    """A hand-written file in the user's own style (4-space indent, final newline)."""
    return (json.dumps({"mcpServers": servers}, indent=4) + "\n").encode("utf-8")


def test_c7_036_user_created_copilot_mcp_file_keeps_user_server_on_upgrade(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _World(tmp_path / "c7", apm_binary_path, FIXTURE_036_COPILOT_KIRO)
    legacy = world.project / _LEGACY_COPILOT_MCP
    apm_entry = _mcp_servers(legacy)["notes"]
    legacy.write_bytes(_user_mcp_json({"mine": _USER_SERVER, "notes": apm_entry}))

    code, out, err = world.run("install")

    assert code == 0, f"{out}\n{err}"
    assert legacy.is_file()
    # Byte-identical to the user's file minus APM's entry (#4526), hand-written.
    assert legacy.read_bytes() == _USER_ONLY_MCP_JSON
    root_servers = _mcp_servers(world.project / ".mcp.json")
    assert "mine" not in root_servers
    assert root_servers["notes"] == {"type": "stdio", "command": "node", "args": ["server.js"]}
    _assert_lock_matches_disk(world)
    after = world.snapshot()

    world.ok("audit", "--ci")
    world.ok("install", "--frozen")
    assert world.snapshot() == after
    world.ok("install")
    assert world.snapshot() == after
