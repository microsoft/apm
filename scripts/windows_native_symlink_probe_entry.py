"""Run the native symlink regression under the disposable runner's standard user."""

from __future__ import annotations

import argparse
import csv
import ctypes
import io
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from apm_cli.utils.console import _rich_error, _rich_info
from apm_cli.utils.git_env import get_git_executable

NATIVE_NODE = (
    "tests/integration/test_windows_native_symlink.py::test_native_standard_user_symlink_fallback"
)
MUTATION_FAILURE = "native-symlink-precedence: APM clone failed after native Git fallback succeeded"


def _whoami_rows(option: str) -> list[list[str]]:
    """Read token information without printing the process environment."""
    executable = Path(os.environ["SYSTEMROOT"]) / "System32" / "whoami.exe"
    result = subprocess.run(  # noqa: S603 - fixed OS utility, no shell
        [str(executable), option, "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return list(csv.reader(io.StringIO(result.stdout)))


def probe_native_context(scratch: Path, expected_sid: str) -> dict[str, object]:
    """Require an actual writable, non-admin token without symlink privilege."""
    if os.name != "nt":
        raise RuntimeError("This acceptance probe requires native Windows")
    users = _whoami_rows("/user")
    if len(users) != 1 or len(users[0]) != 2 or users[0][1] != expected_sid:
        raise RuntimeError("The child token does not match the created standard-user SID")
    groups = _whoami_rows("/groups")
    privileges = _whoami_rows("/priv")
    if not groups or not privileges or any(len(row) < 3 for row in (*groups, *privileges)):
        raise RuntimeError("Windows token information is incomplete")
    group_sids = [row[2] for row in groups]
    privilege_names = [row[0] for row in privileges]
    if ctypes.windll.shell32.IsUserAnAdmin() or "S-1-5-32-544" in group_sids:
        raise RuntimeError("The acceptance process must not have an administrator token")
    if "SeCreateSymbolicLinkPrivilege" in privilege_names:
        raise RuntimeError("The acceptance token must lack symlink privilege, even if disabled")

    scratch.mkdir(parents=True, exist_ok=False)
    target = scratch / "ordinary.txt"
    target.write_bytes(b"ordinary read/write succeeds")
    if target.read_bytes() != b"ordinary read/write succeeds":
        raise RuntimeError("Ordinary scratch I/O failed")
    link = scratch / "link.txt"
    try:
        os.symlink(target, link)
    except OSError as error:
        if error.winerror != 1314:
            raise RuntimeError(
                f"Expected privilege denial 1314, received {error.winerror}"
            ) from error
    else:
        link.unlink()
        raise RuntimeError(
            "Symlink creation succeeded; this is not the required denied-capability case"
        )
    return {
        "sid": expected_sid,
        "username": users[0][0],
        "group_sids": group_sids,
        "privileges": privilege_names,
        "administrator": False,
        "ordinary_io": True,
        "symlink_winerror": 1314,
    }


def check_junit(report: Path, *, mutation: bool) -> int:
    """Reject skipped/empty/error results and require the real regression node."""
    # Only read the report from our own bounded pytest subprocess.
    cases = ET.parse(report).getroot().findall(".//testcase")  # noqa: S314
    native = [
        case
        for case in cases
        if case.get("name") == NATIVE_NODE.rsplit("::", 1)[1]
        and case.get("classname") == "tests.integration.test_windows_native_symlink"
    ]
    if len(native) != 1 or any(
        case.find(tag) is not None for case in cases for tag in ("skipped", "error")
    ):
        raise ValueError("The native regression must execute exactly once without skips or errors")
    failures = [case.find("failure") for case in cases if case.find("failure") is not None]
    if mutation:
        if len(cases) != 1 or len(failures) != 1:
            raise ValueError("The mutation must fail exactly the native regression")
        if MUTATION_FAILURE not in (failures[0].text or ""):
            raise ValueError("The mutation failed for an unrelated reason")
    elif failures:
        raise ValueError("The restored native regression and preservation controls must pass")
    return len(cases)


def source_provenance(root: Path, expected_head: str) -> dict[str, str]:
    """Bind imports and Git state to the explicitly checked-out PR source."""
    import apm_cli

    source = Path(apm_cli.__file__).resolve().parent
    if source != root / "src" / "apm_cli":
        raise RuntimeError(f"APM imported from a different checkout: {source}")
    python = Path(sys.executable)
    if python.parent != root / ".venv" / "Scripts":
        raise RuntimeError(
            "The acceptance process must use this checkout's provisioned interpreter"
        )
    binary = python.with_name("apm.exe")
    if not binary.is_file():
        raise RuntimeError("The provisioned APM executable is missing")
    command = [get_git_executable(), "-c", f"safe.directory={root}", "-C", str(root)]
    head = subprocess.check_output(  # noqa: S603 - resolved Git, fixed argv, no shell
        [*command, "rev-parse", "HEAD"], text=True, timeout=30
    ).strip()
    status = subprocess.check_output(  # noqa: S603 - resolved Git, fixed argv, no shell
        [*command, "status", "--porcelain", "--untracked-files=no"], text=True, timeout=30
    )
    if head != expected_head:
        raise RuntimeError(f"Expected source head {expected_head}, found {head}")
    if status:
        paths = status.splitlines()
        raise RuntimeError(
            f"The tracked acceptance source is dirty ({len(paths)} paths):\n"
            + "\n".join(paths[:20])
        )
    os.environ["APM_BINARY_PATH"] = str(binary)
    return {"head": head, "python": str(python), "apm_binary": str(binary), "source": str(source)}


def run_phase(root: Path, scratch: Path, name: str) -> dict[str, object]:
    """Capture a real pytest exit and inspect its machine-readable result."""
    mutation = name == "mutation"
    env = dict(os.environ)
    if mutation:
        env["APM_WINDOWS_NATIVE_MUTATION"] = "drop-local-precedence"
    else:
        env.pop("APM_WINDOWS_NATIVE_MUTATION", None)
    report = scratch / f"{name}.xml"
    nodes = [NATIVE_NODE]
    if name == "baseline":
        nodes.insert(0, "tests/unit/cache/test_git_symlink_config.py")
    command = [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
        "-o",
        "junit_family=xunit1",
        "--strict-markers",
        "--basetemp",
        str(scratch / f"{name}-cases"),
        "-vv",
        "--tb=short",
        "--show-capture=no",
        "--no-showlocals",
        f"--junitxml={report}",
        *nodes,
    ]
    result = subprocess.run(  # noqa: S603 - pinned interpreter and fixed test nodes, no shell
        command, cwd=root, env=env, check=False, timeout=180
    )
    expected_exit = 1 if mutation else 0
    if result.returncode != expected_exit:
        raise RuntimeError(f"{name}: expected pytest exit {expected_exit}, got {result.returncode}")
    count = check_junit(report, mutation=mutation)
    if name == "baseline" and count < 2:
        raise RuntimeError("The baseline did not execute the preservation controls")
    return {"phase": name, "command": command, "exit_code": result.returncode, "cases": count}


def main() -> int:
    """Qualify the token before enabling the native prerequisite marker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--expected-sid", required=True)
    parser.add_argument("--expected-head", required=True)
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True)
        scratch = args.scratch.resolve(strict=True)
        for name in ("home", "temp", "cache", "appdata", "localappdata", "config", "data"):
            (scratch / name).mkdir(exist_ok=True)
        for key in tuple(os.environ):
            if key.startswith("GIT_"):
                os.environ.pop(key)
        for scope in ("GLOBAL", "SYSTEM"):
            config = scratch / f"{scope.lower()}.gitconfig"
            config.write_bytes(b"")
            os.environ[f"GIT_CONFIG_{scope}"] = str(config)
        os.environ.update(
            HOME=str(scratch / "home"),
            USERPROFILE=str(scratch / "home"),
            TMP=str(scratch / "temp"),
            TEMP=str(scratch / "temp"),
            UV_CACHE_DIR=str(scratch / "cache"),
            APPDATA=str(scratch / "appdata"),
            LOCALAPPDATA=str(scratch / "localappdata"),
            XDG_CACHE_HOME=str(scratch / "cache"),
            XDG_CONFIG_HOME=str(scratch / "config"),
            XDG_DATA_HOME=str(scratch / "data"),
            COVERAGE_FILE=str(scratch / ".coverage"),
            HYPOTHESIS_STORAGE_DIRECTORY=str(scratch / "cache" / "hypothesis"),
            PYTEST_ADDOPTS="",
            PYTHONDONTWRITEBYTECODE="1",
        )
        tempfile.tempdir = str(scratch / "temp")
        provenance = source_provenance(root, args.expected_head)
        context = probe_native_context(scratch / "initial-capability", args.expected_sid)
        os.environ["APM_WINDOWS_NATIVE_EXPECTED_SID"] = args.expected_sid
        os.environ["APM_WINDOWS_NATIVE_STANDARD_USER"] = "1"
        _rich_info("Native standard-user token confirmed; running fallback and mutation controls")
        phases = [run_phase(root, scratch, phase) for phase in ("baseline", "mutation", "restored")]
        if source_provenance(root, args.expected_head) != provenance:
            raise RuntimeError("Source/interpreter provenance changed during acceptance")
        proof = {"source": provenance, "context": context, "phases": phases, "status": "passed"}
        (scratch / "native-proof.json").write_text(
            json.dumps(proof, indent=2, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n"
        )
        _rich_info(
            "Native fallback passed; the old-precedence mutation failed and restoration passed"
        )
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, ET.ParseError) as error:
        _rich_error(f"Native Windows proof failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
