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
from apm_cli.install.drift_records import RECORDS_SWITCH, record_policy, served_outputs
from apm_cli.install.package_resolution import EffectiveDependencySelection
from apm_cli.models.apm_package import APMPackage
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


def _world(
    tmp_path: Path, *, policy: str | None = _POLICY, manifest: str = "", skill: bytes | None = None
) -> dict:
    root = tmp_path / "pkg"
    for rel in _SOURCES.values():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(f"# {rel}\n".encode("ascii"))
    if skill is not None:
        (root / ".apm/skills/guide/SKILL.md").write_bytes(skill)
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
    parsed: bool = True,
):
    # The replay hands the decision the manifest it already parsed.
    package = APMPackage.from_apm_yml(world["root"] / "apm.yml") if parsed else None
    context = SimpleNamespace(**vars(_CONTEXT), package_info=SimpleNamespace(package=package))
    return served_outputs(
        world["dep"],
        world["root"],
        context=context,
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


def test_a_locked_directory_holding_recorded_files_still_binds(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world["dep"].deployed_files = [*world["dep"].deployed_files, ".github/skills/guide"]

    assert _serve(world) is not None
    world["dep"].deployed_files = [*world["dep"].deployed_files, ".github/skills/empty"]
    assert _serve(world) is None


@pytest.mark.parametrize(
    "declaration",
    [".mcp.json", ".lsp.json", ".claude-plugin/plugin.json", "plugin.json", "nested/.mcp.json"],
)
def test_a_plugin_format_server_declaration_replays_live(tmp_path: Path, declaration: str) -> None:
    world = _world(tmp_path)
    path = world["root"] / declaration
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"mcpServers": {"s": {"command": "srv"}}}', encoding="ascii")

    assert _serve(world) is None
    assert path.is_file()


@pytest.mark.parametrize(
    "manifest",
    [
        "devDependencies:\n  mcp:\n    - io.github.acme/server\n",
        "dependencies:\n  lsp:\n    - name: kit-lsp\n      command: kit-ls\n"
        "      extensionToLanguage:\n        .kit: kit\n",
        "devDependencies:\n  lsp:\n    - name: kit-lsp\n      command: kit-ls\n"
        "      extensionToLanguage:\n        .kit: kit\n",
    ],
)
def test_a_package_declaring_dev_or_lsp_servers_replays_live(tmp_path: Path, manifest: str) -> None:
    world = _world(tmp_path, manifest=manifest)

    assert _serve(world) is None


def test_a_manifest_the_replay_did_not_parse_replays_live(tmp_path: Path) -> None:
    world = _world(tmp_path)

    assert _serve(world, parsed=False) is None
    assert _serve(world) is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")
def test_a_linked_source_directory_replays_live(tmp_path: Path) -> None:
    world = _world(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (world["root"] / ".apm/context").symlink_to(elsewhere, target_is_directory=True)

    assert _serve(world) is None


def test_source_bytes_off_the_lock_bound_digest_replay_live(tmp_path: Path) -> None:
    # A record whose raw digest was moved to new source bytes, while its
    # canonical digest (and the lock) still name the installed bytes.
    world = _world(tmp_path)
    source = world["root"] / ".apm/skills/guide/SKILL.md"
    source.write_bytes(b"# unrendered\n")
    files = {d: list(pair) for d, pair in world["record"]["files"].items()}
    files[".github/skills/guide/SKILL.md"][1] = hashlib.sha256(source.read_bytes()).hexdigest()
    _rewrite_record(world, files=files)

    assert _serve(world) is None


@pytest.mark.parametrize(
    "link",
    [
        b"[ref](../context/ref.context.md)",
        b"[ref](/abs/ref.context.md)",
        b"[readme](../../README.md)",
        b"[x](javascript:alert)",
        b"[x](https://)",
    ],
)
def test_a_source_with_a_rewritable_link_replays_live(tmp_path: Path, link: bytes) -> None:
    world = _world(tmp_path, skill=b"# guide\nSee " + link + b".\n")

    assert _serve(world) is None


def test_a_source_with_only_external_urls_is_served(tmp_path: Path) -> None:
    world = _world(tmp_path, skill=b"# guide\nSee [docs](https://example.com/docs).\n")

    assert _serve(world) is not None


@pytest.mark.parametrize(
    ("outcome", "discoveries"),
    [("found", 1), ("absent", 1), ("cache_miss_fetch_fail", 2), ("cached_stale", 2)],
)
def test_the_shared_discovery_keeps_only_answers_a_command_reuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, discoveries: int
) -> None:
    calls: list[Path] = []

    def discover(root: Path) -> SimpleNamespace:
        calls.append(root)
        return SimpleNamespace(outcome=outcome)

    # The discovery I/O boundary below the trust-policy owner.
    monkeypatch.setattr("apm_cli.policy.discovery.discover_policy_with_chain", discover)
    config = SimpleNamespace(policy_provider=None, policy_fetch=None, user_scope=False)
    fetch, _provider = record_policy(config, tmp_path, enabled=True)

    assert fetch().outcome == outcome
    assert fetch().outcome == outcome
    assert len(calls) == discoveries


@pytest.mark.parametrize(
    "target",
    [
        "https://example.com/a",
        "http://h",
        "https://",
        "http:rel/path",
        " https://h/x ",
        "ftp://h/f",
        "javascript:alert",
        "../a.md",
        "/abs/b.context.md",
        "#anchor",
    ],
)
def test_the_live_link_rule_is_the_resolver_rule(tmp_path: Path, target: str) -> None:
    from apm_cli.compilation.link_resolver import UnifiedLinkResolver
    from apm_cli.install.drift_records import _may_rewrite_links

    resolver = UnifiedLinkResolver(tmp_path)
    data = f"[x]({target})".encode()

    assert _may_rewrite_links(data) is not resolver._is_external_url(target)
