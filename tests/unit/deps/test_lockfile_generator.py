"""Older-CLI lockfile generator gate (#2334)."""

from pathlib import Path

import pytest

from apm_cli.deps import lockfile_generator
from apm_cli.deps.lockfile import LockFile
from apm_cli.deps.lockfile_generator import LockfileGeneratorError, assert_writable


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
