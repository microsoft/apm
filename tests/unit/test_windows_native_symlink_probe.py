"""Simulated probe controls; only the hosted Windows job is native evidence."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from xml.sax.saxutils import escape

import pytest

import apm_cli
from scripts import windows_native_symlink_probe_entry as probe
from tests.unit.cache.test_git_symlink_config import _git
from tests.unit.cache.test_git_symlink_config import config_env as config_env

pytestmark = pytest.mark.component

SID = "S-1-5-21-1-2-3-1001"
NATIVE_CASE = (
    '<testcase classname="tests.integration.test_windows_native_symlink" '
    'name="test_native_standard_user_symlink_fallback">{}</testcase>'
)


def _report(path: Path, cases: str) -> Path:
    path.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("cases", "mutation", "message"),
    [
        ("", False, "exactly once"),
        ('<testcase name="unrelated"/>', False, "exactly once"),
        (NATIVE_CASE.format("") * 2, False, "exactly once"),
        (NATIVE_CASE.format("<skipped/>"), False, "without skips"),
        (NATIVE_CASE.format("<error/>"), False, "without skips"),
        (NATIVE_CASE.format("<failure>failed</failure>"), False, "must pass"),
        (NATIVE_CASE.format(""), True, "fail exactly"),
        (NATIVE_CASE.format("<failure>setup failed</failure>"), True, "unrelated"),
        (
            NATIVE_CASE.format(f"<failure>{escape(probe.MUTATION_FAILURE)}</failure>")
            + '<testcase name="extra"/>',
            True,
            "fail exactly",
        ),
    ],
)
def test_junit_rejects_nonproof_results(
    tmp_path: Path, cases: str, mutation: bool, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        probe.check_junit(_report(tmp_path / "result.xml", cases), mutation=mutation)


@pytest.mark.parametrize("mutation", [False, True])
def test_junit_accepts_only_expected_native_outcome(tmp_path: Path, mutation: bool) -> None:
    content = f"<failure>{escape(probe.MUTATION_FAILURE)}</failure>" if mutation else ""
    assert (
        probe.check_junit(
            _report(tmp_path / "result.xml", NATIVE_CASE.format(content)), mutation=mutation
        )
        == 1
    )


@pytest.fixture
def simulated_token(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[list[str]]]:
    rows = {
        "/user": [["machine\\fixture", SID]],
        "/groups": [["Users", "Alias", "S-1-5-32-545", "Enabled"]],
        "/priv": [["SeChangeNotifyPrivilege", "Bypass traverse", "Enabled"]],
    }
    monkeypatch.setattr(probe, "_whoami_rows", rows.__getitem__)
    monkeypatch.setattr(
        probe,
        "ctypes",
        SimpleNamespace(
            windll=SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=lambda: False))
        ),
    )

    def denied(target: Path, link: Path) -> None:
        error = OSError("simulated privilege denial")
        error.winerror = 1314
        raise error

    monkeypatch.setattr(probe, "os", SimpleNamespace(name="nt", symlink=denied))
    return rows


def test_simulated_denial_exercises_real_scratch_io(
    tmp_path: Path, simulated_token: dict[str, list[list[str]]]
) -> None:
    context = probe.probe_native_context(tmp_path / "probe", SID)
    assert context["sid"] == SID
    assert context["ordinary_io"] is True
    assert context["symlink_winerror"] == 1314
    assert (tmp_path / "probe" / "ordinary.txt").read_bytes() == b"ordinary read/write succeeds"


@pytest.mark.parametrize(
    ("option", "rows", "message"),
    [
        ("/user", [["machine\\unexpected", "wrong"]], "does not match"),
        ("/groups", [], "incomplete"),
        ("/priv", [], "incomplete"),
        ("/groups", [["broken"]], "incomplete"),
        ("/groups", [["Administrators", "Alias", "S-1-5-32-544", "Deny only"]], "administrator"),
        (
            "/priv",
            [["SeCreateSymbolicLinkPrivilege", "Create links", "Disabled"]],
            "symlink privilege",
        ),
    ],
)
def test_token_rejects_unqualified_identity(
    tmp_path: Path,
    simulated_token: dict[str, list[list[str]]],
    option: str,
    rows: list[list[str]],
    message: str,
) -> None:
    simulated_token[option] = rows
    with pytest.raises(RuntimeError, match=message):
        probe.probe_native_context(tmp_path / "probe", SID)
    assert not (tmp_path / "probe").exists()


@pytest.mark.parametrize("winerror", [5, None])
def test_wrong_native_denial_is_not_accepted(
    tmp_path: Path,
    simulated_token: dict[str, list[list[str]]],
    monkeypatch: pytest.MonkeyPatch,
    winerror: int | None,
) -> None:
    error = OSError("simulated unrelated error")
    error.winerror = winerror
    monkeypatch.setattr(probe.os, "symlink", Mock(side_effect=error))
    with pytest.raises(RuntimeError, match="Expected privilege denial"):
        probe.probe_native_context(tmp_path / "probe", SID)


def test_successful_link_creation_is_not_denied_capability(
    tmp_path: Path,
    simulated_token: dict[str, list[list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe.os, "symlink", lambda target, link: link.write_bytes(b"simulated"))
    with pytest.raises(RuntimeError, match="Symlink creation succeeded"):
        probe.probe_native_context(tmp_path / "probe", SID)
    assert not (tmp_path / "probe" / "link.txt").exists()


@pytest.fixture
def simulated_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Mock]:
    root = tmp_path / "checkout"
    source = root / "src" / "apm_cli"
    source.mkdir(parents=True)
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.with_name("apm.exe").touch()
    monkeypatch.setattr(apm_cli, "__file__", str(source / "__init__.py"))
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(probe, "get_git_executable", lambda: "resolved-git")
    command = Mock(side_effect=["a" * 40 + "\n", ""])
    monkeypatch.setattr(subprocess, "check_output", command)
    monkeypatch.setenv("APM_BINARY_PATH", "prior-binary")
    return root, command


def test_provenance_binds_exact_checkout_and_interpreter(
    simulated_source: tuple[Path, Mock],
) -> None:
    root, command = simulated_source
    actual = probe.source_provenance(root, "a" * 40)
    assert actual["head"] == "a" * 40
    assert Path(actual["python"]).parent == root / ".venv" / "Scripts"
    assert os.environ["APM_BINARY_PATH"] == actual["apm_binary"]
    assert command.call_args_list[0].args[0] == [
        "resolved-git",
        "-c",
        f"safe.directory={root}",
        "-C",
        str(root),
        "rev-parse",
        "HEAD",
    ]
    assert command.call_args_list[1].args[0][-3:] == [
        "status",
        "--porcelain",
        "--untracked-files=no",
    ]


@pytest.mark.parametrize("failure", ["head", "dirty", "source", "python", "binary"])
def test_provenance_rejects_wrong_source(
    simulated_source: tuple[Path, Mock], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root, command = simulated_source
    if failure == "head":
        command.side_effect = ["b" * 40, ""]
    elif failure == "dirty":
        command.side_effect = ["a" * 40, " M src/apm_cli/utils/git_env.py\n"]
    elif failure == "source":
        monkeypatch.setattr(apm_cli, "__file__", str(root / "elsewhere" / "__init__.py"))
    elif failure == "python":
        monkeypatch.setattr(sys, "executable", str(root / "global" / "python.exe"))
    else:
        (root / ".venv" / "Scripts" / "apm.exe").unlink()
    with pytest.raises(RuntimeError):
        probe.source_provenance(root, "a" * 40)
    assert os.environ["APM_BINARY_PATH"] == "prior-binary"


@pytest.mark.parametrize("pin_checkout_lf", [False, True])
def test_provenance_survives_config_isolation_only_with_bound_checkout_eol(
    tmp_path: Path,
    config_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    pin_checkout_lf: bool,
) -> None:
    """Real Git reproduces CRLF dirtiness when the parent config is removed."""
    seed = tmp_path / "seed"
    _git(config_env, "init", "--quiet", "--template=", str(seed))
    source = seed / "src" / "apm_cli"
    source.mkdir(parents=True)
    (source / "__init__.py").write_bytes(b"fixture = True\n")
    _git(config_env, "-C", str(seed), "add", "src")
    _git(config_env, "-C", str(seed), "commit", "--quiet", "-m", "Fixture")
    system_config = Path(config_env["GIT_CONFIG_SYSTEM"])
    system_config.write_text("[core]\n    autocrlf = true\n", encoding="utf-8")
    clone_env = dict(config_env)
    if pin_checkout_lf:
        clone_env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="core.autocrlf",
            GIT_CONFIG_VALUE_0="false",
        )
    root = tmp_path / "checkout"
    _git(clone_env, "clone", "--quiet", "--template=", str(seed), str(root))
    head = _git(config_env, "-C", str(root), "rev-parse", "HEAD")
    checked_out = root / "src" / "apm_cli" / "__init__.py"
    assert checked_out.read_bytes() == (
        b"fixture = True\n" if pin_checkout_lf else b"fixture = True\r\n"
    )
    system_config.write_bytes(b"")
    stamp = checked_out.stat().st_mtime_ns + 2_000_000_000
    os.utime(checked_out, ns=(stamp, stamp))
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.with_name("apm.exe").touch()
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(apm_cli, "__file__", str(checked_out))
    monkeypatch.setenv("APM_BINARY_PATH", "prior")
    if pin_checkout_lf:
        assert probe.source_provenance(root, head)["head"] == head
    else:
        with pytest.raises(RuntimeError, match="tracked acceptance source is dirty"):
            probe.source_provenance(root, head)


@pytest.mark.parametrize("phase", ["baseline", "mutation", "restored"])
def test_phase_uses_pinned_python_and_inspects_actual_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    mutation = phase == "mutation"
    body = f"<failure>{escape(probe.MUTATION_FAILURE)}</failure>" if mutation else ""
    cases = NATIVE_CASE.format(body)
    if phase == "baseline":
        cases += '<testcase name="preservation-control"/>'
    _report(tmp_path / f"{phase}.xml", cases)
    command = Mock(return_value=subprocess.CompletedProcess([], int(mutation)))
    monkeypatch.setattr(subprocess, "run", command)
    monkeypatch.setenv("APM_WINDOWS_NATIVE_MUTATION", "stale")
    actual = probe.run_phase(tmp_path, tmp_path, phase)
    args = command.call_args.args[0]
    assert args[:4] == [sys.executable, "-B", "-m", "pytest"]
    assert args[-1] == probe.NATIVE_NODE
    assert ("tests/unit/cache/test_git_symlink_config.py" in args) is (phase == "baseline")
    assert command.call_args.kwargs["timeout"] == 180
    assert command.call_args.kwargs["env"].get("APM_WINDOWS_NATIVE_MUTATION") == (
        "drop-local-precedence" if mutation else None
    )
    assert actual["exit_code"] == int(mutation)
    assert actual["cases"] == (2 if phase == "baseline" else 1)


@pytest.mark.parametrize("exit_code", [1, 2, 5])
def test_phase_rejects_nonpassing_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    monkeypatch.setattr(
        subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], exit_code))
    )
    with pytest.raises(RuntimeError, match="expected pytest exit 0"):
        probe.run_phase(tmp_path, tmp_path, "baseline")
