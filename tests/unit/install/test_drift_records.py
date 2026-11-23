"""Drift replay served from package render records: the serve decision (#4940).

Every case below must send the package live (``None``) except the one whose
record binds this lock entry, this run's policy and the live source bytes.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.cache.deploy_store import DeployStore, package_record_key
from apm_cli.core.scope import InstallScope
from apm_cli.install.drift_records import RECORDS_SWITCH, served_outputs
from apm_cli.install.package_resolution import EffectiveDependencySelection
from apm_cli.utils.content_hash import canonical_digest
from apm_cli.version import get_version

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-ci-audit-scratch-materialization")]

_POLICY = "b" * 64
_TARGETS = ("copilot",)
_CONTEXT = SimpleNamespace(targets=_TARGETS, scope=InstallScope.PROJECT)
_SOURCES = {
    ".github/instructions/rule.instructions.md": ".apm/instructions/rule.instructions.md",
    ".github/skills/guide/SKILL.md": ".apm/skills/guide/SKILL.md",
}


def _world(tmp_path: Path, *, policy: str | None = _POLICY, manifest: str = "") -> dict:
    root = tmp_path / "pkg"
    for rel in _SOURCES.values():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(f"# {rel}\n".encode("ascii"))
    (root / "apm.yml").write_text(f"name: pkg\nversion: 1.0.0\n{manifest}", encoding="ascii")
    files = {dest: (root / src).read_bytes() for dest, src in _SOURCES.items()}
    dep = SimpleNamespace(
        repo_url="https://github.com/acme/pkg",
        resolved_commit="a" * 40,
        content_hash="sha256:" + "c" * 64,
        deployed_files=sorted(files),
        deployed_file_hashes={d: f"sha256:{canonical_digest(b)}" for d, b in files.items()},
        native_plugin=None,
        is_local_source=False,
        local_path=None,
    )
    pairs = {d: [canonical_digest(b), hashlib.sha256(b).hexdigest()] for d, b in files.items()}
    record = {
        "v": 1,
        "files": pairs,
        "ascii_only": True,
        "body": None,
    }
    if policy is not None:
        record["policy"] = policy
    store = DeployStore(tmp_path / "cache")
    key = package_record_key(
        dep.repo_url,
        dep.resolved_commit,
        dep.content_hash,
        _TARGETS,
        get_version(),
        InstallScope.PROJECT,
    )
    assert store.put_record(key, record)
    return {"root": root, "dep": dep, "store": store, "key": key, "record": record}


def _mapping(root: Path, _context: object) -> dict[str, Path]:
    return {dest: root / src for dest, src in _SOURCES.items()}


def _serve(
    world: dict,
    *,
    mapping=_mapping,
    selection=None,
    policy: str | None = _POLICY,
    other_dests: tuple[str, ...] = (),
):
    return served_outputs(
        world["dep"],
        world["root"],
        context=_CONTEXT,
        selection=selection or EffectiveDependencySelection(None, None),
        policy_fingerprint=policy,
        other_dests=other_dests,
        store=world["store"],
        mapping=mapping,
    )


def test_a_record_bound_to_lock_policy_and_source_bytes_is_served(tmp_path: Path) -> None:
    world = _world(tmp_path)

    served = _serve(world)

    sources = _mapping(world["root"], _CONTEXT)
    assert served == {dest: source.read_bytes() for dest, source in sources.items()}


def _rewrite_record(world: dict, **changes: object) -> None:
    record = {**world["record"], **changes}
    assert world["store"].put_record(world["key"], record)


def _missing_record(world: dict, _monkeypatch: pytest.MonkeyPatch) -> None:
    path = world["store"]._record_path(world["key"])
    path.unlink()


def _stale_content_hash(world: dict, _monkeypatch: pytest.MonkeyPatch) -> None:
    world["dep"].content_hash = "sha256:" + "d" * 64


def _tampered_raw_sha(world: dict, _monkeypatch: pytest.MonkeyPatch) -> None:
    files = {d: list(pair) for d, pair in world["record"]["files"].items()}
    first = sorted(files)[0]
    files[first][1] = "e" * 64
    _rewrite_record(world, files=files)


def _record_without_policy(world: dict, _monkeypatch: pytest.MonkeyPatch) -> None:
    record = dict(world["record"])
    record.pop("policy")
    assert world["store"].put_record(world["key"], record)


def _lock_lists_an_extra_file(world: dict, _monkeypatch: pytest.MonkeyPatch) -> None:
    world["dep"].deployed_files = [*world["dep"].deployed_files, ".github/prompts/x.prompt.md"]


def _source_bytes_changed(world: dict, _monkeypatch: pytest.MonkeyPatch) -> None:
    (world["root"] / ".apm/skills/guide/SKILL.md").write_bytes(b"# changed\n")


def _source_with_crlf(world: dict, _monkeypatch: pytest.MonkeyPatch) -> None:
    source = world["root"] / ".apm/skills/guide/SKILL.md"
    source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))


def _source_with_a_bom(world: dict, _monkeypatch: pytest.MonkeyPatch) -> None:
    source = world["root"] / ".apm/skills/guide/SKILL.md"
    source.write_bytes(b"\xef\xbb\xbf" + source.read_bytes())


def _hooks_in_the_source(world: dict, _monkeypatch: pytest.MonkeyPatch) -> None:
    (world["root"] / ".apm/hooks").mkdir(parents=True)
    (world["root"] / ".apm/hooks/h.json").write_text("{}", encoding="ascii")


def _native_plugin(world: dict, _monkeypatch: pytest.MonkeyPatch) -> None:
    world["dep"].native_plugin = {"kind": "claude"}


def _switch_off(_world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RECORDS_SWITCH, "0")


@pytest.mark.parametrize(
    "mutate",
    [
        _missing_record,
        _stale_content_hash,
        _tampered_raw_sha,
        _record_without_policy,
        _lock_lists_an_extra_file,
        _source_bytes_changed,
        _source_with_crlf,
        _source_with_a_bom,
        _hooks_in_the_source,
        _native_plugin,
        _switch_off,
    ],
)
def test_any_unproven_input_replays_the_package_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate
) -> None:
    monkeypatch.delenv(RECORDS_SWITCH, raising=False)
    world = _world(tmp_path)
    mutate(world, monkeypatch)

    assert _serve(world) is None
    assert (world["root"] / "apm.yml").is_file()


def test_a_policy_changed_since_install_replays_live(tmp_path: Path) -> None:
    world = _world(tmp_path)

    assert _serve(world, policy="f" * 64) is None
    assert _serve(world, policy=None) is None


def _renders(_root: Path, _context: object) -> None:
    return None


def _omits_a_dest(root: Path, context: object) -> dict[str, Path]:
    planned = _mapping(root, context)
    planned.pop(".github/skills/guide/SKILL.md")
    return planned


def _swaps_two_dests(root: Path, context: object) -> dict[str, Path]:
    first, second = sorted(_SOURCES)
    planned = _mapping(root, context)
    planned[first], planned[second] = planned[second], planned[first]
    return planned


def _one_source_for_two_dests(root: Path, context: object) -> dict[str, Path]:
    first, second = sorted(_SOURCES)
    planned = _mapping(root, context)
    planned[second] = planned[first]
    return planned


@pytest.mark.parametrize(
    "planned", [_renders, _omits_a_dest, _swaps_two_dests, _one_source_for_two_dests]
)
def test_a_mapping_that_differs_from_the_record_replays_live(tmp_path: Path, planned) -> None:
    world = _world(tmp_path)

    assert _serve(world, mapping=planned) is None


def test_a_narrowed_selection_replays_live(tmp_path: Path) -> None:
    world = _world(tmp_path)

    assert _serve(world, selection=EffectiveDependencySelection(("guide",), None)) is None


def test_a_package_declaring_mcp_servers_replays_live(tmp_path: Path) -> None:
    world = _world(tmp_path, manifest="dependencies:\n  mcp:\n    - io.github.acme/server\n")

    assert _serve(world) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and links")
@pytest.mark.parametrize("kind", ["exec-bit", "symlink"])
def test_an_executable_or_linked_source_replays_live(tmp_path: Path, kind: str) -> None:
    world = _world(tmp_path)
    source = world["root"] / ".apm/skills/guide/SKILL.md"
    if kind == "exec-bit":
        source.chmod(0o755)
    else:
        target = tmp_path / "elsewhere.md"
        target.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(target)

    assert _serve(world) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")
def test_a_linked_record_replays_live(tmp_path: Path) -> None:
    world = _world(tmp_path)
    path = world["store"]._record_path(world["key"])
    saved = tmp_path / "saved.json"
    saved.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(saved)

    assert _serve(world) is None


@pytest.mark.parametrize(
    "other", [".github/skills/guide/SKILL.md", ".GITHUB/skills/guide/skill.md"]
)
def test_a_dest_another_package_claims_replays_live(tmp_path: Path, other: str) -> None:
    world = _world(tmp_path)

    assert _serve(world, other_dests=(other,)) is None
    assert _serve(world, other_dests=(".github/prompts/other.prompt.md",)) is not None


def test_case_equal_dests_in_one_record_replay_live(tmp_path: Path) -> None:
    world = _world(tmp_path)
    files = {d: list(pair) for d, pair in world["record"]["files"].items()}
    files[".github/skills/guide/skill.md"] = files[".github/skills/guide/SKILL.md"]
    world["dep"].deployed_files = sorted(files)
    world["dep"].deployed_file_hashes = {d: f"sha256:{pair[0]}" for d, pair in files.items()}
    _rewrite_record(world, files=files)

    assert _serve(world) is None
