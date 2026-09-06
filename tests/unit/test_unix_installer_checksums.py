"""Exercise install.sh itself with harmless archives and isolated process I/O."""

from __future__ import annotations

import hashlib
import io
import json
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(sys.platform == "win32", reason="Unix bootstrap uses POSIX shell tools"),
]

ROOT = Path(__file__).resolve().parents[2]
DENIED_TOOLS = ("sudo", "pip", "pip3", "python", "python3", "mkdir", "cp", "mv", "ln")


def _run_installer(
    tmp_path: Path,
    *,
    platform: str = "darwin",
    selection: str = "pinned",
    mirror: bool = False,
    checksum: str = "matching",
    hash_tools: tuple[str, ...] = ("sha256sum", "shasum"),
    hash_mode: str = "",
    auth_required: bool = False,
    github_host: str = "github.com",
    shell: str = "bash",
) -> tuple[subprocess.CompletedProcess[str], list[dict], Path]:
    """Run the worktree installer; only fixture-local marker execution is allowed."""
    for directory in ("bin", "home", "scratch"):
        (tmp_path / directory).mkdir()
    asset = f"apm-{platform}-x86_64.tar.gz"
    payload = b'#!/bin/sh\nprintf "executed:%s\\n" "$*" >> "$FIXTURE_MARKER"\n'
    with tarfile.open(tmp_path / asset, "w:gz") as archive:
        member = tarfile.TarInfo(f"apm-{platform}-x86_64/apm")
        member.size = len(payload)
        member.mode = 0o755
        archive.addfile(member, io.BytesIO(payload))
    digest = hashlib.sha256((tmp_path / asset).read_bytes()).hexdigest()
    records = {
        "matching": f"{digest}  {asset}\n",
        "binary-mode": f"{digest} *{asset}\n",
        "uppercase": f"{digest.upper()}  {asset}\n",
        "crlf": f"{digest}  {asset}\r\n",
        "no-newline": f"{digest}  {asset}",
        "mismatch": f"{'0' * 64}  {asset}\n",
        "empty": "",
        "html": "<html>not found</html>\n",
        "short": f"{digest[:-1]}  {asset}\n",
        "nonhex": f"{'z' * 64}  {asset}\n",
        "bare": f"{digest}\n",
        "wrong-asset": f"{digest}  apm-other.tar.gz\n",
        "path": f"{digest}  ../{asset}\n",
        "extra-field": f"{digest}  {asset} ignored\n",
        "duplicate": f"{digest}  {asset}\n{digest}  {asset}\n",
        "missing": "",
        "unavailable": "",
    }
    (tmp_path / "checksum").write_text(records[checksum], encoding="ascii")
    (tmp_path / "latest.json").write_text('{"tag_name": "v0.29.0"}\n', encoding="ascii")
    base = (
        "https://mirror.invalid/apm"
        if mirror
        else f"https://{github_host}/microsoft/apm/releases/download"
    )
    metadata = (
        "https://mirror.invalid/apm/latest.json"
        if mirror
        else "https://api.github.com/repos/microsoft/apm/releases/latest"
    )
    url = f"{base}/v0.29.0/{asset}"
    routes = {url: asset, metadata: "latest.json"}
    if checksum != "missing":
        routes[url + ".sha256"] = "unavailable" if checksum == "unavailable" else "checksum"
    (tmp_path / "routes.json").write_text(json.dumps(routes), encoding="ascii")
    tools = ("uname", "ldd", "mktemp", "rm", "curl", "tar", "chmod", *hash_tools, *DENIED_TOOLS)
    stub = ROOT / "tests/utils/unix_installer_stub.py"
    for tool in tools:
        wrapper = tmp_path / "bin" / tool
        wrapper.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(stub))} {tool} "$@"\n',
            encoding="ascii",
        )
        wrapper.chmod(0o755)
    # GNU tar spawns gzip; BSD tar decompresses internally.
    for tool in ("grep", "sed", "awk", "tr", "sort", "head", "dirname", "readlink", "gzip"):
        native = shutil.which(tool)
        assert native is not None, tool
        (tmp_path / "bin" / tool).symlink_to(native)
    marker = tmp_path / "executed.marker"
    env = {
        "PATH": str(tmp_path / "bin"),
        "HOME": str(tmp_path / "home"),
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "FIXTURE_ROOT": str(tmp_path),
        "FIXTURE_OS": "Darwin" if platform == "darwin" else "Linux",
        "FIXTURE_ASSET": asset,
        "FIXTURE_MARKER": str(marker),
        "FIXTURE_HASH_MODE": hash_mode,
        "FIXTURE_TAR": shutil.which("tar") or "",
        "APM_INSTALL_DIR": str(tmp_path / "install/bin"),
        "APM_LIB_DIR": str(tmp_path / "install/lib/apm"),
        "GITHUB_URL": f"https://{github_host}",
        "GITHUB_APM_PAT": "fixture-token",
    }
    for tool in hash_tools:
        native = shutil.which(tool)
        if native:
            env[f"FIXTURE_NATIVE_{tool}"] = native
    if selection == "pinned":
        env["VERSION"] = "v0.29.0"
    if mirror:
        env.update(
            APM_RELEASE_BASE_URL=base,
            APM_RELEASE_METADATA_URL=metadata,
            APM_NO_DIRECT_FALLBACK="1",
        )
    if auth_required:
        env["FIXTURE_AUTH_REQUIRED"] = "1"
    command = [shutil.which(shell) or shell, str(ROOT / "install.sh")]
    if selection == "argument":
        command.append("@v0.29.0")
    elif selection == "self-update":
        import apm_cli.commands.self_update as update_module

        # Use the production self-update command/env bridge, with only persisted
        # preferences and ambient environment replaced to avoid user config I/O.
        with (
            patch.object(update_module, "external_process_env", return_value=env),
            patch("apm_cli.config.get_self_update_install_dir", return_value=None),
            patch("apm_cli.config.get_self_update_channel", return_value="stable"),
        ):
            release = update_module._resolve_self_update_release("0.29.0")
            env = update_module._build_self_update_installer_env(release)
            command = update_module._get_installer_run_command(str(ROOT / "install.sh"))
    result = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30
    )
    trace = [
        json.loads(line)
        for line in (tmp_path / "trace.jsonl").read_text(encoding="ascii").splitlines()
    ]
    assert not (tmp_path / "install").exists()
    assert "fixture-token" not in result.stdout + result.stderr
    if mirror:
        requests = [event for event in trace if event["tool"] == "curl"]
        assert all("-H" not in event["args"] for event in requests)
        hosts = {
            urlparse(arg).hostname
            for event in requests
            for arg in event["args"]
            if urlparse(arg).scheme == "https"
        }
        assert hosts == {"mirror.invalid"}
    return result, trace, marker


def _assert_refused(
    result: subprocess.CompletedProcess[str], trace: list[dict], marker: Path
) -> None:
    """Every integrity failure precedes extraction, execution and pip fallback."""
    assert result.returncode == 1, result.stdout + result.stderr
    assert not marker.exists()
    assert not {event["tool"] for event in trace} & {"tar", "chmod", *DENIED_TOOLS}
    assert "Attempting automatic fallback" not in result.stdout
    assert "checksum" in result.stdout.lower() or "sha-256" in result.stdout.lower()


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("selection", ["pinned", "latest", "self-update"])
@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("checksum", ["matching", "mismatch"])
def test_verification_precedes_archive_consumption(
    tmp_path: Path, platform: str, selection: str, mirror: bool, checksum: str
) -> None:
    """The full platform/source/version matrix must execute only matching bytes."""
    result, trace, marker = _run_installer(
        tmp_path, platform=platform, selection=selection, mirror=mirror, checksum=checksum
    )
    if checksum == "mismatch":
        _assert_refused(result, trace, marker)
    else:
        assert result.returncode == 95, result.stdout + result.stderr
        assert marker.read_text(encoding="ascii") == "executed:--version\n"
        tools = [event["tool"] for event in trace]
        assert tools.index("sha256sum") < tools.index("tar") < tools.index("chmod")
    requests = [event for event in trace if event["tool"] == "curl"]
    parsed = [urlparse(event["args"][-3]) for event in requests if "-o" in event["args"]]
    assert [url.path for url in parsed] == [
        f"{'/apm' if mirror else '/microsoft/apm/releases/download'}/v0.29.0/"
        f"apm-{platform}-x86_64.tar.gz{suffix}"
        for suffix in ("", ".sha256")
    ]
    if mirror:
        assert {url.hostname for url in parsed} == {"mirror.invalid"}
        assert all("-H" not in event["args"] for event in requests)


@pytest.mark.parametrize(
    "checksum",
    [
        "empty",
        "html",
        "short",
        "nonhex",
        "bare",
        "wrong-asset",
        "path",
        "extra-field",
        "duplicate",
        "missing",
        "unavailable",
    ],
)
@pytest.mark.parametrize("mirror", [False, True])
def test_invalid_sidecar_fails_closed(tmp_path: Path, checksum: str, mirror: bool) -> None:
    """Malformed, unbound, duplicate and unavailable sidecars are not verification."""
    _assert_refused(*_run_installer(tmp_path, checksum=checksum, mirror=mirror))


@pytest.mark.parametrize("hash_tools", [("shasum",), ("sha256sum",)])
@pytest.mark.parametrize("checksum", ["matching", "binary-mode", "uppercase", "crlf", "no-newline"])
@pytest.mark.parametrize("shell", ["bash", "sh"])
def test_supported_hash_tools_and_sidecar_formats(
    tmp_path: Path, hash_tools: tuple[str, ...], checksum: str, shell: str
) -> None:
    """Both supported shell entrypoints accept SHA tools and standard sidecars."""
    result, trace, marker = _run_installer(
        tmp_path, hash_tools=hash_tools, checksum=checksum, shell=shell, selection="argument"
    )
    assert result.returncode == 95, result.stdout + result.stderr
    assert marker.read_text(encoding="ascii") == "executed:--version\n"
    assert [e["tool"] for e in trace if e["tool"] in ("shasum", "sha256sum")] == list(hash_tools)


@pytest.mark.parametrize(
    ("hash_mode", "hash_tools"),
    [
        ("failed", ("shasum",)),
        ("malformed", ("shasum",)),
        ("failed", ("sha256sum",)),
        ("malformed", ("sha256sum",)),
        ("failed", ("sha256sum", "shasum")),
        ("malformed", ("sha256sum", "shasum")),
        ("missing", ()),
    ],
)
def test_missing_or_failed_hash_capability_is_fatal(
    tmp_path: Path, hash_mode: str, hash_tools: tuple[str, ...]
) -> None:
    """A present but failing hash tool is no reason to try another install source."""
    result, trace, marker = _run_installer(tmp_path, hash_mode=hash_mode, hash_tools=hash_tools)
    _assert_refused(result, trace, marker)
    assert [e["tool"] for e in trace if e["tool"] in ("shasum", "sha256sum")] == list(
        hash_tools[:1]
    )


@pytest.mark.parametrize("github_host", ["github.com", "ghe.invalid"])
def test_checksum_auth_retry_stays_on_configured_github_host(
    tmp_path: Path, github_host: str
) -> None:
    """Private sidecars use the already-resolved token only on the canonical host."""
    result, trace, marker = _run_installer(tmp_path, auth_required=True, github_host=github_host)
    assert result.returncode == 95, result.stdout + result.stderr
    assert marker.exists()
    requests = [e for e in trace if e["tool"] == "curl" and "-H" in e["args"]]
    assert len(requests) == 1
    assert urlparse(requests[0]["args"][-3]).hostname == github_host
    assert urlparse(requests[0]["args"][-3]).path.endswith(".tar.gz.sha256")
