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
    digest = "sha256:" + hashlib.sha256(_BODY).hexdigest()
    (root / "apm.lock.yaml").write_text(
        "lockfile_version: '1'\ndependencies:\n- repo_url: o/p\n"
        f"  deployed_file_hashes:\n    {_REL}: {digest}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("APM_CACHE_DIR", str(tmp_path / "cache"))
    for name in (
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
    assert sorted(read - classified) == []


def test_the_record_module_reads_the_environment_only_through_its_classified_helper() -> None:
    tree = ast.parse(Path(verdict_record.__file__).read_text(encoding="utf-8"))
    for func in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        if func.name == "_env":
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
