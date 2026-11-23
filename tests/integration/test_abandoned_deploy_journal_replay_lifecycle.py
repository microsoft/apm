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
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.integration._mcp_chain_kit import Chain
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
_JOURNAL = "journal.json"
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


def _kill_mid_refusal(chain: Chain, fault: str, *args: str) -> None:
    """Run ``apm install`` on the engine, killed where the refusal would restore.

    A killed process has no exit contract, so it bypasses the runner's output oracle.
    """
    env = dict(chain.env)
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


def _journal(store: Path) -> dict:
    return json.loads((store / _JOURNAL).read_text(encoding="utf-8"))


def _write_journal(store: Path, journal: dict) -> None:
    (store / _JOURNAL).write_text(json.dumps(journal), encoding="utf-8")


def _unit(journal: dict, suffix: str) -> dict:
    found = [unit for unit in journal["units"] if unit["path"].endswith(suffix)]
    assert len(found) == 1, journal["units"]
    return found[0]


def _assert_kept_and_reported(chain: Chain, gate: _Gate, store: Path, named: str) -> CommandResult:
    """The install stops, names *named*, keeps the journal and applies none of it."""
    leftover = _tree_state(chain.project)
    result = chain.run("install", *gate.args)

    assert result.returncode == 1, _evidence(result)
    assert named in _text(result), _evidence(result)
    assert _text(result).isascii(), _evidence(result)
    assert _tree_state(chain.project) == leftover, _evidence(result)
    kept = [s for s in _stores(chain) if s.name.endswith(store.name.removeprefix("deploy-"))]
    assert len(kept) == 1, _stores(chain)
    assert (kept[0] / _JOURNAL).is_file(), sorted(kept[0].iterdir())
    return result


_SCAN = _GATES[0]


def test_an_unrestorable_path_keeps_the_journal_and_names_the_path(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    chain, _before = _killed_world(tmp_path, apm_binary_path, _SCAN)
    store = _abandoned(chain)
    agent = _unit(_journal(store), "pkg-agent.agent.md")
    # Its parent is now a regular file, so the prior bytes cannot be put back.
    parent = (chain.project / agent["path"]).parent
    shutil.rmtree(parent)
    parent.write_text("not a directory\n", encoding="utf-8")
    _SCAN.cure(chain)

    _assert_kept_and_reported(chain, _SCAN, store, agent["path"])


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
    journal["units"].append(
        {"path": escaping, "kind": "absent", "copy": None, "sha256": None, "link": None}
    )
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
    agent = _unit(journal, "pkg-agent.agent.md")
    # A self-consistent journal: the copy re-hashes to its recorded hash, but
    # the ledger recorded other bytes for this path.
    forged = b"# forged prior bytes\n"
    (store / agent["copy"]).write_bytes(forged)
    agent["sha256"] = hashlib.sha256(forged).hexdigest()
    _write_journal(store, journal)
    _SCAN.cure(chain)

    _assert_kept_and_reported(chain, _SCAN, store, agent["path"])
    assert (chain.project / agent["path"]).read_bytes() != forged
