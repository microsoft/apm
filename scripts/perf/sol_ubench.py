"""Unit-cost microbench for the cold-install speed-of-light model (research only).

Runs on hosted CI runners, never on the shared host. Every number is process CPU
(time.process_time of this process; children via rusage or GetProcessTimes),
median of REPEATS, reported per operation in microseconds.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import ssl
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPEATS = 5
PACKAGES, FILES = 50, 28  # tree-50 floor: ~28 written files per package
WIN = os.name == "nt"
cpu = time.process_time
RESULTS: dict[str, dict] = {}


def record(name: str, samples: list[float], ops: int, note: str = "") -> None:
    per = [s / ops * 1e6 for s in samples]
    RESULTS[name] = {
        "us_per_op_p50": round(statistics.median(per), 2),
        "us_min": round(min(per), 2),
        "us_max": round(max(per), 2),
        "ops": ops,
        "note": note,
    }
    print(f"{name:48s} {statistics.median(per):10.2f} us/op  (n={ops}) {note}", flush=True)


def tree(root: Path) -> list[Path]:
    dirs = []
    for p in range(PACKAGES):
        d = root / "a" / "b" / ".github" / "instructions" / f"pkg-{p:03d}"
        dirs.append(d)
    return dirs


DATA = os.urandom(1024)


def fresh(base: Path, tag: str) -> Path:
    root = base / f"{tag}-{time.perf_counter_ns()}"
    for d in tree(root):
        d.mkdir(parents=True)
    return root


def files_bench(base: Path) -> None:
    n = PACKAGES * FILES

    def run(tag, body):
        out = []
        for _ in range(REPEATS):
            root = fresh(base, tag)
            dirs = tree(root)
            t = cpu()
            body(root, dirs)
            out.append(cpu() - t)
            shutil.rmtree(root, ignore_errors=True)
        return out

    def plain(_root, dirs):
        for d in dirs:
            for i in range(FILES):
                (d / f"f{i}.md").write_bytes(DATA)

    record("write: Path.write_bytes", run("plain", plain), n)

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)

    def excl(_root, dirs):
        for d in dirs:
            if not WIN:
                dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
                for i in range(FILES):
                    fd = os.open(f"f{i}.md", flags, 0o644, dir_fd=dfd)
                    os.write(fd, DATA)
                    os.close(fd)
                os.close(dfd)
            else:
                ds = str(d)
                for i in range(FILES):
                    fd = os.open(os.path.join(ds, f"f{i}.md"), flags, 0o644)
                    os.write(fd, DATA)
                    os.close(fd)

    record("write: os.open O_EXCL|O_NOFOLLOW (dir fd on posix)", run("excl", excl), n)

    def atomic(_root, dirs):
        for d in dirs:
            for i in range(FILES):
                fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp")
                os.write(fd, DATA)
                os.close(fd)
                os.replace(tmp, d / f"f{i}.md")

    record("write: mkstemp + write + os.replace (atomic)", run("atomic", atomic), n)

    def contained(root, dirs):
        r = os.path.realpath(root)
        for d in dirs:
            for i in range(FILES):
                dest = d / f"f{i}.md"
                real = os.path.realpath(dest)
                if os.path.commonpath([r, real]) != r:
                    raise SystemExit("escape")
                fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp")
                os.write(fd, DATA)
                os.close(fd)
                os.replace(tmp, dest)

    record("write: realpath containment + atomic", run("cont", contained), n)

    # Store -> project materialization.
    store = fresh(base, "store")
    for d in tree(store):
        for i in range(FILES):
            (d / f"f{i}.md").write_bytes(DATA)
    sdirs = tree(store)

    def link(_root, dirs):
        for s, d in zip(sdirs, dirs, strict=True):
            for i in range(FILES):
                os.link(s / f"f{i}.md", d / f"f{i}.md")

    record("materialize: os.link from store", run("link", link), n)

    def copy(_root, dirs):
        for s, d in zip(sdirs, dirs, strict=True):
            for i in range(FILES):
                shutil.copyfile(s / f"f{i}.md", d / f"f{i}.md")

    record("materialize: shutil.copyfile", run("copy", copy), n)

    def copy2(_root, dirs):
        for s, d in zip(sdirs, dirs, strict=True):
            for i in range(FILES):
                shutil.copy2(s / f"f{i}.md", d / f"f{i}.md")

    record("materialize: shutil.copy2", run("copy2", copy2), n)

    # Per-call path and read primitives on existing files (depth ~8 below base).
    target = sdirs[0] / "f0.md"
    k = 5000
    for name, fn in (
        ("path: os.path.realpath (existing, depth ~8)", lambda: os.path.realpath(target)),
        ("path: Path.resolve", lambda: target.resolve()),
        ("path: os.lstat", lambda: os.lstat(target)),
        ("path: Path.stat", lambda: target.stat()),
        ("read: Path.read_bytes 1 KB", lambda: target.read_bytes()),
        ("hash: sha256(1 KB) object", lambda: hashlib.sha256(DATA).hexdigest()),
        (
            "hash: read_bytes + sha256 per file",
            lambda: hashlib.sha256(target.read_bytes()).digest(),
        ),
        ("scan: os.scandir of a 28-file dir", lambda: list(os.scandir(sdirs[0]))),
    ):
        out = []
        for _ in range(REPEATS):
            t = cpu()
            for _ in range(k):
                fn()
            out.append(cpu() - t)
        record(name, out, k)
    shutil.rmtree(store, ignore_errors=True)

    big = os.urandom(16 * 1024 * 1024)
    out = []
    for _ in range(REPEATS):
        t = cpu()
        hashlib.sha256(big).digest()
        out.append(cpu() - t)
    record("hash: sha256 per MB (16 MB buffer)", [s / 16 for s in out], 1, "us per MB")


# -- child CPU of git spawns ---------------------------------------------------

if WIN:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _LPFT = ctypes.POINTER(wintypes.FILETIME)
    _k32.GetProcessTimes.argtypes = (wintypes.HANDLE, _LPFT, _LPFT, _LPFT, _LPFT)

    def child_cpu(popen) -> float:
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        _k32.GetProcessTimes(int(popen._handle), c, e, k, u)

        def ft(x):
            return ((x.dwHighDateTime << 32) | x.dwLowDateTime) / 1e7

        return ft(k) + ft(u)
else:
    import resource


def spawn_bench() -> None:
    git = shutil.which("git")
    if not git:
        return
    for name, argv in (
        ("spawn: git --version", [git, "--version"]),
        (
            "spawn: git config --list --show-scope -z",
            [git, "config", "--list", "--show-scope", "-z"],
        ),
    ):
        k = 40
        parent, child = [], []
        for _ in range(REPEATS):
            t = cpu()
            if not WIN:
                r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
            total = 0.0
            for _ in range(k):
                p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # noqa: S603
                p.communicate()
                if WIN:
                    total += child_cpu(p)
            parent.append(cpu() - t)
            if not WIN:
                r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
                total = (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)
            child.append(total)
        record(name + " (parent)", parent, k)
        record(name + " (child)", child, k)


# -- TLS / HTTP ----------------------------------------------------------------

SERVER = r"""
import http.server, ssl, sys
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_GET(self):
        body = b"x" * 12000
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(sys.argv[1], sys.argv[2])
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
print(srv.server_address[1], flush=True)
srv.serve_forever()
"""


def tls_bench(base: Path) -> None:
    cert, key = base / "cert.pem", base / "key.pem"
    openssl = shutil.which("openssl")
    if openssl:
        subprocess.run(  # noqa: S603
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=127.0.0.1",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
        )
        server = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", SERVER, str(cert), str(key)], stdout=subprocess.PIPE, text=True
        )
        port = int(server.stdout.readline())
        try:
            out = []
            for _ in range(REPEATS):
                t = cpu()
                ssl.create_default_context(cafile=str(cert))
                out.append(cpu() - t)
            record("tls: create_default_context(cafile)", out, 1)
            ctx = ssl.create_default_context(cafile=str(cert))
            k = 50
            out = []
            for _ in range(REPEATS):
                t = cpu()
                for _ in range(k):
                    c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx)
                    c.request("GET", "/x")
                    c.getresponse().read()
                    c.close()
                out.append(cpu() - t)
            record("tls local: new connection + GET 12 KB (client CPU)", out, k)
            out = []
            for _ in range(REPEATS):
                c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx)
                c.request("GET", "/warm")
                c.getresponse().read()
                t = cpu()
                for _ in range(k):
                    c.request("GET", "/x")
                    c.getresponse().read()
                out.append(cpu() - t)
                c.close()
            record("tls local: keep-alive GET 12 KB (client CPU)", out, k)
        finally:
            server.kill()
    out = []
    for _ in range(REPEATS):
        t = cpu()
        ssl.create_default_context()
        out.append(cpu() - t)
    record("tls: create_default_context() system CAs", out, 1)
    ctx = ssl.create_default_context()
    k = 8
    try:
        out = []
        for _ in range(3):
            t = cpu()
            for _ in range(k):
                c = http.client.HTTPSConnection("github.com", 443, context=ctx, timeout=20)
                c.request(
                    "GET",
                    "/microsoft/apm.git/info/refs?service=git-upload-pack",
                    headers={"User-Agent": "git/2.47 apm-sol-ubench"},
                )
                c.getresponse().read()
                c.close()
            out.append(cpu() - t)
        record("tls github.com: new connection + info/refs (client CPU)", out, k)
        out = []
        for _ in range(3):
            c = http.client.HTTPSConnection("github.com", 443, context=ctx, timeout=20)
            c.request(
                "GET",
                "/microsoft/apm.git/info/refs?service=git-upload-pack",
                headers={"User-Agent": "git/2.47"},
            )
            c.getresponse().read()
            t = cpu()
            for _ in range(k):
                c.request(
                    "GET",
                    "/microsoft/apm.git/info/refs?service=git-upload-pack",
                    headers={"User-Agent": "git/2.47"},
                )
                c.getresponse().read()
            out.append(cpu() - t)
            c.close()
        record("tls github.com: keep-alive info/refs (client CPU)", out, k)
    except OSError as exc:
        print(f"[!] github.com TLS bench skipped: {exc}", flush=True)


def boot_bench() -> None:
    out_parent = []
    k = 10
    for _ in range(REPEATS):
        if not WIN:
            r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        total = 0.0
        for _ in range(k):
            p = subprocess.Popen([sys.executable, "-c", "pass"])
            p.wait()
            if WIN:
                total += child_cpu(p)
        if not WIN:
            r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
            total = (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)
        out_parent.append(total)
    record("boot: python -c pass (child CPU)", out_parent, k)


def main() -> int:
    base = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "sol-ubench"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    import platform

    print(
        f"[i] {platform.platform()} python {sys.version.split()[0]} cpus {os.cpu_count()}",
        flush=True,
    )
    files_bench(base)
    spawn_bench()
    boot_bench()
    tls_bench(base)
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "ubench.json")
    out.write_text(
        json.dumps({"platform": platform.platform(), "results": RESULTS}, indent=1),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
