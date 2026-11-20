"""Older-CLI lockfile generator gate (#2334)."""

from pathlib import Path

import pytest

from apm_cli.deps import lockfile_generator
from apm_cli.deps.lockfile import LockFile
from apm_cli.deps.lockfile_generator import LockfileGeneratorError, assert_writable

pytestmark = pytest.mark.component


@pytest.fixture
def running(monkeypatch):
    def set_version(version: str) -> None:
        monkeypatch.setattr(lockfile_generator, "running_cli_version", lambda: version)

    set_version("0.25.0")
    return set_version


def _lock(tmp_path: Path, header: str) -> Path:
    path = tmp_path / "apm.lock.yaml"
    path.write_text(f'lockfile_version: "1"\n{header}dependencies: []\n', encoding="utf-8")
    return path


@pytest.mark.parametrize("recorded", ["0.25.0", "0.24.9", "0.25.0rc1", "0.25.0+build.3", "unknown"])
def test_equal_older_prerelease_and_unknown_generators_are_writable(tmp_path, running, recorded):
    assert_writable(_lock(tmp_path, f"apm_version: {recorded}\n"))


def test_missing_lockfile_and_missing_version_are_writable(tmp_path, running):
    assert_writable(tmp_path / "apm.lock.yaml")
    assert_writable(_lock(tmp_path, ""))


@pytest.mark.parametrize(
    ("running_version", "recorded"),
    [("0.24.0", "0.25.0"), ("0.25.0rc1", "0.25.0"), ("0.25.0a9", "0.25.0a10")],
)
def test_older_cli_is_refused_with_both_versions(tmp_path, running, running_version, recorded):
    running(running_version)
    with pytest.raises(LockfileGeneratorError) as exc:
        assert_writable(_lock(tmp_path, f"apm_version: {recorded}\n"))
    message = str(exc.value)
    assert f"written by APM {recorded}, newer than this APM {running_version}" in message
    assert "brew upgrade apm" in message


@pytest.mark.parametrize(
    "header",
    [
        "apm_version: 0.25.0\napm_version: 0.25.0\n",
        "apm_version: 1.0\n",
        "apm_version: banana\n",
        "apm_version: null\n",
        "apm_version:\n",
    ],
    ids=["duplicate", "float", "garbage", "null", "empty"],
)
def test_unverifiable_metadata_fails_closed(tmp_path, running, header):
    with pytest.raises(LockfileGeneratorError, match="Restore the lockfile"):
        assert_writable(_lock(tmp_path, header))


def test_unknown_running_version_cannot_vouch_for_a_versioned_lockfile(tmp_path, running):
    running("unknown")
    with pytest.raises(LockfileGeneratorError, match="cannot be compared"):
        assert_writable(_lock(tmp_path, "apm_version: 0.25.0\n"))


def test_lockfile_write_backstop_leaves_newer_lockfile_bytes(tmp_path, running):
    path = _lock(tmp_path, "apm_version: 9.0.0\n")
    before = path.read_bytes()
    with pytest.raises(LockfileGeneratorError):
        LockFile(apm_version="0.25.0").write(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "text",
    [
        '{"lockfile_version": "1", "apm_version": "9.0.0", "dependencies": []}\n',
        'lockfile_version: "1"\n"apm_version": \'9.0.0\'  # pinned\ndependencies: []\n',
    ],
    ids=["flow", "quoted-key"],
)
def test_newer_generator_is_seen_in_any_yaml_style(tmp_path, running, text):
    path = tmp_path / "apm.lock.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(LockfileGeneratorError, match=r"written by APM 9\.0\.0"):
        assert_writable(path)


def test_flow_style_duplicate_key_fails_closed(tmp_path, running):
    path = tmp_path / "apm.lock.yaml"
    path.write_text('{apm_version: "0.25.0", apm_version: "0.25.0"}\n', encoding="utf-8")
    with pytest.raises(LockfileGeneratorError, match="more than once"):
        assert_writable(path)


def test_legacy_lockfile_is_checked_without_renaming(tmp_path, running):
    from apm_cli.deps.lockfile_generator import check_current_cli

    legacy = tmp_path / "apm.lock"
    legacy.write_text('lockfile_version: "1"\napm_version: 9.0.0\ndependencies: []\n')
    with pytest.raises(LockfileGeneratorError, match=r"apm\.lock:"):
        check_current_cli(tmp_path, command="apm install")
    assert legacy.exists()
    assert not (tmp_path / "apm.lock.yaml").exists()


def test_write_backstop_refuses_unreadable_lockfile_without_claiming_atomicity(tmp_path, running):
    path = tmp_path / "apm.lock.yaml"
    path.write_text("lockfile_version: [unterminated\n", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(LockfileGeneratorError) as exc:
        LockFile(apm_version="0.25.0").write(path)
    assert "cannot be verified" in str(exc.value)
    assert "Nothing was changed" not in str(exc.value)
    assert "review other files" in str(exc.value)
    assert path.read_bytes() == before
    # Entry gates leave unreadable lockfiles to the parser's own diagnostics.
    assert_writable(path)


def test_top_level_merge_key_hides_the_generator(tmp_path, running):
    path = tmp_path / "apm.lock.yaml"
    path.write_text('base: &b {apm_version: "9.0.0"}\n<<: *b\ndependencies: []\n')
    with pytest.raises(LockfileGeneratorError, match="cannot be verified"):
        LockFile(apm_version="0.25.0").write(path)


def test_stamp_and_gate_share_one_version_owner():
    from apm_cli.deps.lockfile import installed_apm_version
    from apm_cli.version import get_version

    assert installed_apm_version() == get_version() == lockfile_generator.running_cli_version()


def test_running_version_always_accepts_its_own_stamp(tmp_path, running):
    running("0.25.0.dev1")
    assert_writable(_lock(tmp_path, "apm_version: 0.25.0.dev1\n"))


@pytest.fixture
def stamping(monkeypatch, running):
    """The running CLI is 0.40.0 for both the stamp and the gate."""
    from apm_cli.deps import lockfile as lockfile_module

    running("0.40.0")
    monkeypatch.setattr(lockfile_module, "installed_apm_version", lambda: "0.40.0")
    return _recorded_stamp


def _recorded_stamp(path: Path) -> str | None:
    import re

    match = re.search(r"(?m)^apm_version: (.*)$", path.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def _assert_stamp(path: Path, expected: str) -> None:
    assert _recorded_stamp(path) == expected


def _older_lock(tmp_path: Path, lock: LockFile) -> Path:
    path = tmp_path / "apm.lock.yaml"
    path.write_text(lock.to_yaml(), encoding="utf-8")
    return path


@pytest.mark.parametrize("with_timestamp", [False, True], ids=["plain", "legacy-timestamp"])
def test_changed_write_stamps_the_running_cli(tmp_path, stamping, with_timestamp):
    """Any writer that changes a lockfile records this CLI, not the loaded stamp (#3962)."""
    path = _older_lock(
        tmp_path,
        LockFile(
            apm_version="0.1.0",
            generated_at="2020-01-01T00:00:00+00:00" if with_timestamp else None,
            mcp_servers=["old"],
        ),
    )
    lock = LockFile.read(path)
    lock.mcp_servers = ["new"]
    lock.write(path)
    _assert_stamp(path, "0.40.0")


def test_semantic_noop_write_keeps_the_recorded_stamp(tmp_path, stamping):
    path = _older_lock(tmp_path, LockFile(apm_version="0.1.0", mcp_servers=["same"]))
    LockFile.read(path).write(path)
    _assert_stamp(path, "0.1.0")


def test_new_lockfile_is_stamped(tmp_path, stamping):
    path = tmp_path / "apm.lock.yaml"
    LockFile().write(path)
    _assert_stamp(path, "0.40.0")


def test_write_against_a_passed_existing_lockfile_stamps_changes(tmp_path, stamping):
    path = _older_lock(tmp_path, LockFile(apm_version="0.1.0"))
    existing = LockFile.read(path)
    from dataclasses import replace

    replace(existing, mcp_servers=["new"]).write(path, existing_lockfile=existing)
    _assert_stamp(path, "0.40.0")


def test_untranslated_mcp_claims_keep_their_recorded_stamp(tmp_path, stamping):
    """A pre-0.37 lock's copilot claims mean what its stamp says until translated."""
    path = _older_lock(
        tmp_path,
        LockFile(
            apm_version="0.36.0",
            mcp_servers=["foo"],
            mcp_target_servers={"copilot": ["foo"]},
        ),
    )
    lock = LockFile.read(path)
    lock.lsp_servers = ["added"]
    lock.write(path)
    _assert_stamp(path, "0.36.0")


def test_new_mcp_claims_on_an_older_lock_are_stamped(tmp_path, stamping):
    path = _older_lock(tmp_path, LockFile(apm_version="0.36.0"))
    lock = LockFile.read(path)
    lock.mcp_servers = ["foo"]
    lock.mcp_target_servers = {"copilot": ["foo"]}
    lock.write(path)
    _assert_stamp(path, "0.40.0")


def test_null_generator_warns_on_read(tmp_path, running, monkeypatch):
    """A read-only read of ``apm_version: null`` warns like any unverifiable stamp."""
    from apm_cli.utils import console

    warnings: list[str] = []
    monkeypatch.setattr(console, "_rich_warning", lambda message, **_: warnings.append(message))
    path = _lock(tmp_path, "apm_version: null\n")
    before = path.read_bytes()
    LockFile.read(path)
    assert len(warnings) == 1
    assert path.read_bytes() == before
    assert "apm_version 'null' is not a valid APM version" in warnings[0]


def test_missing_generator_does_not_warn_on_read(tmp_path, running, monkeypatch):
    from apm_cli.utils import console

    warnings: list[str] = []
    monkeypatch.setattr(console, "_rich_warning", lambda message, **_: warnings.append(message))
    LockFile.read(_lock(tmp_path, ""))
    assert warnings == []


@pytest.mark.parametrize("command", ["lock", "prune"])
def test_every_command_renders_the_refusal_like_install(tmp_path, running, monkeypatch, command):
    """Commands share one ``[x]`` refusal renderer, never click's ``Error:`` (#3967)."""
    from click.testing import CliRunner

    from apm_cli.cli import cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text("name: p\nversion: 1.0.0\n", encoding="utf-8")
    _lock(tmp_path, "apm_version: 99.0.0\n")
    result = CliRunner().invoke(cli, [command])
    assert result.exit_code == 1, result.output
    assert "[x] Refusing to run 'apm " in result.output
    assert "Error: Refusing" not in result.output
    assert "apm.lock.yaml:" in result.output


@pytest.mark.parametrize("passed", [False, True], ids=["loaded", "passed"])
def test_semantic_noop_write_keeps_the_file_bytes(tmp_path, stamping, passed):
    """A proven no-op never rewrites the lock, so comments and formatting survive."""
    path = _older_lock(tmp_path, LockFile(apm_version="0.1.0", mcp_servers=["same"]))
    original = "# reviewed by ops\n" + path.read_text(encoding="utf-8")
    path.write_text(original, encoding="utf-8")
    lock = LockFile.read(path)
    lock.write(path, **({"existing_lockfile": LockFile.read(path)} if passed else {}))
    assert path.read_text(encoding="utf-8") == original


def test_removing_the_last_untranslated_claim_stamps_the_writer(tmp_path, stamping):
    """Only a lock still carrying a pre-0.37 claim keeps the old stamp."""
    path = _older_lock(
        tmp_path,
        LockFile(
            apm_version="0.36.0", mcp_servers=["foo"], mcp_target_servers={"copilot": ["foo"]}
        ),
    )
    lock = LockFile.read(path)
    lock.mcp_servers = []
    lock.mcp_target_servers = {}
    lock.write(path)
    _assert_stamp(path, "0.40.0")


@pytest.mark.parametrize("header", ["apm_version: null\n", "apm_version: 99.0.0\n", ""])
def test_bundle_and_project_readers_warn_alike(tmp_path, running, monkeypatch, header):
    """The bundle reader keeps the absent/null distinction the project reader has."""
    from apm_cli.bundle.local_bundle import _read_bundle_lockfile

    warned: list[object] = []
    monkeypatch.setattr(
        lockfile_generator, "warn_if_newer", lambda _p, recorded, **_k: warned.append(recorded)
    )
    _lock(tmp_path, header)
    LockFile.read(tmp_path / "apm.lock.yaml")
    _read_bundle_lockfile(tmp_path)
    assert warned[0] == warned[1]


def test_packed_bundle_lock_names_the_packer(tmp_path, stamping):
    """A bundle lock is a new artifact: its generator is the packer, not the input (#3962)."""
    import yaml

    from apm_cli.bundle.lockfile_enrichment import enrich_lockfile_for_pack

    source = LockFile(apm_version="0.35.0", mcp_servers=["s"])
    enriched = yaml.safe_load(enrich_lockfile_for_pack(source, "plugin", "all"))
    assert enriched["apm_version"] == "0.40.0"
    assert source.apm_version == "0.35.0"


def test_noop_write_from_a_fresh_lock_keeps_the_recorded_stamp(tmp_path, stamping):
    """A rebuilt but unchanged lock keeps its bytes and generator, never dropping the stamp."""
    path = _older_lock(tmp_path, LockFile(apm_version="0.1.0", mcp_servers=["same"]))
    before = path.read_bytes()
    LockFile(mcp_servers=["same"]).write(path)
    assert path.read_bytes() == before
    _assert_stamp(path, "0.1.0")


def test_noop_write_records_an_explicit_stamp(tmp_path, stamping):
    """A caller that stamps a semantically unchanged lock (the MCP translation) is written."""
    path = _older_lock(tmp_path, LockFile(apm_version="0.35.0", mcp_servers=["same"]))
    lock = LockFile.read(path)
    lock.apm_version = "0.40.0"
    lock.write(path, existing_lockfile=LockFile.read(path))
    _assert_stamp(path, "0.40.0")
