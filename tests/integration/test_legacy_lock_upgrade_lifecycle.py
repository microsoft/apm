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
_IGNORED = {"apm_modules", ".git", ".apm-local"}
_USER_EDIT = "\n# local note\n"


_REMEDY = (
    "Move it aside, then run 'apm install' to restore it, or 'apm install --force' to overwrite it."
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
        ("v0.35.1", "untracked", False),
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
    before 0.37, and only while git holds the edit (tracked and clean); otherwise
    the edit is kept."""
    fixture = FIXTURE_036 if stamp == "candidate" else FIXTURES / stamp
    expected = _fresh_render(tmp_path, apm_binary_path, fixture)
    world = _stamped_world(tmp_path, apm_binary_path, stamp)
    edited_bytes = _forge_lock_hash(world, _C3_EDITED)
    assert edited_bytes != expected[_C3_EDITED]
    for args in _C3_GIT_STATES[git_state]:
        world._git("-c", "user.email=t@example.com", "-c", "user.name=t", *args)
    git_status = world._git("status", "--porcelain", "--", _C3_EDITED)
    assert (git_status == "") is (git_state == "tracked-clean"), git_status

    code, out, err = world.run("install")

    target = world.project / _C3_EDITED
    if overwritten:
        assert code == 0, f"{out}\n{err}"
        assert _kept_lines(out + err) == []
        assert target.read_bytes() == expected[_C3_EDITED]
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


def test_c5_unedited_036_project_upgrades_unchanged(tmp_path: Path, apm_binary_path: Path) -> None:
    """A project written by a released 0.36.x keeps every deployed byte on upgrade."""
    produced_by = json.loads((FIXTURE_036 / "FIXTURE.json").read_text())["apm_version"]
    assert produced_by.startswith("0.36."), produced_by
    expected = _fresh_render(tmp_path, apm_binary_path, FIXTURE_036)
    world = _World(tmp_path / "c5", apm_binary_path, FIXTURE_036)
    before = world.snapshot()
    deployed = sorted(p for p in before if p not in {"apm.yml", "apm.lock.yaml", ".gitignore"})
    assert deployed and all(before[p] == expected[p] for p in deployed)

    code, out, err = world.run("install")

    assert code == 0, f"{out}\n{err}"
    assert _kept_lines(out + err) == []
    after = world.snapshot()
    assert {p: after.get(p) for p in deployed} == {p: before[p] for p in deployed}
    assert set(after) == set(before)
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
    assert json.loads(legacy.read_text(encoding="utf-8")) == {"mcpServers": {"mine": _USER_SERVER}}
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
