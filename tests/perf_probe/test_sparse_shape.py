"""Research probe (CI only): fetch shape of a monorepo SUBDIR dep on the git path.

TODAY = what git_cache does for a subdir dep: a partial bare (``clone --bare
--no-tags --filter=blob:none``: every branch's commits and trees), a shared
no-checkout clone, ``sparse-checkout set --cone <dir>``, then ``checkout <sha>``,
which lazily fetches the cone's blobs from the promisor remote.
CEILING = only the pinned commit's trees (``fetch --depth=1 --filter=blob:none
<sha>``), then exactly the cone's blobs in ONE request (``fetch --no-tags
--filter=blob:none <blob ids>`` over stdin), as an in-process want list would.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import time
from pathlib import Path

import pytest

SHA_AC = "f10fdb8b2660ce85c98f220b2f1ca24653768a5c"
DEPS = [
    ("https://github.com/github/awesome-copilot", SHA_AC, "skills/review-and-refactor"),
    ("https://github.com/github/awesome-copilot", SHA_AC, "skills/documentation-writer"),
    ("https://github.com/github/awesome-copilot", SHA_AC, "agents"),
    ("https://github.com/microsoft/apm", None, "packages/apm-guide"),
]


def _children_cpu() -> float:
    if os.name == "nt":
        return 0.0
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def _git(cwd, *args, stdin=None, env=None):
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        check=True,
        env=env,
    )


def _size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _count(git_dir: Path) -> dict:
    out = _git(None, "--git-dir", str(git_dir), "count-objects", "-v").stdout.decode()
    return dict(line.split(": ") for line in out.splitlines())


def _phase(label, fn, rows):
    cpu, wall = _children_cpu(), time.monotonic()
    extra = fn() or {}
    rows[label] = {
        "wall_s": round(time.monotonic() - wall, 3),
        "child_cpu_s": round(_children_cpu() - cpu, 3),
        **extra,
    }


@pytest.mark.parametrize("url,sha,cone", DEPS)
def test_shape(tmp_path: Path, url: str, sha: str | None, cone: str) -> None:
    rows: dict = {}
    if sha is None:
        refs = dict(
            line.split("\t")[::-1]
            for line in _git(None, "ls-remote", url, "refs/tags/v0.37.0*")
            .stdout.decode()
            .splitlines()
        )
        sha = refs.get("refs/tags/v0.37.0^{}") or refs["refs/tags/v0.37.0"]
    bare = tmp_path / "bare.git"
    trace = tmp_path / "trace.json"

    def today_bare():
        _git(None, "clone", "-q", "--bare", "--no-tags", "--filter=blob:none", url, str(bare))
        return {"bytes": _size(bare / "objects"), "objects": _count(bare)}

    _phase("today.bare_partial_full_history", today_bare, rows)
    work = tmp_path / "work"

    def today_checkout():
        _git(None, "clone", "-q", "--local", "--shared", "--no-checkout", str(bare), str(work))
        _git(work, "config", "remote.origin.url", url)
        _git(work, "config", "remote.origin.promisor", "true")
        _git(work, "config", "remote.origin.partialclonefilter", "blob:none")
        _git(work, "sparse-checkout", "set", "--cone", cone)
        env = {**os.environ, "GIT_TRACE2_EVENT": str(trace)}
        before = _size(work / ".git" / "objects")
        _git(work, "checkout", "-q", sha, env=env)
        fetches = sum(
            1
            for line in trace.read_text(encoding="utf-8", errors="replace").splitlines()
            if '"event":"child_start"' in line and '"fetch"' in line
        )
        files = sum(1 for p in (work / cone).rglob("*") if p.is_file())
        return {
            "lazy_fetch_children": fetches,
            "lazy_bytes": _size(work / ".git" / "objects") - before,
            "cone_files": files,
        }

    _phase("today.sparse_checkout_lazy", today_checkout, rows)
    pin = tmp_path / "pin.git"

    def ceiling_trees():
        _git(None, "init", "-q", "--bare", str(pin))
        _git(pin, "fetch", "-q", "--depth=1", "--no-tags", "--filter=blob:none", url, sha)
        return {"bytes": _size(pin / "objects"), "objects": _count(pin)}

    _phase("ceiling.pin_trees_depth1", ceiling_trees, rows)

    def ceiling_blobs():
        listing = _git(pin, "ls-tree", "-r", sha, "--", cone).stdout.decode().splitlines()
        blobs = [line.split()[2] for line in listing if line.split()[1] == "blob"]
        before = _size(pin / "objects")
        _git(
            pin,
            "-c",
            "fetch.negotiationAlgorithm=noop",
            "fetch",
            "-q",
            "--no-tags",
            "--no-write-fetch-head",
            url,
            *blobs,
        )
        return {"cone_blobs": len(blobs), "bytes": _size(pin / "objects") - before, "requests": 1}

    _phase("ceiling.cone_blobs_one_request", ceiling_blobs, rows)
    print("SPARSE-PROBE " + json.dumps({"url": url, "cone": cone, "sha": sha[:12], **rows}))
