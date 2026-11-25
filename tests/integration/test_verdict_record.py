"""Verified-state record (#4977): a no-op replays only what a verified run recorded.

Component cells over a real filesystem: the recorder runs only after the engine
proved a no-op, a hit replays the exact output and writes nothing, and every
verdict input it summarizes (manifest, lock, deployed bytes, governed listings,
APM_HOME state, policy cache, allowlisted environment) misses when it changes.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

from apm_cli.install import verdict_attest, verdict_record

pytestmark = [pytest.mark.component, pytest.mark.promise("P-INV-I2")]

_REL = ".github/prompts/a.prompt.md"
_BODY = b"# prompt\n"
_COMMIT = "a" * 40
# The lock's only package is declared: a lock row apm.yml dropped is an orphan.
_MANIFEST = "name: demo\nversion: 1.0.0\ndependencies:\n  apm:\n    - o/p\n"
_OUTPUT = "[>] Installing dependencies from apm.yml...\n[i] No changes -- install state already up to date in 0.9s.\n"


def _snapshot(*roots: Path) -> dict[str, bytes]:
    return {
        str(path): path.read_bytes()
        for root in roots
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    (root / ".github" / "prompts").mkdir(parents=True)
    (root / _REL).write_bytes(_BODY)
    (root / "apm_modules" / "o" / "p").mkdir(parents=True)
    (root / "apm_modules" / "o" / "p" / "apm.yml").write_text("name: p\n", encoding="utf-8")
    (root / "apm.yml").write_text(_MANIFEST, encoding="utf-8")
    from apm_cli.utils.content_hash import compute_package_hash

    digest = "sha256:" + hashlib.sha256(_BODY).hexdigest()
    package_hash = compute_package_hash(root / "apm_modules" / "o" / "p", framed=True)
    from apm_cli.install.cache_pin import write_marker

    write_marker(root / "apm_modules" / "o" / "p", _COMMIT)
    (root / "apm.lock.yaml").write_text(
        "lockfile_version: '4'\ndependencies:\n- repo_url: o/p\n"
        f"  resolved_commit: {_COMMIT}\n"
        f"  content_hash: {package_hash}\n  content_hash_framed: true\n"
        f"  deployed_file_hashes:\n    {_REL}: {digest}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("APM_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "user").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    for name in (
        "XDG_CONFIG_HOME",
        "CI",
        "GITHUB_ACTIONS",
        "APM_INSTALL_FAST_PATH",
        "APM_VERBOSE",
        "APM_POLICY_DISABLE",
        "APM_E2E_TESTS",
    ):
        monkeypatch.delenv(name, raising=False)
    _update_checked(-60)  # the daily update check ran a minute ago
    monkeypatch.chdir(root)
    return root


def _update_checked(offset: float) -> Path:
    """Set when the update notice's daily check last ran, *offset* seconds from now."""
    import time

    from apm_cli.utils.version_checker import get_update_cache_path

    checked = get_update_cache_path()
    checked.touch()
    os.utime(checked, (time.time() + offset, time.time() + offset))
    return checked


def _record(text: str = _OUTPUT, *, verified: bool = True, argv: list[str] | None = None) -> None:
    with verdict_record.recording(argv or ["install"]):
        sys.stdout.write(text)
        if verified:
            verdict_record.note_verified_noop()


def _home(project: Path) -> Path:
    return project.parent / "home"


def test_a_hit_replays_the_recorded_output_and_writes_nothing(project: Path, capsys) -> None:
    _record()
    capsys.readouterr()
    before = _snapshot(project, _home(project))

    assert verdict_record.replay(["install"]) is True

    out = capsys.readouterr().out
    assert out.startswith("[>] Installing dependencies from apm.yml...\n")
    assert out.endswith("s.\n") and "up to date in " in out
    assert _snapshot(project, _home(project)) == before


def test_nothing_is_recorded_without_a_verified_no_op(project: Path) -> None:
    _record(verified=False)

    assert verdict_record.replay(["install"]) is False
    assert not (_home(project) / "verdicts").exists()


def test_output_with_another_clock_token_is_never_recorded(project: Path, capsys) -> None:
    _record(_OUTPUT + "[i] Org policy (cached, fetched 3m ago)\n")
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""
    assert not (_home(project) / "verdicts").exists()


@pytest.mark.parametrize(
    "change",
    [
        "manifest",
        "lock",
        "deployed bytes",
        "package copy",
        "unmanaged file",
        "approvals",
        "policy cache",
        "allowlisted env",
        "argv",
    ],
)
def test_any_verdict_input_change_misses(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys, change: str
) -> None:
    _record()
    if change == "manifest":
        (project / "apm.yml").write_text("name: demo\nversion: 1.0.0\n# edited\n", encoding="utf-8")
    elif change == "lock":
        with (project / "apm.lock.yaml").open("a", encoding="utf-8") as handle:
            handle.write("# edited\n")
    elif change == "deployed bytes":
        (project / _REL).write_bytes(b"# tampered\n")
    elif change == "package copy":
        (project / "apm_modules" / "o" / "p" / "apm.yml").write_text("name: q\n", encoding="utf-8")
    elif change == "unmanaged file":
        (project / ".github" / "prompts" / "extra.prompt.md").write_text("x\n", encoding="utf-8")
    elif change == "approvals":
        (_home(project) / "approvals.yml").write_text("approved: [o/p]\n", encoding="utf-8")
    elif change == "policy cache":
        policy = project.parent / "cache" / "policy_v1" / "k"
        policy.mkdir(parents=True)
        (policy / "p.yml").write_text("enforcement: block\n", encoding="utf-8")
    elif change == "allowlisted env":
        monkeypatch.setenv("APM_VERBOSE", "1")
    capsys.readouterr()

    argv = ["install", "--frozen"] if change == "argv" else ["install"]
    assert verdict_record.replay(argv) is False
    assert capsys.readouterr().out == ""


# Executable gates: install re-judges the lock's recorded exec_status, which none of
# these moves, so the record's key is their only control (#4770, W3d R1).
_GATE_CHANGES = (
    "user exec deny",
    "project exec deny",
    "revoked approval",
    "user deny after approval",
    "org exec deny-all",
)
_APPROVED_FIRST = frozenset({"revoked approval", "user deny after approval"})


def _user_executables(project: Path, **store: list[str]) -> None:
    import json

    config = {"executables": {"allow": [], "deny": [], **store}}
    _home(project).mkdir(exist_ok=True)
    (_home(project) / "config.json").write_text(json.dumps(config) + "\n", encoding="utf-8")


def _gate_change(project: Path, change: str) -> None:
    if change in ("user exec deny", "user deny after approval"):
        _user_executables(project, deny=["o/p"])
    elif change == "project exec deny":
        (project / "apm.yml").write_text(
            "name: demo\nversion: 1.0.0\nexecutables:\n  deny:\n    - o/p\n", encoding="utf-8"
        )
    elif change == "revoked approval":
        _user_executables(project)
    elif change == "org exec deny-all":
        policy = project.parent / "cache" / "policy_v1" / "k"
        policy.mkdir(parents=True)
        (policy / "p.yml").write_text("executables:\n  deny_all: true\n", encoding="utf-8")


@pytest.mark.parametrize("change", _GATE_CHANGES)
def test_an_executables_gate_change_misses_the_replay(project: Path, capsys, change: str) -> None:
    """apm.yml, the lock and every deployed byte are unchanged; the gate input is not."""
    if change in _APPROVED_FIRST:
        _user_executables(project, allow=["o/p"])
    _record()
    capsys.readouterr()
    assert verdict_record.replay(["install"]) is True, "positive control: run 2 hits"
    capsys.readouterr()

    _gate_change(project, change)

    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("change", _GATE_CHANGES)
def test_an_executables_gate_change_misses_the_attested_hit(project: Path, change: str) -> None:
    """The attested hit re-judges the lock's exec_status; the gate input must miss the key."""
    if change in _APPROVED_FIRST:
        _user_executables(project, allow=["o/p"])
    _attest(project)
    assert _attested_hit() is True, "positive control: run 2 matches the attestation"

    _gate_change(project, change)

    _assert_proves_itself()


def test_an_unrelated_environment_variable_still_hits(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _record()
    monkeypatch.setenv("APM_PERF_SPAWN_LOG", "/tmp/spawns.jsonl")
    monkeypatch.setenv("SOME_UNRELATED_TOOL_FLAG", "1")
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is True
    assert "up to date in " in capsys.readouterr().out


@pytest.mark.parametrize("name", ["CI", "GITHUB_ACTIONS", "PROXY_REGISTRY_URL"])
def test_ci_proxy_or_no_policy_never_hits_or_records(
    project: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "1")
    _record()
    assert not (_home(project) / "verdicts").exists()
    monkeypatch.delenv(name)
    _record()
    monkeypatch.setenv(name, "1")
    assert verdict_record.replay(["install"]) is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX key mode")
def test_a_key_readable_by_others_is_no_key(project: Path, capsys) -> None:
    _record()
    key = _home(project) / verdict_record.KEY_FILE
    key.chmod(0o644)
    capsys.readouterr()
    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""


def test_a_record_planted_under_another_key_misses(project: Path, capsys) -> None:
    _record()
    key = _home(project) / verdict_record.KEY_FILE
    key.write_bytes(b"\x01" * 32)
    capsys.readouterr()
    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""


def test_the_kill_switch_forces_the_full_path(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """APM_INSTALL_FAST_PATH=0 (the documented fast-path kill switch) never replays or records."""
    from apm_cli.install import noop_diff

    assert verdict_record._KILL_VALUES == noop_diff._DISABLED
    _record()
    monkeypatch.setenv("APM_INSTALL_FAST_PATH", "0")
    capsys.readouterr()
    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""
    records = sorted((_home(project) / "verdicts").iterdir())
    before = [p.read_bytes() for p in records]
    _record("changed\n")
    assert [p.read_bytes() for p in records] == before


def _env_names_read(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and node.args:
            called = ast.unparse(node.func)
            if called in {"os.environ.get", "os.getenv", "environ.get", "os.environ.pop"}:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names.add(arg.value)
        if isinstance(node, ast.Subscript) and ast.unparse(node.value) in {"os.environ", "environ"}:
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                names.add(node.slice.value)
    return names


def test_every_environment_variable_the_cli_reads_is_classified() -> None:
    """Closed world: a new env read must be allowlisted (a verdict input) or declared not one."""
    src = Path(verdict_record.__file__).resolve().parents[1]
    read: set[str] = set()
    for path in src.rglob("*.py"):
        read |= _env_names_read(path.read_text(encoding="utf-8"))
    classified = set(verdict_record.ENV_ALLOWLIST) | set(verdict_record.ENV_NOT_VERDICT)
    # Named only by their owners (architecture guards), and keyed by a run that reads
    # them (_env_log). get_apm_home: the key and records live there, so another home
    # holds no valid record. registry_proxy: a configured proxy never hits.
    classified |= {"APM_HOME", "APM_POLICY_DISABLE", "PROXY_REGISTRY_ALLOW_HTTP"}
    assert sorted(read - classified) == []


def test_the_record_module_reads_the_environment_only_through_its_classified_helper() -> None:
    tree = ast.parse(Path(verdict_record.__file__).read_text(encoding="utf-8"))
    for func in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        if func.name in {"_env", "_env_prefixed", "_env_log", "_env_snapshot"}:
            continue
        for node in ast.walk(func):
            assert "os.environ" not in ast.unparse(node) or not isinstance(
                node, (ast.Call, ast.Subscript, ast.Attribute)
            ), f"{func.name} reads os.environ directly"


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL")
def test_a_windows_key_readable_by_everyone_is_no_key(project: Path, capsys) -> None:
    import subprocess

    _record()
    key = _home(project) / verdict_record.KEY_FILE
    subprocess.run(["icacls", str(key), "/grant", "*S-1-1-0:R"], check=True, capture_output=True)
    capsys.readouterr()
    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL")
def test_a_windows_key_created_by_apm_is_private(project: Path) -> None:
    from apm_cli.install import verdict_key_windows

    _record()
    key = _home(project) / verdict_record.KEY_FILE
    assert verdict_key_windows.private_to_current_user(key)
    assert key.stat().st_size == 32


def test_a_record_expires_with_its_oldest_cached_policy(project: Path, capsys) -> None:
    """Valid at most for the code-capped policy TTL, so a new org policy always reaches a hit."""
    import json
    import time

    meta = project.parent / "cache" / "policy_v1" / "k" / "src.meta.json"
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"cached_at": time.time() - 2 * 3600}), encoding="utf-8")
    _record()
    capsys.readouterr()
    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""


def test_a_home_inside_the_project_never_records_or_hits(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A key or record shipped in a checkout must never authenticate anything (D2)."""
    monkeypatch.setenv("APM_HOME", str(project / ".apm-home"))
    _record()
    capsys.readouterr()
    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""
    assert not (project / ".apm-home" / "verdicts").exists()


def test_an_owner_without_a_known_no_op_form_records_nothing(project: Path, capsys) -> None:
    """Registry MCP, MCP updates, configured servers: a safe miss, never a guessed text."""
    with verdict_record.recording(["install"]):
        sys.stdout.write(_OUTPUT)
        verdict_record.note_verified_noop()
        verdict_record.unknown("registry mcp")
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""
    assert not (_home(project) / "verdicts").exists()


def test_every_target_root_and_mcp_config_path_is_a_verdict_input(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Coverage (W3d R4): each file an MCP adapter names, in or out of the project, misses."""
    from apm_cli.factory import ClientFactory
    from apm_cli.integration.targets import KNOWN_TARGETS

    user = project.parent / "user"
    for name in ("LOCALAPPDATA", "APPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "HERMES_HOME"):
        monkeypatch.setenv(name, str(user / name.lower()))
    roots = {
        profile.root_dir
        for profile in KNOWN_TARGETS.values()
        if profile.root_dir.startswith(".") or profile.root_dir == "agent-data"
    }
    assert sorted(roots - set(verdict_record._GOVERNED_DIRS)) == []
    for runtime in sorted(ClientFactory.supported_clients()):
        adapter = ClientFactory.create_client(runtime, project_root=project, user_scope=False)
        path = Path(os.path.expanduser(str(adapter.get_config_path())))
        path.parent.mkdir(parents=True, exist_ok=True)
        _record()
        before = path.read_bytes() if path.exists() else None
        path.write_bytes((before or b"") + b"\n")
        capsys.readouterr()
        verdict_record.replay(["install"])
        assert capsys.readouterr().out == "", f"{runtime}: {path} is not a verdict input"
        if before is None:
            path.unlink()
        else:
            path.write_bytes(before)


def test_a_variable_the_run_read_by_name_misses_and_never_leaves_its_value(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Names built at run time (MCP ${VAR} placeholders) are keyed by the run's own reads."""
    monkeypatch.setenv("MY_MCP_TOKEN", "s3cr3t-token-value")
    monkeypatch.setenv("UNREAD_TOOL_FLAG", "1")
    with verdict_record.recording(["install"]):
        assert os.environ.get("MY_MCP_TOKEN") == "s3cr3t-token-value"
        sys.stdout.write(_OUTPUT)
        verdict_record.note_verified_noop()
    records = list((_home(project) / "verdicts").iterdir())
    assert b"s3cr3t-token-value" not in records[0].read_bytes()
    monkeypatch.setenv("UNREAD_TOOL_FLAG", "2")
    capsys.readouterr()
    verdict_record.replay(["install"])
    assert "up to date in " in capsys.readouterr().out
    monkeypatch.setenv("MY_MCP_TOKEN", "rotated")
    verdict_record.replay(["install"])
    assert capsys.readouterr().out == ""


def test_git_config_a_child_git_reads_is_a_verdict_input(project: Path, capsys) -> None:
    """The no-op's `git remote` honors url.insteadOf: a hit keys every git config scope."""
    _record()
    gitconfig = project.parent / "user" / ".gitconfig"
    gitconfig.write_text('[url "https://mirror.example/"]\n\tinsteadOf = https://github.com/\n')
    capsys.readouterr()
    verdict_record.replay(["install"])
    assert capsys.readouterr().out == ""


def test_a_git_config_include_is_followed_and_keyed(project: Path, capsys) -> None:
    """An [include] (or [includeIf]) file is read by git, so its bytes are a verdict input."""
    user = project.parent / "user"
    (user / "extra.gitconfig").write_text("[core]\n\tautocrlf = false\n", encoding="utf-8")
    (user / ".gitconfig").write_text(
        '[includeIf "gitdir:~/work/"]\n\tpath = extra.gitconfig\n', encoding="utf-8"
    )
    _record()
    capsys.readouterr()
    verdict_record.replay(["install"])
    assert "up to date in " in capsys.readouterr().out
    (user / "extra.gitconfig").write_text(
        '[url "https://mirror.example/"]\n\tinsteadOf = https://github.com/\n', encoding="utf-8"
    )
    verdict_record.replay(["install"])
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "config",
    [
        "[include]\n\tpath = extra.gitconfig ; a comment\n",
        '[include]\n\tpath = "C:\\\\Users\\\\me\\\\extra.gitconfig"\n',
        "[include] path = extra.gitconfig\n",
        "[include]\n\tpath = %(prefix)/etc/extra.gitconfig\n",
        '[include]\n\tpath = "extra"".gitconfig"\n',
        '[includeIf "gitdir:a]b/"]\n\tpath = extra.gitconfig\n',
    ],
    ids=[
        "inline-comment",
        "quoted-backslash",
        "key-on-header",
        "prefix-form",
        "inner-quote",
        "ambiguous-header",
    ],
)
def test_include_syntax_the_reader_does_not_parse_is_never_recorded(
    project: Path, config: str
) -> None:
    """W3d R3: an include value the lexical reader could under-key fails closed."""
    (project.parent / "user" / ".gitconfig").write_text(config, encoding="utf-8")
    _record()
    assert not (_home(project) / "verdicts").exists()


@pytest.mark.parametrize("row", ["  source: local\n", "  virtual_path: skills/x\n"])
def test_a_package_copy_a_hit_cannot_prove_is_never_recorded(project: Path, row: str) -> None:
    with (project / "apm.lock.yaml").open("a", encoding="utf-8") as handle:
        handle.write(row)
    _record()
    assert not (_home(project) / "verdicts").exists()


def test_the_run_s_own_environment_changes_are_not_keyed(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The CLI sets PYTHONIOENCODING on Windows (cli._configure_encoding); a hit sees the
    state before the CLI runs, so the record keys the run's entry state."""
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    with verdict_record.recording(["install"]):
        monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
        sys.stdout.write(_OUTPUT)
        verdict_record.note_verified_noop()
    os.environ.pop("PYTHONIOENCODING")  # the CLI process ends; the next run starts without it
    capsys.readouterr()
    verdict_record.replay(["install"])
    assert "up to date in " in capsys.readouterr().out


@pytest.mark.parametrize("variable", ["GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_DIR"])
def test_a_redirected_git_config_is_keyed_by_its_bytes(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys, variable: str
) -> None:
    """W3d R7: git reads the file a GIT_* redirect names, so its bytes are a verdict input."""
    target = project.parent / "redirect"
    target.mkdir()
    config = target / "config" if variable == "GIT_DIR" else target / "gitconfig"
    config.write_text("[core]\n\tautocrlf = false\n", encoding="utf-8")
    monkeypatch.setenv(variable, str(target if variable == "GIT_DIR" else config))
    if variable == "GIT_DIR":
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")  # git var refuses a GIT_DIR that is no repo
    _record()
    capsys.readouterr()
    verdict_record.replay(["install"])
    assert "up to date in " in capsys.readouterr().out
    config.write_text('[url "https://mirror.example/"]\n\tinsteadOf = https://github.com/\n')
    verdict_record.replay(["install"])
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "env",
    [
        {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "include.path", "GIT_CONFIG_VALUE_0": "x"},
        {"GIT_CONFIG_PARAMETERS": "'include.path'='x'"},
    ],
    ids=["key-n", "parameters"],
)
def test_an_environment_injected_git_include_is_never_recorded(
    project: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _record()
    assert not (_home(project) / "verdicts").exists()


def test_the_system_git_config_is_the_one_git_names(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W3d R7: the system file is asked of git (`git var GIT_CONFIG_SYSTEM`), never guessed."""
    import json
    import subprocess

    from apm_cli.utils.git_env import get_git_executable

    for name in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_SYSTEM"):
        monkeypatch.delenv(name, raising=False)
    asked = subprocess.run(
        [get_git_executable(), "var", "GIT_CONFIG_SYSTEM"],
        capture_output=True,
        text=True,
        check=False,
    )
    if asked.returncode != 0:
        pytest.skip("git older than 2.42 cannot name its system config")
    _record()
    (record,) = (_home(project) / "verdicts").iterdir()
    assert json.loads(record.read_text(encoding="utf-8"))["git_system"] == asked.stdout.strip()


def test_a_clock_moved_back_before_the_record_misses(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """W3d R8: a clock earlier than the record's issue time (beyond skew) never hits."""
    import time

    _record()
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 2 * verdict_record._CLOCK_SKEW)
    capsys.readouterr()
    verdict_record.replay(["install"])
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(("ttl", "hits"), [(300, False), (3600, True)])
def test_a_record_expires_when_discovery_would_refetch_its_policy(
    project: Path, capsys, ttl: int, hits: bool
) -> None:
    """The policy's own cache.ttl (capped), not the cap alone, bounds a hit (W3d R2)."""
    import json
    import time

    cache = project.parent / "cache" / "policy_v1"
    cache.mkdir(parents=True)
    (cache / "k.yml").write_text(f"enforcement: warn\ncache:\n  ttl: {ttl}\n", encoding="utf-8")
    (cache / "k.meta.json").write_text(
        json.dumps({"cached_at": time.time() - 600}), encoding="utf-8"
    )
    _record()
    capsys.readouterr()
    verdict_record.replay(["install"])
    assert ("up to date in " in capsys.readouterr().out) is hits


def test_a_policy_age_replays_as_its_cache_entry_s_age_now(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """QL O1: the policy line's cache age is templated, never replayed as recorded."""
    import json
    import time

    cache = project.parent / "cache" / "policy_v1"
    cache.mkdir(parents=True)
    (cache / "k.yml").write_text("enforcement: block\ncache:\n  ttl: 3600\n", encoding="utf-8")
    (cache / "k.meta.json").write_text(json.dumps({"cached_at": time.time() - 180}), "utf-8")
    line = "[i] Policy: org:acme/.github (cached, fetched 3m ago) -- enforcement=block\n"
    _record(line + _OUTPUT)
    (record,) = (_home(project) / "verdicts").iterdir()
    assert "fetched 3m ago" not in record.read_text(encoding="utf-8")
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 120)
    capsys.readouterr()
    verdict_record.replay(["install"])
    assert "(cached, fetched 5m ago) -- enforcement=block" in capsys.readouterr().out


def test_a_run_with_lifecycle_scripts_is_never_recorded(project: Path) -> None:
    """W3d R1: scripts fire on every install; a replay fires none, so nothing is recorded."""
    from apm_cli.core.lifecycle_scripts import discover_scripts

    (project / "apm.yml").write_text(
        "name: demo\nversion: 1.0.0\nlifecycle:\n  post-install:\n    - run: echo done\n",
        encoding="utf-8",
    )
    assert discover_scripts(str(project))
    _record()
    assert not (_home(project) / "verdicts").exists()


@pytest.mark.skipif(os.name != "nt", reason="the POSIX policy directory is /etc/apm/policy.d")
def test_a_policy_script_added_after_the_record_misses(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The admin policy-script directory (PROGRAMDATA/APM/policy.d) is a verdict input."""
    program_data = project.parent / "programdata"
    (program_data / "APM" / "policy.d").mkdir(parents=True)
    monkeypatch.setenv("PROGRAMDATA", str(program_data))
    _record()
    (program_data / "APM" / "policy.d" / "audit.json").write_text("{}", encoding="utf-8")
    capsys.readouterr()
    verdict_record.replay(["install"])
    assert capsys.readouterr().out == ""


def test_the_hash_stream_follows_the_lock_owner(project: Path, capsys) -> None:
    """A version 1-3 lock verifies legacy package hashes, as the lock owner selects."""
    from apm_cli.deps.lockfile import FRAMED_CONTENT_HASH_VERSION
    from apm_cli.utils.content_hash import compute_package_hash

    assert verdict_record._FRAMED_LOCKFILE_VERSION == FRAMED_CONTENT_HASH_VERSION
    legacy = compute_package_hash(project / "apm_modules" / "o" / "p", framed=False)
    lock = (project / "apm.lock.yaml").read_text(encoding="utf-8")
    lock = re.sub(r"content_hash: \S+", f"content_hash: {legacy}", lock)
    lock = lock.replace("lockfile_version: '4'", "lockfile_version: '1'")
    lock = re.sub(r"\n\s*content_hash_framed: true", "", lock)
    (project / "apm.lock.yaml").write_text(lock, encoding="utf-8")
    _record()
    capsys.readouterr()
    verdict_record.replay(["install"])
    assert "up to date in " in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_a_symlink_inside_a_package_copy_misses(project: Path, capsys) -> None:
    """Package hashing skips links; the engine's source scan refuses them, so no hit."""
    _record()
    package = project / "apm_modules" / "o" / "p"
    (package / "link.md").symlink_to(package / "apm.yml")
    capsys.readouterr()
    verdict_record.replay(["install"])
    assert capsys.readouterr().out == ""


def test_root_apm_sources_are_never_recorded(project: Path) -> None:
    """Root .apm/ primitives are not verdict inputs: such a project never records."""
    (project / ".apm" / "prompts").mkdir(parents=True)
    _record()
    assert not (_home(project) / "verdicts").exists()


def test_a_lock_changed_after_the_proof_is_never_recorded(project: Path) -> None:
    """The record is written under the lifecycle lock only over the lock the run proved."""
    with verdict_record.recording(["install"]):
        sys.stdout.write(_OUTPUT)
        verdict_record.note_verified_noop()
        with (project / "apm.lock.yaml").open("a", encoding="utf-8") as handle:
            handle.write("# a concurrent install\n")
    assert not (_home(project) / "verdicts").exists()


def test_a_policy_governing_another_directory_is_never_recorded(project: Path) -> None:
    """policy.unmanaged_files.directories outside the keyed target roots: no record."""
    import json
    import time

    cache = project.parent / "cache" / "policy_v1"
    cache.mkdir(parents=True)
    (cache / "k.yml").write_text(
        "enforcement: warn\ncache:\n  ttl: 3600\nunmanaged_files:\n  action: deny\n"
        "  directories: [docs]\n",
        encoding="utf-8",
    )
    (cache / "k.meta.json").write_text(json.dumps({"cached_at": time.time()}), "utf-8")
    _record()
    assert not (_home(project) / "verdicts").exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_an_org_without_a_cached_policy_is_never_recorded(project: Path) -> None:
    """Discovery re-checks an uncached org every run; a record cannot stand in for it."""
    import subprocess

    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(
        ["git", "-C", str(project), "remote", "add", "origin", "https://github.com/acme/app.git"],
        check=True,
    )
    _record()
    assert not (_home(project) / "verdicts").exists()


def test_configuring_a_client_without_native_comparison_records_nothing(project: Path) -> None:
    """IntelliJ lists the server as already configured next time: an unknown no-op form."""
    from apm_cli.integration.mcp_install_summary import mark_unknown_noop_forms

    with verdict_record.recording(["install"]):
        sys.stdout.write(_OUTPUT)
        mark_unknown_noop_forms((), (), configuring=["srv"], uncompared=["intellij"])
        verdict_record.note_verified_noop()
    assert not (_home(project) / "verdicts").exists()


def test_a_manifest_edited_while_the_run_proved_it_is_never_recorded(project: Path) -> None:
    """W3d R4: an editor saving apm.yml mid-install: the next run installs, it never hits."""
    with verdict_record.recording(["install"]):
        sys.stdout.write(_OUTPUT)
        verdict_record.note_verified_noop()
        (project / "apm.yml").write_text(
            "name: demo\nversion: 1.0.0\ndependencies:\n  apm:\n    - o/q\n", encoding="utf-8"
        )
    assert not (_home(project) / "verdicts").exists()


def test_disabling_policy_after_the_record_misses(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Every install reads the policy switch through its owner, so the run keys it."""
    from apm_cli.policy.discovery import policy_disabled_by_env

    with verdict_record.recording(["install"]):
        assert policy_disabled_by_env() is False
        sys.stdout.write(_OUTPUT)
        verdict_record.note_verified_noop()
    monkeypatch.setenv("APM_POLICY_DISABLE", "1")
    capsys.readouterr()
    verdict_record.replay(["install"])
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("change", ["edited", "removed"])
def test_a_ca_bundle_changed_after_the_record_misses(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys, change: str
) -> None:
    """TLS startup loads the selected bundle and replay runs before it: its bytes are keyed."""
    bundle = project.parent / "extra-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\nA\n-----END CERTIFICATE-----\n", "ascii")
    monkeypatch.setenv("APM_EXTRA_CA_BUNDLE", str(bundle))
    _record()
    capsys.readouterr()
    assert verdict_record.replay(["install"]) is True
    if change == "edited":
        bundle.write_text("-----BEGIN CERTIFICATE-----\nB\n-----END CERTIFICATE-----\n", "ascii")
    else:
        bundle.unlink()
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""


def test_the_cli_is_imported_inside_the_record() -> None:
    """TLS startup reads its settings at import: the record's entry state and env log precede it."""
    from apm_cli import entrypoint

    tree = ast.parse(Path(entrypoint.__file__).read_text(encoding="utf-8"))
    (block,) = [node for node in ast.walk(tree) if isinstance(node, ast.With)]
    inside = [node.module for node in ast.walk(block) if isinstance(node, ast.ImportFrom)]
    outside = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "apm_cli.cli"
    ]
    assert inside == ["apm_cli.cli"] and outside == ["apm_cli.cli"]


@pytest.mark.skipif(os.name != "nt", reason="the POSIX policy directory is /etc/apm/policy.d")
def test_a_policy_script_edited_after_the_record_misses(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """An admin policy script's bytes are a verdict input, not only its name."""
    program_data = project.parent / "programdata"
    (program_data / "APM" / "policy.d").mkdir(parents=True)
    (program_data / "APM" / "policy.d" / "audit.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PROGRAMDATA", str(program_data))
    _record()
    assert (_home(project) / "verdicts").exists()
    (program_data / "APM" / "policy.d" / "audit.json").write_text(
        '{"post-install": [{"run": "echo audit"}]}', encoding="utf-8"
    )
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize(
    ("cached", "records"),
    [
        ("another checkout", False),
        ("a lower-priority candidate", False),
        ("the first candidate", True),
    ],
)
def test_only_a_fresh_first_candidate_cached_for_this_project_records(
    project: Path, cached: str, records: bool
) -> None:
    """Discovery fetches every candidate it has no fresh entry for, for this project."""
    import subprocess

    from apm_cli.policy import discovery
    from apm_cli.policy.parser import load_policy

    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(
        ["git", "-C", str(project), "remote", "add", "origin", "https://github.com/acme/app.git"],
        check=True,
    )
    policy, _warnings = load_policy("enforcement: warn\ncache:\n  ttl: 3600\n")
    ref, owner = "acme/.github-private", project
    if cached == "another checkout":
        owner = project.parent / "other"
    elif cached == "a lower-priority candidate":
        ref = "acme/.github"
    discovery._write_cache(ref, policy, owner)
    _record()

    assert (_home(project) / "verdicts").exists() is records


def test_a_recreated_checkout_misses(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Deployment receipts bind the root's identity: a byte-identical new checkout misses."""
    _record()
    copy = project.parent / "copy"
    shutil.copytree(project, copy, symlinks=True)
    monkeypatch.chdir(project.parent)
    shutil.rmtree(project)
    copy.rename(project)
    monkeypatch.chdir(project)
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("marker", ["deleted", "foreign"])
def test_a_package_copy_whose_pin_marker_is_not_current_misses(
    project: Path, capsys, marker: str
) -> None:
    """The content hash skips .apm-pin; the full run's marker sync repairs it, so a hit cannot."""
    from apm_cli.install.cache_pin import MARKER_FILENAME, write_marker

    _record()
    package = project / "apm_modules" / "o" / "p"
    if marker == "deleted":
        (package / MARKER_FILENAME).unlink()
    else:
        write_marker(package, "b" * 40)
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(("later", "hits"), [(1800, True), (7200, False)])
def test_a_record_expires_when_the_daily_update_check_falls_due(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys, later: int, hits: bool
) -> None:
    """A due update check may print a notice: the record never replays past it."""
    import time

    _update_checked(-23 * 3600)
    _record()
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + later)
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is hits
    assert ("up to date in " in capsys.readouterr().out) is hits


def _record_after_update_check(output: str) -> None:
    from apm_cli.utils.version_checker import save_version_check_timestamp

    with verdict_record.recording(["install"]):
        sys.stdout.write(output)
        save_version_check_timestamp()
        verdict_record.note_verified_noop()


def test_a_run_whose_update_check_printed_nothing_records(project: Path, capsys) -> None:
    """The first no-op of a day runs the update check; with nothing new it prints
    nothing, and so does the next run while the check is fresh: it records and replays."""
    _record_after_update_check(_OUTPUT)
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is True
    _assert_no_notice(capsys.readouterr().out)


def test_a_run_that_printed_the_update_notice_is_never_recorded(project: Path) -> None:
    """The next ordinary run would not print the notice, so its output is not recorded."""
    from apm_cli.update_notice import NEW_VERSION_NOTICE

    _record_after_update_check(f"[!] {NEW_VERSION_NOTICE}9.9.9 (current: 0.1.0)\n{_OUTPUT}")

    assert not (_home(project) / "verdicts").exists()


def test_a_wrapped_update_notice_is_never_recorded(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Copilot #5383: a narrow terminal wraps the notice; it printed all the same."""
    from apm_cli.update_notice import NEW_VERSION_NOTICE
    from apm_cli.utils import console
    from apm_cli.utils.version_checker import save_version_check_timestamp

    for name, value in (("FORCE_COLOR", "1"), ("NO_COLOR", "1"), ("COLUMNS", "24")):
        monkeypatch.setenv(name, value)  # a narrow terminal, as Rich reads it
    console._reset_console()
    with verdict_record.recording(["install"]):
        console._rich_warning(f"{NEW_VERSION_NOTICE}9.9.9 (current: 0.1.0)", symbol="warning")
        sys.stdout.write(_OUTPUT)
        save_version_check_timestamp()
        verdict_record.note_verified_noop()

    console._reset_console()
    assert NEW_VERSION_NOTICE not in capsys.readouterr().out, "the notice must wrap here"
    assert not (_home(project) / "verdicts").exists()


def _assert_no_notice(out: str) -> None:
    from apm_cli.update_notice import NEW_VERSION_NOTICE

    assert "up to date in " in out and NEW_VERSION_NOTICE not in out


def test_the_common_dir_config_git_reads_is_keyed(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """GIT_COMMON_DIR without GIT_DIR: a child git reads that directory's config."""
    common = project.parent / "common"
    common.mkdir()
    remote = '[remote "origin"]\n\turl = https://github.com/{}/app.git\n'
    (common / "config").write_text(remote.format("acme"), encoding="utf-8")
    monkeypatch.setenv("GIT_COMMON_DIR", str(common))
    _record()
    capsys.readouterr()
    assert verdict_record.replay(["install"]) is True
    (common / "config").write_text(remote.format("other"), encoding="utf-8")
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("excluded", ["__pycache__", ".git"])
def test_a_file_under_a_directory_the_package_hash_skips_misses(
    project: Path, capsys, excluded: str
) -> None:
    """content_hash skips these directories; the source scan may read files there."""
    _record()
    cache = project / "apm_modules" / "o" / "p" / ".apm" / "prompts" / excluded
    cache.mkdir(parents=True)
    (cache / "late.prompt.md").write_text("# late\n", encoding="utf-8")
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""


def test_an_edited_file_named_like_a_hash_skipped_directory_misses(project: Path, capsys) -> None:
    """content_hash skips any path part named __pycache__ or .git, files included."""
    odd = project / "apm_modules" / "o" / "p" / "__pycache__"
    odd.write_text("# before\n", encoding="utf-8")
    _record()
    odd.write_text("# after\n", encoding="utf-8")
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_inside_a_package_copy_misses(project: Path, capsys) -> None:
    """The hash skips non-regular files; a hit cannot prove what the scan would make of one."""
    _record()
    os.mkfifo(project / "apm_modules" / "o" / "p" / "pipe")
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "change",
    [
        "an orphan package directory",
        "a leftover fast-apply marker",
        "a client directory under HOME",
        "the receipts' gitignore",
        "a bare repository above the project",
    ],
)
def test_every_input_the_read_set_audit_found_misses(project: Path, capsys, change: str) -> None:
    """Inputs the recording no-op reads (strace read-set audit) that the record keys."""
    _record()
    capsys.readouterr()
    assert verdict_record.replay(["install"]) is True
    if change == "an orphan package directory":
        (project / "apm_modules" / "o" / "q").mkdir()
    elif change == "a leftover fast-apply marker":
        (project / "apm_modules" / ".apm-fast-apply").write_text("{}", encoding="utf-8")
    elif change == "a client directory under HOME":
        (project.parent / "user" / ".codeium" / "windsurf").mkdir(parents=True)
    elif change == "the receipts' gitignore":
        (project / ".apm-local").mkdir(exist_ok=True)
        (project / ".apm-local" / ".gitignore").write_text("*\n", encoding="utf-8")
    else:
        (project.parent / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_branch_switch_after_the_record_misses(project: Path, capsys) -> None:
    """The Copilot App integration records the project's branch name (from HEAD)."""
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(project)], check=True)
    _record()
    capsys.readouterr()
    assert verdict_record.replay(["install"]) is True
    (project / ".git" / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""


def test_a_linked_worktree_is_never_recorded(project: Path) -> None:
    """.git is a file naming the gitdir: the record fails closed (no record, so no hit),
    whatever that gitdir's HEAD says."""
    gitdir = project.parent / "gitdir"
    gitdir.mkdir()
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (project / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    _record()

    assert not (_home(project) / "verdicts").exists()


_HOSTED_REMOTES = {
    "gitlab": "https://gitlab.com/acme/app.git",
    "ado": "https://dev.azure.com/acme/proj/_git/app",
}


def _hosted_project(project: Path, host: str) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(
        ["git", "-C", str(project), "remote", "add", "origin", _HOSTED_REMOTES[host]], check=True
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize("host", sorted(_HOSTED_REMOTES))
def test_a_gitlab_or_ado_run_whose_policy_came_from_the_cache_records_and_hits(
    project: Path, capsys, host: str
) -> None:
    """#5075: discovery fetched nothing, so its result rests only on keyed cache state."""
    _hosted_project(project, host)
    _record()
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is True
    assert "up to date in " in capsys.readouterr().out


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize("host", sorted(_HOSTED_REMOTES))
def test_a_gitlab_or_ado_run_that_attempted_a_policy_fetch_never_records(
    project: Path, host: str
) -> None:
    """A fetch (or its credential lookup) this run means the next run fetches too."""
    from apm_cli.core.policy_fetch_ledger import note_fetch_attempt

    _hosted_project(project, host)
    with verdict_record.recording(["install"]):
        sys.stdout.write(_OUTPUT)
        note_fetch_attempt(host)
        verdict_record.note_verified_noop()

    assert not (_home(project) / "verdicts").exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_pinned_gitlab_project_never_records_even_with_a_fresh_cache(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A policy.hash pin makes GitLab discovery read the source, never the cache (W3d R1)."""
    from apm_cli.policy import discovery
    from apm_cli.policy.parser import load_policy

    def _offline(*_args, **_kwargs):
        raise OSError("offline")

    _hosted_project(project, "gitlab")
    (project / "apm.yml").write_text(
        f"name: demo\nversion: 1.0.0\npolicy:\n  hash: sha256:{'a' * 64}\n", encoding="utf-8"
    )
    policy, _warnings = load_policy("enforcement: warn\ncache:\n  ttl: 3600\n")
    discovery._write_cache("gitlab.com/acme/apm-policy", policy, project)
    monkeypatch.setattr("apm_cli.core.auth.AuthResolver.try_with_fallback", _offline)
    with verdict_record.recording(["install"]):
        with contextlib.suppress(OSError):  # the stubbed transport fails; the run goes on
            discovery.discover_policy(project, expected_hash=f"sha256:{'a' * 64}")
        sys.stdout.write(_OUTPUT)
        verdict_record.note_verified_noop()

    assert not (_home(project) / "verdicts").exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_policy_cache_changed_after_discovery_is_never_recorded(project: Path) -> None:
    """The cache discovery read must still be the one the record keys (Copilot, #5331)."""
    from apm_cli.policy import discovery
    from apm_cli.policy.parser import load_policy

    _hosted_project(project, "ado")
    policy, _warnings = load_policy("enforcement: warn\ncache:\n  ttl: 3600\n")
    discovery._write_cache("dev.azure.com/acme/apm/apm-policy", policy, project)
    with verdict_record.recording(["install"]):
        sys.stdout.write(_OUTPUT)
        verdict_record.note_verified_noop()
        shutil.rmtree(project.parent / "cache" / "policy_v1")

    assert not (_home(project) / "verdicts").exists()


def _cached_ado_project(project: Path) -> Path:
    from apm_cli.policy import discovery
    from apm_cli.policy.parser import load_policy

    _hosted_project(project, "ado")
    policy, _warnings = load_policy("enforcement: warn\ncache:\n  ttl: 3600\n")
    discovery._write_cache("dev.azure.com/acme/apm/apm-policy", policy, project)
    (cached,) = (p for p in (project.parent / "cache" / "policy_v1").rglob("*.yml"))
    return cached


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_policy_cache_rewritten_in_place_after_discovery_is_never_recorded(
    project: Path,
) -> None:
    """Same size and mtime, other bytes: the cache's content is bound too."""
    cached = _cached_ado_project(project)
    with verdict_record.recording(["install"]):
        sys.stdout.write(_OUTPUT)
        verdict_record.note_verified_noop()
        info = cached.stat()
        data = cached.read_bytes()
        cached.write_bytes(data.replace(b"warn", b"bloc"))
        os.utime(cached, ns=(info.st_atime_ns, info.st_mtime_ns))

    assert not (_home(project) / "verdicts").exists()


@pytest.mark.skipif(
    shutil.which("git") is None or os.name == "nt" or os.geteuid() == 0,
    reason="needs git and a POSIX non-root user",
)
def test_an_unreadable_policy_cache_at_entry_records_nothing_and_runs_on(
    project: Path, capsys
) -> None:
    """W3d R3: a cache the snapshot cannot read never aborts the install."""
    cached = _cached_ado_project(project)
    cached.chmod(0)
    try:
        with verdict_record.recording(["install"]):
            sys.stdout.write(_OUTPUT)
            verdict_record.note_verified_noop()
    finally:
        cached.chmod(0o600)

    assert capsys.readouterr().out == _OUTPUT
    assert not (_home(project) / "verdicts").exists()


def _target_line(project: Path) -> str:
    """The target line the full install announces for *project* (its owner's resolution)."""
    from apm_cli.core.target_detection import format_provenance, resolve_targets
    from apm_cli.models.apm_package import APMPackage, package_target_declaration

    manifest = APMPackage.from_apm_yml(project / "apm.yml")
    restriction = package_target_declaration(manifest).restriction()
    return format_provenance(resolve_targets(project, yaml_targets=restriction))


def _frozen(argv: list[str], frozen: bool | None = None) -> None:
    """The run's frozen decision, through its single reader (which notes it); *frozen*
    stands for an environment-implied one (CI with a lockfile selects frozen)."""
    from types import SimpleNamespace

    from apm_cli.install.service import InstallService

    decided = "--frozen" in argv if frozen is None else frozen
    InstallService.frozen_disposition(SimpleNamespace(frozen=decided))


def _attest(
    project: Path,
    line: str | None = None,
    during=None,
    printed: str = "",
    argv: tuple[str, ...] = ("install",),
) -> None:
    """A converged full install: it announced its targets and attested the state."""
    with verdict_record.recording(list(argv)):
        _frozen(list(argv))
        sys.stdout.write(printed + "[+] Installed 1 APM dependency.\n")
        verdict_attest.note_run_targets(line if line is not None else _target_line(project))
        verdict_attest.note_attested_install()
        if during is not None:
            during()


def _attested_hit(argv: list[str] | None = None, frozen: bool | None = None) -> bool:
    """What the next install's engine sees, inside its own recorded run."""
    with verdict_record.recording(argv or ["install"]):
        _frozen(argv or ["install"], frozen)
        return verdict_attest.attested_state(argv or ["install"], Path.cwd())


def _stored(project: Path) -> dict:
    import json

    (path,) = (_home(project) / "verdicts").rglob("*.json")
    return json.loads(path.read_text(encoding="utf-8"))


def test_an_attested_install_lets_the_next_unchanged_install_skip_the_proof(
    project: Path, capsys
) -> None:
    """W3d attested-record ruling: a converged full install attests what it committed;
    the next unchanged install matches it, and nothing replays its output (A5)."""
    _attest(project)
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""
    assert _attested_hit() is True
    assert (_stored(project)["kind"], _stored(project)["provenance"]) == ("attested", "attested")


@pytest.mark.parametrize(
    "change",
    [
        "manifest",
        "lock",
        "deployed bytes",
        "package copy",
        "unmanaged file",
        "planted link",
        "approvals",
        "policy cache",
        "allowlisted env",
        "lifecycle script",
    ],
)
def test_a_tree_changed_after_the_attested_install_never_skips_the_proof(
    project: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """A8: anything changed after run 1 makes run 2 prove its verdict itself."""
    _attest(project)
    prompts = project / ".github" / "prompts"
    if change == "manifest":
        (project / "apm.yml").write_text("name: demo\nversion: 1.0.0\n# edited\n", encoding="utf-8")
    elif change == "lock":
        with (project / "apm.lock.yaml").open("a", encoding="utf-8") as handle:
            handle.write("# edited\n")
    elif change == "deployed bytes":
        (project / _REL).write_bytes(b"# tampered\n")
    elif change == "package copy":
        (project / "apm_modules" / "o" / "p" / "apm.yml").write_text("name: q\n", encoding="utf-8")
    elif change == "unmanaged file":
        (prompts / "extra.prompt.md").write_text("x\n", encoding="utf-8")
    elif change == "planted link":
        if os.name == "nt":
            pytest.skip("symlink creation needs privileges on Windows")
        (prompts / "link.prompt.md").symlink_to(project / "apm.yml")
    elif change == "approvals":
        (_home(project) / "approvals.yml").write_text("approved: [o/p]\n", encoding="utf-8")
    elif change == "policy cache":
        policy = project.parent / "cache" / "policy_v1" / "k"
        policy.mkdir(parents=True)
        (policy / "p.yml").write_text("enforcement: block\n", encoding="utf-8")
    elif change == "allowlisted env":
        monkeypatch.setenv("APM_VERBOSE", "1")
    elif change == "lifecycle script":
        (project / "apm.yml").write_text(
            "name: demo\nversion: 1.0.0\nlifecycle:\n  post-install:\n    - run: echo done\n",
            encoding="utf-8",
        )

    _assert_proves_itself()


def test_a_target_set_widened_by_the_install_is_never_attested(project: Path) -> None:
    """A1: run 1 announced one target set, and discovery at its end finds another."""
    _attest(project, line="Targets: none  (source: test)")

    assert not (_home(project) / "verdicts").exists()


def test_a_run_with_lifecycle_scripts_is_never_attested(project: Path) -> None:
    """A4: scripts fire on every install; skipping the proof would skip them."""
    (project / "apm.yml").write_text(
        "name: demo\nversion: 1.0.0\nlifecycle:\n  post-install:\n    - run: echo done\n",
        encoding="utf-8",
    )
    _attest(project)

    assert not (_home(project) / "verdicts").exists()


def test_a_lock_changed_after_the_install_committed_is_never_attested(project: Path) -> None:
    """A6: the record keys only the lock bytes the install committed under its lock."""

    def edit() -> None:
        with (project / "apm.lock.yaml").open("a", encoding="utf-8") as handle:
            handle.write("# edited after commit\n")

    _attest(project, during=edit)

    assert not (_home(project) / "verdicts").exists()


def test_output_recorded_after_an_attested_hit_stays_attested(project: Path, capsys) -> None:
    """A5: provenance never upgrades; the replayable record a matched run writes
    says it rests on an attestation, not on a proof."""
    _attest(project)
    assert _attested_hit() is True
    _record_attested_noop()
    capsys.readouterr()

    assert (_stored(project)["kind"], _stored(project)["provenance"]) == ("output", "attested")
    assert verdict_record.replay(["install"]) is True
    assert "up to date in " in capsys.readouterr().out


def _record_attested_noop() -> None:
    with verdict_record.recording(["install"]):
        sys.stdout.write(_OUTPUT)
        verdict_record.note_verified_noop(verdict_record.ATTESTED)


def test_a_deferral_warning_the_install_printed_is_never_attested(project: Path) -> None:
    """A2 allowlist: a kept stale copy means the next run has work; it must prove itself."""
    _attest(project, printed="[!] Kept .claude/skills/x: it changed after the install.\n")

    assert not (_home(project) / "verdicts").exists()


def test_a_parked_mcp_notice_is_re_judged_live_and_still_attests(project: Path) -> None:
    """A2 allowlist: the services phase re-runs on every attested hit, so its notice may pass."""
    _attest(
        project,
        printed="  [!] Parked MCP server 'm' from transitive package o/p (self-defined, not approved)\n",
    )

    _assert_attested_record(project)


def test_an_unprefixed_warning_the_install_printed_is_never_attested(project: Path) -> None:
    """Copilot #5383: a warning printed without a symbol (a hook file that contributes no
    entries) still refuses the attestation, so the next install prints it again."""
    from apm_cli.utils.console import _rich_warning

    _attest(project, during=lambda: _rich_warning("Hook file h.json contributes no entries"))

    _assert_proves_itself()


_FROZEN = ["install", "--frozen"]


def test_a_plain_attestation_serves_the_first_frozen_install(project: Path) -> None:
    """#4770: CI's install-then-frozen path; the frozen refusals re-run on the hit."""
    _attest(project)

    assert _attested_hit(_FROZEN) is True
    _assert_keyed(project, ["install"], "plain")


def test_a_frozen_attestation_never_serves_a_plain_install(project: Path) -> None:
    """W3d AF1/AF5: the argv class is one-way."""
    _attest(project, argv=tuple(_FROZEN))
    assert _attested_hit(_FROZEN) is True, "positive control: frozen serves frozen"

    _assert_proves_itself()
    _assert_keyed(project, _FROZEN, "frozen")


def _assert_keyed(project: Path, argv: list[str], disposition: str) -> None:
    """The durable attested record names what it was keyed with (MAC-bound)."""
    stored = _stored(project)
    assert (stored["kind"], stored["argv"], stored["disposition"]) == (
        "attested",
        argv,
        disposition,
    )


def test_an_environment_implied_frozen_install_is_served_only_by_a_plain_record(
    project: Path,
) -> None:
    """W3d R2(c): argv ["install"] with a CI-implied frozen disposition accepts a plain
    ["install"] attestation and never one keyed ["install", "--frozen"]."""
    _attest(project)
    assert _attested_hit(["install"], frozen=True) is True

    _attest(project, argv=tuple(_FROZEN))

    assert _attested_hit(["install"], frozen=True) is False
    _assert_keyed(project, _FROZEN, "frozen")


@pytest.mark.parametrize("forged", [{"argv": ["install"]}, {"disposition": "plain"}])
def test_a_forged_attestation_argv_never_serves(project: Path, forged: dict) -> None:
    """The argv and frozen disposition are bound by the record's MAC."""
    import json

    _attest(project, argv=tuple(_FROZEN))
    (path,) = (_home(project) / "verdicts").rglob("*.json")
    path.write_text(json.dumps({**_stored(project), **forged}), encoding="utf-8")

    assert _attested_hit(_FROZEN) is False
    _assert_proves_itself()


@pytest.mark.parametrize("change", ["manifest", "lock", "deployed bytes", "package copy"])
def test_a_changed_input_never_serves_a_frozen_install(project: Path, change: str) -> None:
    _attest(project)
    assert _attested_hit(_FROZEN) is True, "positive control"
    if change == "manifest":
        (project / "apm.yml").write_text(_MANIFEST + "# edited\n", encoding="utf-8")
    elif change == "lock":
        with (project / "apm.lock.yaml").open("a", encoding="utf-8") as handle:
            handle.write("# edited\n")
    elif change == "deployed bytes":
        (project / _REL).write_bytes(b"# tampered\n")
    else:
        (project / "apm_modules" / "o" / "p" / "apm.yml").write_text("name: q\n", encoding="utf-8")

    assert _attested_hit(_FROZEN) is False
    _assert_keyed(project, ["install"], "plain")  # the record stays; only the hit misses


def test_a_frozen_hit_re_proves_every_declared_edge_from_the_lock(project: Path) -> None:
    """Copilot #5423: the attested gates run the frozen edge walk; an edge the lock does
    not hold declines a frozen hit and never a plain one (positive control)."""
    from types import SimpleNamespace

    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.diff_apply import Decline

    package = project / "apm_modules" / "o" / "p" / "apm.yml"
    package.write_text("name: p\nversion: 1.0.0\n", encoding="utf-8")  # a readable copy
    lock = LockFile.read(project / "apm.lock.yaml")
    before = lock_bytes(project)  # the frozen no-write invariant, judged against this
    assert verdict_attest.attested_gates(project, lock, None, SimpleNamespace(replay=True))
    from apm_cli.models.apm_package import clear_apm_yml_cache

    (project / "apm.yml").write_text(_MANIFEST + "    - o/unlocked\n", encoding="utf-8")
    clear_apm_yml_cache()  # a new run reads the edited manifest
    assert verdict_attest.attested_gates(project, lock, None, SimpleNamespace(replay=False))

    with pytest.raises(Decline, match="unlocked dependency edge"):
        verdict_attest.attested_gates(project, lock, None, SimpleNamespace(replay=True))
    assert lock_bytes(project) == before


def lock_bytes(project: Path) -> bytes:
    return (project / "apm.lock.yaml").read_bytes()


def test_every_frozen_only_owner_is_classified_for_an_attested_hit() -> None:
    """W3d AF2, closed world: the frozen owner row's modules and every frozen method of
    InstallService are classified live or implied (install/verdict_attest.py)."""
    import inspect
    import json

    from apm_cli.install.service import InstallService

    owners = json.loads(
        (Path(__file__).parents[2] / ".apm/architecture/owners/install-deployment.json").read_text(
            encoding="utf-8"
        )
    )["owners"]
    (row,) = (o for o in owners if o["id"] == "frozen-install-mutation-eligibility")
    modules = {Path(sel).stem for sel in row["selectors"]} - {"service"}
    methods = {
        f"service:{name}"
        for name, _ in inspect.getmembers(InstallService, inspect.isfunction)
        if "frozen" in name
    }
    classified = verdict_attest.FROZEN_OWNERS

    _assert_classified(modules | methods, classified)


def _assert_classified(names: set[str], classified: dict) -> None:
    missing = sorted(names - set(classified))
    assert not missing, f"classify these frozen owners in FROZEN_OWNERS: {missing}"
    assert all(kind in ("live", "implied") and why for kind, why in classified.values())


def _assert_proves_itself() -> None:
    """Run 2 does not match the attestation: the engine proves its verdict itself."""
    assert _attested_hit() is False


def _assert_attested_record(project: Path) -> None:
    """The durable attested record exists and run 2 matches it."""
    assert _stored(project)["kind"] == "attested"
    assert _attested_hit() is True


def _fake_package(root: Path, body: str) -> Path:
    package = root / "apm_cli"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(body, encoding="utf-8")
    return package


@pytest.mark.parametrize("layout", ["source", "installed"])
def test_changed_apm_code_at_the_same_version_misses(
    project: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    """Copilot #5383 (W3d A3): a pip or editable install runs under Python, so the
    record keys the APM code itself: a source tree's files or the installed RECORD."""
    import apm_cli

    site = project.parent / "site"
    package = _fake_package(site, "VERSION = 1\n")
    if layout == "installed":
        dist = site / "apm_cli-0.0.0.dist-info"
        dist.mkdir()
        (dist / "RECORD").write_text("apm_cli/__init__.py,sha256=old,12\n", encoding="utf-8")
    monkeypatch.setattr(apm_cli, "__file__", str(package / "__init__.py"))
    _record()
    _assert_replays(True)  # the same code still hits: the key is stable
    if layout == "installed":
        (dist / "RECORD").write_text("apm_cli/__init__.py,sha256=new,12\n", encoding="utf-8")
    else:
        (package / "__init__.py").write_text("VERSION = 1  # patched\n", encoding="utf-8")

    _assert_replays(False)


def test_an_editable_install_keys_its_source_files(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An editable install's RECORD does not cover its source, so the source content is
    keyed: an edit that keeps the size and the timestamps still misses."""
    import apm_cli

    site = project.parent / "site"
    package = _fake_package(site / "src", "VERSION = 1\n")
    dist = site / "apm_cli-0.0.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "RECORD").write_text("apm_cli/__init__.py,sha256=x,12\n", encoding="utf-8")
    (dist / "direct_url.json").write_text('{"dir_info": {"editable": true}}', encoding="utf-8")
    monkeypatch.setattr(apm_cli, "__file__", str(package / "__init__.py"))
    _record()
    _assert_replays(True)
    # The same size, and the old timestamps restored: only the content changed.
    source = package / "__init__.py"
    before = os.stat(source)
    source.write_text("VERSION = 2\n", encoding="utf-8")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))

    _assert_replays(False)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_an_unreadable_apm_source_tree_never_records(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W3d A3: with no readable implementation fingerprint, nothing is recorded (never
    a version-only key)."""
    import apm_cli

    package = _fake_package(project.parent / "site", "VERSION = 1\n")
    (package / "hidden.py").write_text("x = 1\n", encoding="utf-8")
    os.chmod(package / "hidden.py", 0)
    monkeypatch.setattr(apm_cli, "__file__", str(package / "__init__.py"))
    try:
        _record()
    finally:
        os.chmod(package / "hidden.py", 0o644)

    assert not (_home(project) / "verdicts").exists()


@pytest.mark.parametrize(
    ("printed", "attests"),
    [
        ("\x1b[33m[!] Kept .claude/skills/x: it changed after the install.\x1b[0m\n", False),
        ("\x1b]8;;https://example.invalid\x07[!] Kept x\x1b]8;;\x07\n", False),
        ("\x1b[33m  [!] Parked MCP server 'm' from transitive package o/p\x1b[0m\n", True),
    ],
    ids=["colored-kept", "unknown-escape", "colored-parked"],
)
def test_terminal_styling_never_hides_a_deferral_warning(
    project: Path, printed: str, attests: bool
) -> None:
    """Copilot #5383 (A2): styling is stripped before a line is judged; an escape
    the record does not understand blocks the attestation."""
    _attest(project, printed=printed)

    assert (_home(project) / "verdicts").exists() is attests


def _assert_replays(expected: bool) -> None:
    assert verdict_record.replay(["install"]) is expected


# #5434 (RT1, W3d): the full run decides Claude rules from carriers -- the root
# AGENTS.md / CLAUDE.md, the compile record, and a directory's AGENTS.md for a
# routable instruction. The replay never re-decides them: it declines while a root
# carrier or record may stand in (compiled_agents_md_may_carry's owner), a run that
# decided routing records nothing, and the engine declines for a routable instruction.
_CARRIER = "## Global Instructions\n\nAlways run the linter.\n"


def _nested_carrier(project: Path) -> None:
    nested = project / "src" / "test"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "AGENTS.md").write_text(_CARRIER, encoding="utf-8")
    (nested / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md"])
def test_a_root_carrier_appearing_after_the_record_misses(project: Path, capsys, name: str) -> None:
    _record()
    (project / name).write_text(_CARRIER, encoding="utf-8")
    capsys.readouterr()

    _assert_replays(False)


def test_a_compile_record_appearing_after_the_record_misses(project: Path, capsys) -> None:
    from apm_cli.core.agents_md_compile_record import write_record

    _record()
    write_record(project, {"AGENTS.md": {"sha256": "sha256:0", "instructions": []}})
    capsys.readouterr()

    _assert_replays(False)


def test_a_root_carrier_removed_after_the_record_misses(project: Path, capsys) -> None:
    """A run with a root carrier writes no replayable record, so removing it cannot hit."""
    (project / "AGENTS.md").write_text(_CARRIER, encoding="utf-8")
    (project / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
    _record()
    assert not verdict_record._record_path(project).exists()
    (project / "AGENTS.md").unlink()
    capsys.readouterr()

    _assert_replays(False)


def test_control_with_no_carrier_the_record_still_replays(project: Path, capsys) -> None:
    _record()
    capsys.readouterr()

    _assert_replays(True)


def _decide_claude_routing(project: Path, apply_to: str) -> None:
    """The instruction integrator's Claude routing step for one instruction."""
    from types import SimpleNamespace

    from apm_cli.core.scope import InstallScope
    from apm_cli.integration.instruction_integrator import InstructionIntegrator
    from apm_cli.integration.targets import KNOWN_TARGETS

    source = project / "apm_modules" / "o" / "p" / ".apm" / "instructions" / "t.instructions.md"
    content = f"---\napplyTo: '{apply_to}'\n---\nRun the tests.\n"
    prepared = {source: SimpleNamespace(content=content, body="Run the tests.\n")}
    InstructionIntegrator()._claude_rule_sources(
        KNOWN_TARGETS["claude"],
        [source],
        prepared,
        SimpleNamespace(),
        project,
        InstallScope.PROJECT,
        None,
    )


@pytest.mark.parametrize(
    ("apply_to", "replays"),
    [("**", False), ("src/test/**", False), ("**/*.py", True)],
)
def test_a_run_that_decided_claude_routing_records_nothing(
    project: Path, capsys, apply_to: str, replays: bool
) -> None:
    """A routable instruction (always-on or one literal directory, even while that
    directory is absent) means the run decided routing: nothing is recorded, so a
    carrier created later (src/test/AGENTS.md importing CLAUDE.md) cannot be skipped.
    A path-scoped instruction never routes: its no-op still records and replays."""
    with verdict_record.recording(["install"]):
        sys.stdout.write(_OUTPUT)
        _decide_claude_routing(project, apply_to)
        verdict_record.note_verified_noop()
    _nested_carrier(project)
    capsys.readouterr()

    _assert_replays(replays)


def _ship_instruction(project: Path, apply_to: str) -> None:
    instructions = project / "apm_modules" / "o" / "p" / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "t.instructions.md").write_text(
        f"---\napplyTo: '{apply_to}'\n---\nRun the tests.\n", encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("apply_to", "carrier", "declines"),
    [
        ("src/test/**", True, True),
        ("src/test/**", False, True),  # the directory does not exist yet: still routable
        ("**", False, True),
        ("**/*.py", True, False),
        (None, True, False),  # no instruction: a nested carrier decides nothing
        ("missing-copy", False, True),  # a copy the engine would restore: unknown counts
    ],
)
def test_the_engine_fast_path_declines_for_a_routable_instruction(
    project: Path, apply_to: str | None, carrier: bool, declines: bool
) -> None:
    from types import SimpleNamespace

    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install import diff_apply

    if apply_to == "missing-copy":
        shutil.rmtree(project / "apm_modules" / "o" / "p")
    elif apply_to is not None:
        _ship_instruction(project, apply_to)
    if carrier:
        _nested_carrier(project)
    lock = LockFile.read(project / "apm.lock.yaml")
    claude = [SimpleNamespace(compile_family="claude")]

    if declines:
        with pytest.raises(diff_apply.Decline):
            diff_apply._refuse_claude_carried_rules(project, claude, lock)
    else:
        diff_apply._refuse_claude_carried_rules(project, claude, lock)
    # Another target family never declines here.
    diff_apply._refuse_claude_carried_rules(project, [SimpleNamespace(compile_family="x")], lock)


def test_the_replay_and_the_engine_share_one_carrier_owner(project: Path, capsys) -> None:
    """Every root carrier the engine's predicate names also stops the record and the replay."""
    from apm_cli.compilation.claude_agents_md import compiled_agents_md_may_carry
    from apm_cli.core.agents_md_compile_record import ROOT_CARRIERS

    for name in ROOT_CARRIERS:
        (project / name).write_text(_CARRIER, encoding="utf-8")
        assert compiled_agents_md_may_carry(project)
        _record()
        capsys.readouterr()
        _assert_replays(False)
        (project / name).unlink()
    assert not compiled_agents_md_may_carry(project)
    _record()
    capsys.readouterr()
    _assert_replays(True)


def _link_dir(path: Path, target: Path) -> None:
    """A symlink to *target* (a junction on Windows, which needs no privilege)."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(path))
    else:
        path.symlink_to(target, target_is_directory=True)


def _swap_for_link(project: Path, rel: str) -> None:
    """Replace the governed directory *rel* with a link to an identical copy outside."""
    outside = project.parent / "outside" / rel
    outside.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(project / rel, outside, symlinks=True)
    shutil.rmtree(project / rel)
    _link_dir(project / rel, outside)


@pytest.mark.windows_compat
@pytest.mark.parametrize("rel", [".github", ".github/prompts"], ids=["root", "subdir"])
def test_a_governed_directory_swapped_for_a_link_misses_the_replay(
    project: Path, capsys, rel: str
) -> None:
    """The engine never deploys through a link (diff_apply._within), so a hit must not (#5438)."""
    _record()
    _swap_for_link(project, rel)
    capsys.readouterr()

    hit = verdict_record.replay(["install"])

    assert (hit, capsys.readouterr().out) == (False, "")


@pytest.mark.windows_compat
@pytest.mark.parametrize("rel", [".github", ".github/prompts"], ids=["root", "subdir"])
def test_a_governed_directory_swapped_for_a_link_misses_the_attested_hit(
    project: Path, rel: str
) -> None:
    _attest(project)
    assert _attested_hit() is True, "positive control: run 2 matches the attestation"

    _swap_for_link(project, rel)

    _assert_proves_itself()


@pytest.mark.windows_compat
def test_the_engine_fast_path_refuses_a_linked_target_root(project: Path) -> None:
    """Control: the owner the record must agree with declines the same state."""
    from apm_cli.install.diff_apply import Decline, _within

    _swap_for_link(project, ".github")

    with pytest.raises(Decline, match="symlink in a deployed path"):
        _within(project, _REL)


@pytest.mark.windows_compat
def test_a_linked_target_root_is_never_recorded(project: Path) -> None:
    _swap_for_link(project, ".github")

    _record()

    assert not (_home(project) / "verdicts").exists()


@pytest.mark.windows_compat
@pytest.mark.parametrize("dangling", [False, True], ids=["to-a-directory", "dangling"])
def test_a_nested_directory_link_present_before_recording_is_never_recorded(
    project: Path, dangling: bool
) -> None:
    """A link listed among a governed directory's files, with no deployed file
    beneath it, still names content the record cannot key (#5438)."""
    outside = project.parent / "shared"
    outside.mkdir()
    (outside / "notes.md").write_text("shared\n")
    _link_dir(project / ".github" / "shared", outside)
    if dangling:
        shutil.rmtree(outside)

    _record()

    assert not (_home(project) / "verdicts").exists()
