"""Offline process boundaries for the real Unix installer regression tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def _download(root: Path, args: list[str]) -> int:
    """Serve only declared fixture routes, including private-sidecar retries."""
    urls = [arg for arg in args if urlparse(arg).scheme == "https"]
    assert len(urls) == 1
    routes = json.loads((root / "routes.json").read_text(encoding="ascii"))
    source = routes.get(urls[0])
    if source is None:
        return 22
    if source == "unavailable":
        return 7
    if "/releases/assets/" in urlparse(urls[0]).path:
        assert "Authorization: token fixture-token" in args
        assert "Accept: application/octet-stream" in args
    if "/releases/tags/" in urlparse(urls[0]).path:
        assert "Authorization: token fixture-token" in args
    if urls[0].endswith(".sha256") and os.environ.get("FIXTURE_AUTH_REQUIRED"):
        if "-H" not in args:
            return 22
        assert args[args.index("-H") + 1] == "Authorization: token fixture-token"
    payload = (root / source).read_bytes()
    if "-o" in args:
        output = Path(args[args.index("-o") + 1])
        assert output.resolve().is_relative_to(root / "scratch")
        output.write_bytes(payload)
    else:
        sys.stdout.buffer.write(payload)
    return 0


def main(tool: str, args: list[str]) -> int:
    """Serve fixture bytes and deny every installation or external operation."""
    root = Path(os.environ["FIXTURE_ROOT"])
    with (root / "trace.jsonl").open("a", encoding="ascii") as stream:
        stream.write(json.dumps({"tool": tool, "args": args}) + "\n")
    if tool == "uname":
        print(os.environ["FIXTURE_OS"] if args == ["-s"] else "x86_64")
        return 0
    if tool == "ldd":
        print("ldd (GNU libc) 2.39")
        return 0
    if tool == "mktemp":
        assert args == ["-d"]
        print(root / "scratch")
        return 0
    if tool == "rm":
        assert args == ["-rf", str(root / "scratch")]
        return 0
    if tool == "curl":
        return _download(root, args)
    if tool in ("sha256sum", "shasum"):
        assert args[:-1] == (["-a", "256"] if tool == "shasum" else [])
        archive = Path(args[-1])
        assert archive.resolve().is_relative_to(root / "scratch")
        mode = os.environ.get("FIXTURE_HASH_MODE", "")
        if mode == "failed":
            return 97
        if mode == "malformed":
            print("not-a-digest")
            return 0
        # Use the native hashing tool when installed; the other platform's
        # tool is emulated with a real SHA-256 computation, never a canned hash.
        native = os.environ.get(f"FIXTURE_NATIVE_{tool}")
        if native:
            return subprocess.run([native, *args], check=False).returncode
        print(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive}")
        return 0
    if tool == "tar":
        assert args == [
            "-xzf",
            str(root / "scratch" / os.environ["FIXTURE_ASSET"]),
            "-C",
            str(root / "scratch"),
        ]
        return subprocess.run([os.environ["FIXTURE_TAR"], *args], check=False).returncode
    if tool == "chmod":
        assert Path(args[-1]).resolve().is_relative_to(root / "scratch")
        Path(args[-1]).chmod(0o755)
        return 0
    print(f"FIXTURE_BLOCKED={tool}", file=sys.stderr)
    return 95


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2:]))
