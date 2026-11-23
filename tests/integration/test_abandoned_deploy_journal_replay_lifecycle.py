"""APMLifecycle: the next install replays an abandoned deploy journal (#4863).

PROMISE: P-INV-I7 -- a refused install leaves durable state as it found it,
even when the process is killed between deploy and the refusal's restore.
``apm install`` journals the prior state of every path it deploys and
restores it when a later gate refuses (content scan, ``require_hashes``,
``unmanaged_files.action: deny``). If the process dies before that restore,
the next install, under the lifecycle lock and before anything else, replays
the abandoned journal: every journaled path is restored and ``apm audit --ci``
is clean.

The journal is data on disk and is trusted no further than the deployment
ledger: a path that cannot be restored, a malformed journal, a path escaping
the deploy root and copy bytes the ledger does not vouch for each keep the
journal, name it and stop the install without applying it.

The kill is deterministic: ``tests/integration/_deploy_kill/sitecustomize.py``
exits the engine process (``os._exit``) where the in-process restore would run.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from apm_cli.utils.content_hash import compute_file_hash
from tests.integration._mcp_chain_kit import Chain, stdio_server
from tests.lifecycle import driver
from tests.utils.apm_lifecycle_runner import CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I7"),
]

_KILL_HOOK = Path(__file__).with_name("_deploy_kill")
_KILLED_EXIT = 137
_JOURNAL = "journal.jsonl"
_HAND_WRITTEN = ".kiro/hand-written.md"
_HIDDEN_DESCRIPTION = "agent \\u202e helper"
_UNMANAGED_POLICY = (
    "name: acme\nversion: 1.0.0\nenforcement: block\n"
    "unmanaged_files:\n  action: deny\n  directories: ['.kiro']\n"
)


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _text(result: CommandResult) -> str:
    return " ".join(f"{result.stdout} {result.stderr}".split())


def _write_package(chain: Chain, *, revision: str, description: str, extra: bool) -> None:
    """A local package: an agent and a skill that change with *revision*, an optional extra."""
    root = chain.project / "pkg"
    agents = root / ".apm" / "agents"
    skill = root / ".apm" / "skills" / "pkg-skill"
    agents.mkdir(parents=True, exist_ok=True)
    skill.mkdir(parents=True, exist_ok=True)
    manifest = {"name": "pkg", "version": "1.0.0", "description": "journal replay fixture"}
    (root / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (agents / "pkg-agent.agent.md").write_text(
        f'---\ndescription: "{description}"\n---\n# pkg agent {revision}\n', encoding="utf-8"
    )
    (skill / "SKILL.md").write_text(
        f"---\nname: pkg-skill\ndescription: fixture skill\n---\n# pkg skill {revision}\n",
        encoding="utf-8",
    )
    extra_agent = agents / "extra-agent.agent.md"
    if extra:
        extra_agent.write_text(f"# extra agent {revision}\n", encoding="utf-8")
    elif extra_agent.exists():
        extra_agent.unlink()


def _tree_state(root: Path) -> dict[str, str]:
    """relpath -> sha256 (or ``<dir>``) for the committed project state."""
    skip = {".git", "pkg", "apm_modules", "apm.yml"}
    state: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] in skip:
            continue
        if path.is_dir() and not path.is_symlink():
            state[rel.as_posix()] = "<dir>"
        else:
            state[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return state


def _stores(chain: Chain) -> list[Path]:
    parent = Path(chain.env["APM_HOME"]) / "install-snapshots"
    return sorted(parent.iterdir()) if parent.is_dir() else []


def _abandoned(chain: Chain) -> Path:
    stores = [s for s in _stores(chain) if s.name.startswith("deploy-")]
    assert len(stores) == 1, stores
    return stores[0]


def _kill_mid_refusal(chain: Chain, fault: str, *args: str, **env: str) -> None:
    """Run ``apm install`` on the engine, killed where the refusal would restore.

    A killed process has no exit contract, so it bypasses the runner's output oracle.
    """
    env = {**chain.env, **env}
    env["APM_TEST_DEPLOY_FAULT"] = fault
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(_KILL_HOOK), env.get("PYTHONPATH", "")) if part
    )
    command = (*driver.driver_command("engine"), "install", *args)
    result = subprocess.run(
        command, cwd=chain.project, env=env, capture_output=True, text=True, timeout=180
    )
    assert result.returncode == _KILLED_EXIT, (command, result.stdout, result.stderr)


def _assert_lock_matches_disk(project: Path) -> None:
    """I1: every deployed path the lockfile records is on disk."""
    lock = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8")) or {}
    claims = {
        path for dep in lock.get("dependencies") or [] for path in dep.get("deployed_files") or []
    }
    claims.update(lock.get("local_deployed_files") or [])
    missing = sorted(path for path in claims if not (project / path).exists())
    assert missing == [], f"lockfile claims files not on disk: {missing}"


class _Gate:
    """One gate that refuses after deploy: the world, the offence and its cure."""

    def __init__(self, name: str, target: str, fault: str) -> None:
        self.name = name
        audit = ("--audit", "block") if name == "content-scan" else ()
        self.args = (*audit, "--target", target)
        self.fault = fault

    def world(self, tmp_path: Path, binary: Path) -> Chain:
        chain = Chain(tmp_path / self.name, binary)
        chain.write_manifest([], apm=["./pkg"])
        if self.name == "content-scan":
            chain.ok("experimental", "enable", "external_scanners")
        if self.name == "unmanaged-deny":
            chain.seed_org_policy(_UNMANAGED_POLICY)
        return chain

    def offend(self, chain: Chain) -> None:
        hidden = self.name == "content-scan"
        description = _HIDDEN_DESCRIPTION if hidden else "agent helper v2"
        _write_package(chain, revision="v2", description=description, extra=True)
        if self.name == "unmanaged-deny":
            path = chain.project / _HAND_WRITTEN
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# hand written\n", encoding="utf-8")

    def cure(self, chain: Chain) -> None:
        _write_package(chain, revision="v1", description="agent helper", extra=False)
        (chain.project / _HAND_WRITTEN).unlink(missing_ok=True)


_GATES = [
    _Gate("content-scan", "kiro", "kill"),
    _Gate("require-hashes", "kiro", "hash+kill"),
    _Gate("unmanaged-deny", "kiro", "kill"),
]


def _killed_world(tmp_path: Path, binary: Path, gate: _Gate) -> tuple[Chain, dict[str, str]]:
    """Install v1, then kill a refused v2 install between deploy and restore."""
    chain = gate.world(tmp_path, binary)
    _write_package(chain, revision="v1", description="agent helper", extra=False)
    chain.ok("install", *gate.args)
    before = _tree_state(chain.project)
    assert "apm.lock.yaml" in before, before

    gate.offend(chain)
    _kill_mid_refusal(chain, gate.fault, *gate.args)
    _abandoned(chain)
    leftover = _tree_state(chain.project)
    assert leftover != before, "the killed install deployed nothing"
    return chain, before


@pytest.mark.parametrize("gate", _GATES, ids=[gate.name for gate in _GATES])
def test_the_next_install_replays_a_journal_killed_before_a_gate_refusal(
    tmp_path: Path, apm_binary_path: Path, gate: _Gate
) -> None:
    chain, before = _killed_world(tmp_path, apm_binary_path, gate)
    gate.cure(chain)

    result = chain.run("install", *gate.args)

    assert result.returncode == 0, _evidence(result)
    assert _tree_state(chain.project) == before, _evidence(result)
    assert _stores(chain) == [], _evidence(result)
    assert "interrupted install" in _text(result), _evidence(result)
    assert _text(result).isascii(), _evidence(result)
    _assert_lock_matches_disk(chain.project)
    audit = chain.run("audit", "--ci")
    assert audit.returncode == 0, _evidence(audit)
    assert chain.run("install", *gate.args).returncode == 0
    assert _tree_state(chain.project) == before


def _journal(store: Path) -> list[dict]:
    """The manifest lines: a header, then one line per journaled path."""
    text = (store / _JOURNAL).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def _write_journal(store: Path, lines: list[dict]) -> None:
    text = "".join(json.dumps(line) + "\n" for line in lines)
    (store / _JOURNAL).write_text(text, encoding="utf-8")


def _unit(lines: list[dict], suffix: str) -> dict:
    found = [line for line in lines[1:] if line.get("path", "").endswith(suffix)]
    assert len(found) == 1, lines
    return found[0]


def _assert_kept_and_reported(
    chain: Chain, gate: _Gate, store: Path, named: str, *, applied: bool = False
) -> CommandResult:
    """The install stops, names *named* and keeps the journal.

    A journal replay cannot prove is not applied at all; a path that fails
    while a proven journal is applied leaves the other paths restored.
    """
    leftover = _tree_state(chain.project)
    result = chain.run("install", *gate.args)

    assert result.returncode == 1, _evidence(result)
    assert named in _text(result), _evidence(result)
    assert _text(result).isascii(), _evidence(result)
    if not applied:
        assert _tree_state(chain.project) == leftover, _evidence(result)
    kept = [s for s in _stores(chain) if s.name.endswith(store.name.removeprefix("deploy-"))]
    assert len(kept) == 1, _stores(chain)
    assert kept[0].name.startswith("retained-"), kept
    assert (kept[0] / _JOURNAL).is_file(), sorted(kept[0].iterdir())
    # The kept journal is not replayed again: the next install proceeds.
    return result


_SCAN = _GATES[0]


def test_an_unrestorable_path_keeps_the_journal_and_names_the_path(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain, _before = _killed_world(tmp_path, apm_binary_path, _SCAN)
    store = _abandoned(chain)
    journal = _journal(store)
    agent = _unit(journal, "pkg-agent.md")
    extra = _unit(journal, "extra-agent.md")
    # Its parent is now a regular file, so the prior bytes cannot be put back.
    parent = (chain.project / agent["path"]).parent
    shutil.rmtree(parent)
    parent.write_text("not a directory\n", encoding="utf-8")
    _SCAN.cure(chain)

    _assert_kept_and_reported(chain, _SCAN, store, agent["path"], applied=True)
    assert not (chain.project / extra["path"]).exists()


def test_a_malformed_journal_is_kept_and_reported_not_applied(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain, _before = _killed_world(tmp_path, apm_binary_path, _SCAN)
    store = _abandoned(chain)
    (store / _JOURNAL).write_text("{not json", encoding="utf-8")
    _SCAN.cure(chain)

    _assert_kept_and_reported(chain, _SCAN, store, store.name.removeprefix("deploy-"))


def test_a_journal_path_escaping_its_root_is_kept_and_reported_not_applied(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain, _before = _killed_world(tmp_path, apm_binary_path, _SCAN)
    store = _abandoned(chain)
    outside = chain.project.parent / "outside.md"
    outside.write_text("# outside the deploy root\n", encoding="utf-8")
    journal = _journal(store)
    escaping = "../outside.md"
    journal.append({"base": "root", "path": escaping, "kind": "absent", "copy": None})
    _write_journal(store, journal)
    _SCAN.cure(chain)

    _assert_kept_and_reported(chain, _SCAN, store, escaping)
    assert outside.read_text(encoding="utf-8") == "# outside the deploy root\n"


def test_journal_bytes_the_ledger_does_not_vouch_for_are_kept_not_restored(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain, _before = _killed_world(tmp_path, apm_binary_path, _SCAN)
    store = _abandoned(chain)
    journal = _journal(store)
    agent = _unit(journal, "pkg-agent.md")
    # A self-consistent journal: the copy re-hashes to its recorded hash, but
    # the ledger recorded other bytes for this path.
    forged = b"# forged prior bytes\n"
    (store / agent["copy"]).write_bytes(forged)
    agent["hash"] = compute_file_hash(store / agent["copy"])
    _write_journal(store, journal)
    _SCAN.cure(chain)

    _assert_kept_and_reported(chain, _SCAN, store, agent["path"])
    assert (chain.project / agent["path"]).read_bytes() != forged


def test_a_user_file_at_a_journaled_absent_path_survives_replay(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Replay removes only bytes the killed install wrote; a user's file there stays."""
    chain, before = _killed_world(tmp_path, apm_binary_path, _SCAN)
    extra = _unit(_journal(_abandoned(chain)), "extra-agent.md")
    assert extra["kind"] == "absent", extra
    user_file = chain.project / extra["path"]
    user_file.write_text("# my own agent, written after the crash\n", encoding="utf-8")
    _SCAN.cure(chain)

    result = chain.run("install", *_SCAN.args)

    assert result.returncode == 0, _evidence(result)
    assert user_file.read_text(encoding="utf-8") == "# my own agent, written after the crash\n"
    warning = f"Kept {extra['path']}"
    assert warning in _text(result), _evidence(result)
    assert _text(result).isascii(), _evidence(result)
    after = _tree_state(chain.project)
    assert {path for path in after if path not in before} == {extra["path"]}, after
    # The pre-install copies stay available, and no later install replays them.
    assert [s.name.split("-")[0] for s in _stores(chain)] == ["retained"], _stores(chain)
    assert "pre-install copies are kept at" in _text(result), _evidence(result)
    assert chain.run("install", *_SCAN.args).returncode == 0


def test_a_kill_after_the_lockfile_commit_settles_the_journal(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A journal whose lockfile reached disk recorded a committed install: keep it."""
    chain = _SCAN.world(tmp_path, apm_binary_path)
    _write_package(chain, revision="v1", description="agent helper", extra=False)
    chain.ok("install", *_SCAN.args)
    _write_package(chain, revision="v2", description="agent helper v2", extra=True)
    _kill_mid_refusal(chain, "settle+kill", *_SCAN.args)
    _abandoned(chain)
    committed = _tree_state(chain.project)

    result = chain.run("install", *_SCAN.args)

    assert result.returncode == 0, _evidence(result)
    assert _tree_state(chain.project) == committed, _evidence(result)
    assert "interrupted install" not in _text(result), _evidence(result)
    assert _stores(chain) == [], _evidence(result)
    _assert_lock_matches_disk(chain.project)
    assert chain.run("audit", "--ci").returncode == 0


def test_a_kill_mid_deploy_restores_every_journaled_path(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Killed before any gate seal: each path journaled before its write is restored."""
    chain = _SCAN.world(tmp_path, apm_binary_path)
    _write_package(chain, revision="v1", description="agent helper", extra=False)
    chain.ok("install", *_SCAN.args)
    before = _tree_state(chain.project)
    _write_package(chain, revision="v2", description="agent helper v2", extra=True)
    _kill_mid_refusal(chain, "deploy+kill", *_SCAN.args, APM_TEST_DEPLOY_KILL_AFTER="2")
    journal = _journal(_abandoned(chain))
    written = [line for line in journal[1:] if "kind" in line and line["base"] == "root"]
    assert written, journal
    _write_package(chain, revision="v1", description="agent helper", extra=False)

    result = chain.run("install", *_SCAN.args)

    assert result.returncode == 0, _evidence(result)
    assert "left by an interrupted install" in _text(result), _evidence(result)
    assert _tree_state(chain.project) == before, _evidence(result)
    assert _stores(chain) == [], _evidence(result)
    assert chain.run("audit", "--ci").returncode == 0


# -- shared config files: hook settings and the hook sidecar (#5009) ------------------
#
# The ledger never claims shared config files; APM's config file records do. A
# killed install's write is restored when the file still holds exactly what the
# journal sealed and the record says APM wrote those bytes; else kept and reported.

_SETTINGS = ".claude/settings.json"
_SIDECAR = ".claude/apm-hooks.json"
_SHARED_GATE = _Gate("require-hashes", "claude", "hash+kill")


def _write_root_hook(chain: Chain, revision: str) -> None:
    """The project's own hook, deployed into Claude's shared settings and its sidecar."""
    hooks = chain.project / ".apm" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    command = {"type": "command", "command": f"echo lint-{revision}"}
    data = {"hooks": {"Stop": [{"hooks": [command]}]}}
    (hooks / "lint.json").write_text(json.dumps(data), encoding="utf-8")


def _killed_shared_config(tmp_path: Path, binary: Path) -> tuple[Chain, dict[str, str]]:
    """Install v1 of the project's hook, then kill a refused v2 install that changed it."""
    chain = Chain(tmp_path / "shared", binary, dirs=(".claude",))
    chain.write_manifest([], apm=[], targets=["claude"], includes="auto")
    _write_root_hook(chain, "v1")
    chain.ok("install")
    before = _tree_state(chain.project)
    assert {_SETTINGS, _SIDECAR} <= set(before), before

    _write_root_hook(chain, "v2")
    _kill_mid_refusal(chain, _SHARED_GATE.fault)
    journal = _journal(_abandoned(chain))
    assert {_unit(journal, path)["path"] for path in (_SETTINGS, _SIDECAR)} == {
        _SETTINGS,
        _SIDECAR,
    }
    assert _tree_state(chain.project)[_SETTINGS] != before[_SETTINGS], "nothing changed"
    _write_root_hook(chain, "v1")
    return chain, before


def test_a_kill_after_a_shared_config_change_is_restored_on_the_next_install(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain, before = _killed_shared_config(tmp_path, apm_binary_path)

    result = chain.run("install")

    assert result.returncode == 0, _evidence(result)
    assert "interrupted install" in _text(result), _evidence(result)
    assert _text(result).isascii(), _evidence(result)
    assert _tree_state(chain.project) == before, _evidence(result)
    assert _stores(chain) == [], _evidence(result)
    assert chain.run("audit", "--ci").returncode == 0


def test_a_tampered_root_copy_is_kept_and_reported_not_restored(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain, _before = _killed_world(tmp_path, apm_binary_path, _SCAN)
    store = _abandoned(chain)
    agent = _unit(_journal(store), "pkg-agent.md")
    (store / agent["copy"]).write_bytes(b"# tampered copy\n")
    _SCAN.cure(chain)

    result = _assert_kept_and_reported(chain, _SCAN, store, agent["path"])

    assert "journal copy does not match its recorded hash" in _text(result), _evidence(result)
    assert (chain.project / agent["path"]).read_bytes() != b"# tampered copy\n"


def test_a_kill_settling_a_repair_keeps_the_repaired_file(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A repair commits no lockfile change; a kill at the journal's discard settles it."""
    chain = _SCAN.world(tmp_path, apm_binary_path)
    _write_package(chain, revision="v1", description="agent helper", extra=False)
    chain.ok("install", *_SCAN.args)
    before = _tree_state(chain.project)
    agent = next(path for path in before if path.endswith("pkg-agent.md"))
    (chain.project / agent).unlink()
    _kill_mid_refusal(chain, "settle+kill", *_SCAN.args)
    _abandoned(chain)

    result = chain.run("install", *_SCAN.args)

    assert result.returncode == 0, _evidence(result)
    assert _tree_state(chain.project) == before, _evidence(result)
    assert _stores(chain) == [], _evidence(result)


def test_an_mcp_install_replays_a_killed_install_first(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """``apm install --mcp`` opens no install transaction, yet replays before it writes."""
    chain, before = _killed_world(tmp_path, apm_binary_path, _SCAN)
    _SCAN.cure(chain)

    result = chain.run("install", "--mcp", "replay-srv", "--target", "kiro", "--", "echo", "hi")

    assert "left by an interrupted install" in _text(result), _evidence(result)
    assert _stores(chain) == [], _evidence(result)
    assert result.returncode == 0, _evidence(result)
    # The killed install's files are restored; the MCP install then adds its server.
    after = _tree_state(chain.project)
    restored = {k: v for k, v in after.items() if k in before and k != "apm.lock.yaml"}
    assert restored == {k: v for k, v in before.items() if k != "apm.lock.yaml"}, after
    assert not any(path.endswith("extra-agent.md") for path in after), after


def _first_written(chain: Chain, journal: list[dict]) -> tuple[dict, Path]:
    """The first deploy-root unit and a file at or below it the killed install wrote."""
    unit = next(line for line in journal[1:] if line.get("base") == "root")
    path = chain.project / unit["path"]
    if path.is_dir():
        path = next(p for p in sorted(path.rglob("*")) if p.is_file())
    assert path.is_file(), unit
    return unit, path


def test_a_file_a_user_edits_after_a_mid_deploy_kill_is_kept(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Post-write seals: the user's edit is kept and named; the other write is restored."""
    chain = _SCAN.world(tmp_path, apm_binary_path)
    _write_package(chain, revision="v1", description="agent helper", extra=False)
    chain.ok("install", *_SCAN.args)
    before = _tree_state(chain.project)
    _write_package(chain, revision="v2", description="agent helper v2", extra=True)
    _kill_mid_refusal(chain, "deploy+kill", *_SCAN.args, APM_TEST_DEPLOY_KILL_AFTER="2")
    journal = _journal(_abandoned(chain))
    unit, edited = _first_written(chain, journal)
    edited.write_text("# my edit after the crash\n", encoding="utf-8")
    others = [line for line in journal[1:] if "kind" in line and line is not unit]
    assert others, journal
    _write_package(chain, revision="v1", description="agent helper", extra=False)

    result = chain.run("install", *_SCAN.args)

    assert f"Kept {unit['path']}: it changed after the interrupted install" in _text(result), (
        _evidence(result)
    )
    assert edited.read_text(encoding="utf-8") == "# my edit after the crash\n"
    for other in others:
        rel = other["path"]
        if other["base"] == "root" and rel in before:
            assert _tree_state(chain.project).get(rel) == before[rel], rel
    assert _text(result).isascii(), _evidence(result)


def test_a_write_killed_before_its_seal_is_restored(tmp_path: Path, apm_binary_path: Path) -> None:
    """A path written but not yet sealed holds APM's expected bytes: it is restored."""
    chain = _SCAN.world(tmp_path, apm_binary_path)
    _write_package(chain, revision="v1", description="agent helper", extra=False)
    chain.ok("install", *_SCAN.args)
    before = _tree_state(chain.project)
    _write_package(chain, revision="v2", description="agent helper v2", extra=True)
    _kill_mid_refusal(chain, "unsealed+kill", *_SCAN.args, APM_TEST_DEPLOY_KILL_AFTER="3")
    journal = _journal(_abandoned(chain))
    units = [line for line in journal[1:] if "kind" in line]
    assert len(units) == 3, journal
    _write_package(chain, revision="v1", description="agent helper", extra=False)

    result = chain.run("install", *_SCAN.args)

    assert result.returncode == 0, _evidence(result)
    assert "Kept" not in _text(result), _evidence(result)
    assert _tree_state(chain.project) == before, _evidence(result)
    assert _stores(chain) == [], _evidence(result)


def test_a_shared_config_without_its_baseline_is_kept_and_reported(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The killed install's write is not vouched by the records: nothing is applied."""
    chain, _before = _killed_shared_config(tmp_path, apm_binary_path)
    store = _abandoned(chain)
    settings = chain.project / _SETTINGS
    shutil.rmtree(Path(chain.env["APM_HOME"]) / "deployment-receipts" / "config-files")
    held = settings.read_bytes()

    _assert_kept_and_reported(chain, _SHARED_GATE, store, _SETTINGS)
    assert settings.read_bytes() == held


def test_a_shared_config_a_user_edits_after_the_kill_is_kept(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The user's edit is kept and named; the sidecar the killed install changed is restored."""
    chain, before = _killed_shared_config(tmp_path, apm_binary_path)
    settings = chain.project / _SETTINGS
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["permissions"] = {"allow": ["Bash(ls)"]}  # the user's own setting
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    result = chain.run("install")

    assert result.returncode == 0, _evidence(result)
    assert f"Kept {_SETTINGS}: it changed after the interrupted install" in _text(result), (
        _evidence(result)
    )
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after["permissions"] == {"allow": ["Bash(ls)"]}, after
    assert _tree_state(chain.project)[_SIDECAR] == before[_SIDECAR], _evidence(result)
    assert _text(result).isascii(), _evidence(result)


# -- cleanup rewrites of files the records do not know yet (#5038) ---------------------
#
# An install before the config file records existed left hook settings with no
# record. A later install's cleanup that rewrites them records them as legacy, so a
# kill after that rewrite is restored on the next install like any recorded write.

_USERS_SETTINGS = {"permissions": {"allow": ["Bash(ls)"]}}


def test_a_kill_after_a_hook_cleanup_of_an_unrecorded_file_is_restored(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain = Chain(tmp_path / "cleanup", apm_binary_path, dirs=(".claude", ".github"))
    settings = chain.project / _SETTINGS
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(_USERS_SETTINGS, indent=2) + "\n", encoding="utf-8")
    _write_root_hook(chain, "v1")
    chain.write_manifest([], apm=[], targets=["claude", "copilot"], includes="auto")
    chain.ok("install")
    before = _tree_state(chain.project)
    assert "lint-v1" in settings.read_text(encoding="utf-8")
    # Installed before the records existed: nothing vouches for the settings yet.
    shutil.rmtree(Path(chain.env["APM_HOME"]) / "deployment-receipts" / "config-files")

    # Dropping Claude cleans APM's hook out of the shared settings; kill after that
    # write is done (and recorded), before the sidecar is touched.
    chain.write_manifest([], apm=[], targets=["copilot"], includes="auto")
    _kill_mid_refusal(chain, "deploy+kill", APM_TEST_DEPLOY_KILL_BEFORE=_SIDECAR)
    journal = _journal(_abandoned(chain))
    assert _unit(journal, _SETTINGS)["path"] == _SETTINGS, journal
    assert _SIDECAR not in {line.get("path") for line in journal}, journal
    assert json.loads(settings.read_text(encoding="utf-8")) == _USERS_SETTINGS
    chain.write_manifest([], apm=[], targets=["claude", "copilot"], includes="auto")

    result = chain.run("install")

    assert result.returncode == 0, _evidence(result)
    assert "Kept" not in _text(result), _evidence(result)
    assert "interrupted install" in _text(result), _evidence(result)
    assert _tree_state(chain.project) == before, _evidence(result)
    assert _stores(chain) == [], _evidence(result)
    assert _text(result).isascii(), _evidence(result)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory modes")
def test_a_mode_change_on_an_mcp_config_directory_apm_created_is_kept(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#5086: the native MCP config directory is journaled with its mode, so replay keeps it."""
    chain = Chain(tmp_path / "mcp-dir", apm_binary_path)
    chain.write_manifest([stdio_server("replay-srv", "hi")], apm=["./pkg"])
    _write_package(chain, revision="v1", description="agent helper", extra=False)
    settings = ".kiro/settings"
    config = f"{settings}/mcp.json"
    _kill_mid_refusal(chain, "path+kill", *_SCAN.args, APM_TEST_DEPLOY_KILL_PATH=config)
    config_dir = chain.project / settings
    assert (chain.project / config).is_file(), _tree_state(chain.project)
    _abandoned(chain)
    os.chmod(config_dir, 0o700)

    result = chain.run("install", *_SCAN.args)

    assert result.returncode == 0, _evidence(result)
    assert f"Kept {settings}: it changed after the interrupted install" in _text(result), _evidence(
        result
    )
    assert config_dir.is_dir(), _evidence(result)
    assert _mode(config_dir) == 0o700, _evidence(result)
    assert (config_dir / "mcp.json").is_file(), _evidence(result)
    assert _text(result).isascii(), _evidence(result)


def _packed_bundle(chain: Chain, version: str) -> Path:
    """Pack a local plugin bundle whose skill and agent change with *version*."""
    producer = chain.isolated.work_root / "producer"
    skill = producer / ".apm" / "skills" / "bundled-skill"
    agents = producer / ".apm" / "agents"
    skill.mkdir(parents=True, exist_ok=True)
    agents.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": "bundled",
        "version": version,
        "description": "journal replay bundle fixture",
        "targets": ["claude"],
        "dependencies": {"apm": []},
    }
    (producer / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (skill / "SKILL.md").write_text(
        f"---\nname: bundled-skill\ndescription: fixture skill\n---\n# skill {version}\n",
        encoding="utf-8",
    )
    (agents / "bundled-agent.agent.md").write_text(
        f'---\ndescription: "bundled agent"\n---\n# agent {version}\n', encoding="utf-8"
    )
    out = chain.isolated.work_root / f"out-{version}"
    for args in (("install", "--no-policy"), ("pack", "-o", str(out))):
        result = chain.runner.run(args, cwd=producer, env=chain.env, scenario_id=" ".join(args))
        assert result.returncode == 0, _evidence(result)
    return out / f"bundled-{version}"


def test_a_local_bundle_install_killed_mid_deploy_is_replayed(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A local bundle install journals its deploy writes like any install (#5046)."""
    chain = Chain(tmp_path / "bundle", apm_binary_path, dirs=(".claude",))
    v1, v2 = _packed_bundle(chain, "1.0.0"), _packed_bundle(chain, "2.0.0")
    chain.ok("install", str(v1))
    before = _tree_state(chain.project)
    assert "apm.lock.yaml" in before, before

    _kill_mid_refusal(chain, "deploy+kill", str(v2), "--force", APM_TEST_DEPLOY_KILL_AFTER="1")
    _abandoned(chain)
    assert _tree_state(chain.project) != before, "the killed install deployed nothing"

    result = chain.run("install", str(v1))

    assert result.returncode == 0, _evidence(result)
    assert "left by an interrupted install" in _text(result), _evidence(result)
    assert _tree_state(chain.project) == before, _evidence(result)
    assert _stores(chain) == [], _evidence(result)
    _assert_lock_matches_disk(chain.project)


_CANVAS = ".github/extensions/board/extension.mjs"


def _canvas_bundle(chain: Chain, version: str, *, canvas: bool) -> Path:
    """Pack a local bundle whose version 1 ships a canvas extension and later ones do not."""
    producer = chain.isolated.work_root / "canvas-producer"
    skill = producer / ".apm" / "skills" / "canvasbundle-s"
    skill.mkdir(parents=True, exist_ok=True)
    (producer / ".github").mkdir(exist_ok=True)
    manifest = {
        "name": "canvasbundle",
        "version": version,
        "description": "journal replay canvas fixture",
        "targets": ["copilot"],
        "dependencies": {"apm": []},
    }
    (producer / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: canvasbundle-s\ndescription: fixture skill\n---\n# skill\n", encoding="utf-8"
    )
    extensions = producer / ".apm" / "extensions"
    shutil.rmtree(extensions, ignore_errors=True)
    if canvas:
        (extensions / "board").mkdir(parents=True)
        (extensions / "board" / "extension.mjs").write_text(
            "export default { name: 'board' };\n", encoding="utf-8"
        )
    out = chain.isolated.work_root / f"canvas-out-{version}"
    for args in (("install", "--no-policy"), ("pack", "-o", str(out))):
        result = chain.runner.run(args, cwd=producer, env=chain.env, scenario_id=" ".join(args))
        assert result.returncode == 0, _evidence(result)
    return out / f"canvasbundle-{version}"


def test_a_local_bundle_retire_killed_mid_delete_is_replayed(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A bundle reinstall's retire deletes are journaled like its deploys (#5130)."""
    chain = Chain(tmp_path / "retire", apm_binary_path, dirs=(".github",))
    chain.ok("experimental", "enable", "canvas")
    v1 = _canvas_bundle(chain, "1.0.0", canvas=True)
    v2 = _canvas_bundle(chain, "2.0.0", canvas=False)
    chain.ok("install", str(v1))
    before = _tree_state(chain.project)
    assert _CANVAS in before, before

    # Version 2 ships no canvas: its install retires the extension, killed mid-retire.
    _kill_mid_refusal(chain, "retire+kill", str(v2))
    journal = _journal(_abandoned(chain))
    assert any(line.get("path") == _CANVAS for line in journal[1:]), journal
    assert not (chain.project / _CANVAS).exists()

    result = chain.run("install", str(v1))

    assert result.returncode == 0, _evidence(result)
    assert "left by an interrupted install" in _text(result), _evidence(result)
    assert _tree_state(chain.project) == before, _evidence(result)
    assert _stores(chain) == [], _evidence(result)
    _assert_lock_matches_disk(chain.project)
