"""Recorder and replay rules of the no-op install fast path (noop_fast_path.py).

The lifecycle module tests/lifecycle/security/test_noop_fast_path.py proves
the fast path against the real CLI; these cases pin the recorder's own
fail-closed rules: only a recorded no-op is replayed, any changed input
re-runs, and an instrumentation gap or an unreadable stamp records nothing.
The owner runs for real; only its collaborators (policy discovery, the
lockfile hash verifier) and the stdlib are controlled.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.exceptions import Exit

from apm_cli.install import noop_fast_path as fp
from apm_cli.policy.discovery import PolicyFetchResult

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I2")]
_REAL_STAT, _REAL_LSTAT = os.stat, os.lstat  # captured before any recorder runs
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="no-op replay is POSIX-only")

_SUMMARY = "No changes -- install state already up to date in 1.2s."
_HIT = "[i] install fast path: replayed a verified no-op"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "apm.lock.yaml").write_text("lockfile_version: '1'\ndependencies: []\n", "utf-8")
    (root / "input.txt").write_text("v1\n", encoding="utf-8")
    monkeypatch.setenv("APM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("APM_INSTALL_FAST_PATH", "trace")
    monkeypatch.chdir(root)
    _serve_policy(monkeypatch, "org:contoso")
    return root


def _serve_policy(monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    """Live discovery answers *source*; the canonical router classifies it."""
    monkeypatch.setattr(
        "apm_cli.policy.outcome_routing.discover_trust_policy",
        lambda _root: PolicyFetchResult(outcome="disabled", source=source),
    )


def _stamps() -> list[Path]:
    return sorted((Path(os.environ["APM_CACHE_DIR"]) / "noop_v1").glob("*.json"))


class _Install:
    """A stand-in install: reads its input, probes a path, prints a no-op summary."""

    def __init__(self, root: Path, *, write: bool = False, summary: str = _SUMMARY) -> None:
        self.root = root
        self.write = write
        self.summary = summary
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        (self.root / "input.txt").read_text(encoding="utf-8")
        os.path.exists(self.root / "probed-but-absent")
        if self.write:
            (self.root / "output.txt").write_text("x", encoding="utf-8")
        print("[*] Installing")
        print(self.summary)


def _install(run: _Install) -> None:
    fp.run_with_fast_path({}, run, project_root=run.root)


def _converge(run: _Install) -> None:
    """A no-op marks the invocation pending; the next one records it with the live policy."""
    _install(run)
    _install(run)


@_POSIX_ONLY
def test_a_recorded_noop_is_replayed_without_running(project, capsys) -> None:
    run = _Install(project)
    _converge(run)
    capsys.readouterr()
    _install(run)
    out, err = capsys.readouterr()
    assert run.calls == 2, err
    assert _HIT in err, err
    assert out.splitlines()[0] == "[*] Installing"
    assert out.splitlines()[-1].startswith("No changes -- install state already up to date in ")
    assert "1.2s" not in out.splitlines()[-1], "the replay renders its own elapsed time"


@_POSIX_ONLY
def test_a_miss_without_any_stamp_runs_no_live_discovery(project, monkeypatch) -> None:
    discovered: list[Path] = []
    monkeypatch.setattr(
        "apm_cli.policy.outcome_routing.discover_trust_policy",
        lambda root: discovered.append(root) or PolicyFetchResult(outcome="disabled"),
    )
    run = _Install(project)
    _install(run)
    assert run.calls == 1
    assert discovered == [], "a miss without a stamp must not discover policy"
    assert [json.loads(p.read_text("utf-8")).get("pending") for p in _stamps()] == [True]


@_POSIX_ONLY
@pytest.mark.parametrize(
    "change",
    [
        lambda root: (root / "input.txt").write_text("v2\n", encoding="utf-8"),
        lambda root: (root / "probed-but-absent").mkdir(),
        lambda root: (root / "apm.lock.yaml").unlink(),
    ],
    ids=["read-file-content", "probed-path-appears", "lockfile-removed"],
)
def test_any_changed_input_runs_the_full_install(project, change) -> None:
    run = _Install(project)
    _converge(run)
    change(project)
    _install(run)
    assert run.calls == 3


@_POSIX_ONLY
def test_a_changed_org_policy_runs_the_full_install(project, monkeypatch) -> None:
    run = _Install(project)
    _converge(run)
    _serve_policy(monkeypatch, "org:tightened")
    _install(run)
    assert run.calls == 3


@_POSIX_ONLY
def test_deployed_hash_drift_runs_the_full_install(project, monkeypatch) -> None:
    run = _Install(project)
    _converge(run)
    monkeypatch.setattr(
        "apm_cli.policy.ci_checks.verify_deployed_file_hashes",
        lambda _root, _lock: (["drift"], []),
    )
    _install(run)
    assert run.calls == 3


@_POSIX_ONLY
def test_a_run_that_writes_records_no_stamp(project) -> None:
    run = _Install(project, write=True)
    for _ in range(3):
        _install(run)
    assert run.calls == 3
    assert _stamps() == []


@_POSIX_ONLY
def test_discarding_output_to_devnull_is_not_a_write(project) -> None:
    class _QuietInstall(_Install):
        def __call__(self) -> None:
            with open(os.devnull, "w", encoding="utf-8") as sink:
                sink.write("discarded")
            super().__call__()

    run = _QuietInstall(project)
    _converge(run)
    _install(run)
    assert run.calls == 2


@_POSIX_ONLY
def test_a_run_without_the_noop_summary_records_no_stamp(project) -> None:
    run = _Install(project, summary="Installed 1 package in 1.0s.")
    for _ in range(3):
        _install(run)
    assert run.calls == 3
    assert _stamps() == []


@_POSIX_ONLY
def test_an_instrumentation_gap_records_no_stamp(project, monkeypatch) -> None:
    def unobserved_exists(path) -> bool:  # an exists the probes cannot see
        try:
            _REAL_STAT(os.fspath(path))
        except OSError:
            return False
        return True

    monkeypatch.setattr(os.path, "exists", unobserved_exists)
    run = _Install(project)
    for _ in range(3):
        _install(run)
    assert run.calls == 3
    assert _stamps() == []


@_POSIX_ONLY
def test_an_unreadable_stamp_runs_the_full_install(project) -> None:
    run = _Install(project)
    _converge(run)
    for stamp in _stamps():
        stamp.write_text("{not json", encoding="utf-8")
    _install(run)
    assert run.calls == 3


@_POSIX_ONLY
def test_a_malformed_stamp_runs_the_full_install(project) -> None:
    run = _Install(project)
    _converge(run)
    for stamp in _stamps():
        data = json.loads(stamp.read_text(encoding="utf-8"))
        data["output"] = [[1, 7]]
        stamp.write_text(json.dumps(data), encoding="utf-8")
    _install(run)
    assert run.calls == 3


@_POSIX_ONLY
def test_an_input_changed_during_the_run_records_no_stamp(project) -> None:
    run = _Install(project)
    _install(run)  # pending
    # A concurrent editor: its descriptor opened before the run, so the
    # recorder does not see the write that lands after the run read the file.
    editor = os.open(project / "input.txt", os.O_WRONLY | os.O_APPEND)

    class _RacingInstall(_Install):
        def __call__(self) -> None:
            super().__call__()
            os.write(editor, b"changed by someone else\n")

    racing = _RacingInstall(project)
    try:
        _install(racing)
    finally:
        os.close(editor)
    assert [json.loads(p.read_text("utf-8")).get("pending") for p in _stamps()] == [True]


@_POSIX_ONLY
def test_the_fast_path_can_be_disabled(project, monkeypatch) -> None:
    run = _Install(project)
    _converge(run)
    monkeypatch.setenv("APM_INSTALL_FAST_PATH", "0")
    _install(run)
    assert run.calls == 3


@_POSIX_ONLY
def test_a_read_of_an_expiring_cache_records_no_stamp(project) -> None:
    cached = Path(os.environ["APM_CACHE_DIR"]) / "http_v1" / "entry"
    cached.parent.mkdir(parents=True)
    cached.write_text("fresh until the TTL ends", encoding="utf-8")

    class _CachedInstall(_Install):
        def __call__(self) -> None:
            cached.read_text(encoding="utf-8")
            super().__call__()

    run = _CachedInstall(project)
    for _ in range(3):
        _install(run)
    assert run.calls == 3
    assert _stamps() == []


@_POSIX_ONLY
@pytest.mark.parametrize("code, replayed", [(0, True), (1, False)])
def test_only_a_clean_command_exit_records(project, code, replayed) -> None:
    class _ExitingInstall(_Install):
        def __call__(self) -> None:
            super().__call__()
            raise Exit(code)

    run = _ExitingInstall(project)
    for _ in range(3):
        try:
            _install(run)
        except Exit as error:
            assert error.exit_code == code
    assert run.calls == (2 if replayed else 3)


def test_windows_always_runs_the_full_install(project, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    run = _Install(project)
    for _ in range(3):
        _install(run)
    assert run.calls == 3


def test_the_fast_path_is_off_by_default(project, monkeypatch) -> None:
    """Unset, install runs exactly as before: no recorder, no discovery, no stamp."""
    monkeypatch.delenv("APM_INSTALL_FAST_PATH")
    discovered: list[Path] = []
    monkeypatch.setattr(
        "apm_cli.policy.outcome_routing.discover_trust_policy",
        lambda root: discovered.append(root) or PolicyFetchResult(outcome="disabled"),
    )
    stdout, environ_class = sys.stdout, type(os.environ)
    seen: list[tuple[bool, bool, bool]] = []

    class _Untouched(_Install):
        def __call__(self) -> None:
            seen.append(
                (
                    os.stat is _REAL_STAT and os.lstat is _REAL_LSTAT,
                    type(os.environ) is environ_class,
                    sys.stdout is stdout,
                )
            )
            super().__call__()

    run = _Untouched(project)
    for _ in range(3):
        _install(run)
    assert run.calls == 3
    assert seen == [(True, True, True)] * 3, "no fast-path code may wrap an unflagged install"
    assert discovered == []
    assert _stamps() == []


def test_only_a_plain_reinstall_is_eligible(monkeypatch) -> None:
    monkeypatch.setenv("APM_INSTALL_FAST_PATH", "1")
    monkeypatch.setattr(sys, "argv", ["apm", "install"])
    assert fp.eligible({"frozen": True}) is (sys.platform != "win32")
    for option in ("packages", "update", "refresh", "dry_run", "root", "global_", "no_policy"):
        assert fp.eligible({option: ("x",)}) is False
    assert fp.eligible({"mcp_name": "srv"}) is False
    monkeypatch.setattr(sys, "argv", ["apm", "install", "--policy", "./org.yml"])
    assert fp.eligible({}) is False


@_POSIX_ONLY
def test_a_parent_component_path_records_no_stamp(project) -> None:
    class _ParentInstall(_Install):
        def __call__(self) -> None:
            (self.root / ".." / self.root.name / "input.txt").read_text(encoding="utf-8")
            super().__call__()

    run = _ParentInstall(project)
    for _ in range(3):
        _install(run)
    assert run.calls == 3
    assert _stamps() == []


@_POSIX_ONLY
def test_an_existing_cache_root_is_not_chmodded(tmp_path: Path, monkeypatch) -> None:
    """The cache owner leaves an existing 0o700 root alone, so a no-op can stay write-free."""
    from apm_cli.cache.paths import get_cache_root

    root = tmp_path / "cache"
    root.mkdir()
    root.chmod(0o700)
    before = root.stat().st_ctime_ns
    monkeypatch.setenv("APM_CACHE_DIR", str(root))
    assert get_cache_root() == root.resolve()
    assert root.stat().st_ctime_ns == before, "an unchanged cache root must not be chmodded"
    root.chmod(0o755)
    get_cache_root()
    assert (root.stat().st_mode & 0o777) == 0o700, "a loosened cache root is still tightened"
