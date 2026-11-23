"""Verified-state record (#4977): a no-op replays only what a verified run recorded.

Component cells over a real filesystem: the recorder runs only after the engine
proved a no-op, a hit replays the exact output and writes nothing, and every
verdict input it summarizes (manifest, lock, deployed bytes, governed listings,
APM_HOME state, policy cache, allowlisted environment) misses when it changes.
"""

from __future__ import annotations

import ast
import hashlib
import os
import sys
from pathlib import Path

import pytest

from apm_cli.install import verdict_record

pytestmark = [pytest.mark.component, pytest.mark.promise("P-INV-I2")]

_REL = ".github/prompts/a.prompt.md"
_BODY = b"# prompt\n"
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
    (root / "apm.yml").write_text("name: demo\nversion: 1.0.0\n", encoding="utf-8")
    from apm_cli.utils.content_hash import compute_package_hash

    digest = "sha256:" + hashlib.sha256(_BODY).hexdigest()
    package_hash = compute_package_hash(root / "apm_modules" / "o" / "p", framed=True)
    (root / "apm.lock.yaml").write_text(
        "lockfile_version: '1'\ndependencies:\n- repo_url: o/p\n"
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
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(root)
    return root


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


def test_an_unrelated_environment_variable_still_hits(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _record()
    monkeypatch.setenv("APM_PERF_SPAWN_LOG", "/tmp/spawns.jsonl")
    monkeypatch.setenv("SOME_UNRELATED_TOOL_FLAG", "1")
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is True
    assert "up to date in " in capsys.readouterr().out


@pytest.mark.parametrize(
    "name", ["CI", "GITHUB_ACTIONS", "PROXY_REGISTRY_URL", "APM_POLICY_DISABLE"]
)
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
    classified |= {"APM_HOME", "PROXY_REGISTRY_ALLOW_HTTP"}
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
    """Registry MCP, MCP updates, LSP...: a safe miss, never a guessed no-op text."""
    with verdict_record.recording(["install"]):
        sys.stdout.write(_OUTPUT)
        verdict_record.note_full_success()
        verdict_record.unknown("lsp")
    capsys.readouterr()

    assert verdict_record.replay(["install"]) is False
    assert capsys.readouterr().out == ""
    assert not (_home(project) / "verdicts").exists()


def test_change_only_lines_are_shown_and_never_in_the_no_op_view(project: Path, capsys) -> None:
    """An owner's change-only line prints; its no-op form records without printing."""
    with verdict_record.recording(["install"]):
        sys.stdout.write("[>] Installing dependencies from apm.yml...\n")
        with verdict_record.change_only():
            sys.stdout.write("[*] Configured 1 server\n")
        with verdict_record.noop_only():
            sys.stdout.write("[*] All servers up to date\n")
        sys.stdout.write("[i] No changes -- install state already up to date in 0.4s.\n")
        verdict_record.note_verified_noop()
    shown = capsys.readouterr().out

    assert "Configured 1 server" in shown and "All servers up to date" not in shown
    assert verdict_record.replay(["install"]) is True
    replayed = capsys.readouterr().out
    assert "All servers up to date" in replayed and "Configured 1 server" not in replayed


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


def test_a_git_config_with_an_include_is_never_recorded(project: Path) -> None:
    (project.parent / "user" / ".gitconfig").write_text("[include]\n\tpath = other\n")
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
