"""Shared fixtures: isolated git sandboxes and an injectable fake policy.

Tests never run the real CI suites: the runner is driven with a fake policy
whose checks are trivial shell commands. Sandboxes live under tests/.work/
(gitignored) and are removed after each test.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parents[1] / ".apm" / "hooks" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import preflight_lib as lib  # noqa: E402
import preflight_policy as policy  # noqa: E402

WORK = Path(__file__).resolve().parent / ".work"


def run_git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
    for key in list(os.environ):
        if key.startswith("GIT_") or key == lib.BYPASS_ENV:
            monkeypatch.delenv(key, raising=False)
    gitconfig = workdir / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = Preflight Test\n\temail = preflight@example.com\n"
        "[init]\n\tdefaultBranch = main\n"
        "[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n",
        encoding="ascii",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(scope="session", autouse=True)
def _cleanup_work_root() -> None:
    yield
    shutil.rmtree(WORK, ignore_errors=True)


@pytest.fixture
def workdir(request: pytest.FixtureRequest) -> Path:
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", request.node.name)[:60]
    path = WORK / f"{name}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True)
    yield path.resolve()
    shutil.rmtree(path, ignore_errors=True)


@dataclass
class Sandbox:
    """A work clone with an `origin` bare remote and a feature branch."""

    root: Path
    remote: Path

    def git(self, *args: str) -> str:
        return run_git(self.root, *args)

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def commit(self, message: str = "change") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    @property
    def git_dir(self) -> Path:
        return Path(self.git("rev-parse", "--path-format=absolute", "--git-dir"))

    def receipt(self) -> dict:
        return json.loads(lib.receipt_path(self.git_dir).read_text(encoding="utf-8"))


def make_sandbox(base: Path) -> Sandbox:
    remote = base / "remote.git"
    root = base / "work"
    run_git(base, "init", "-q", "--bare", "-b", "main", str(remote))
    run_git(base, "init", "-q", "-b", "main", str(root))
    box = Sandbox(root=root, remote=remote)
    box.git("remote", "add", "origin", str(remote))
    box.write("README.md", "sandbox\n")
    box.write(lib.DEPLOYED_SCRIPTS_REL + "/marker.txt", "bundle v1\n")
    box.write(lib.DEPLOYED_HOOK_JSON_REL, '{"version": 1, "hooks": {}}\n')
    box.commit("initial")
    box.git("push", "-q", "origin", "main")
    box.git("fetch", "-q", "origin")
    box.git("checkout", "-q", "-b", "feature")
    return box


@pytest.fixture
def sandbox(workdir: Path) -> Sandbox:
    return make_sandbox(workdir)


def fake_select(changed: list[str]) -> dict[str, str]:
    tiers = {"T0": "always"}
    if any(p.startswith("src/") for p in changed):
        tiers["T1"] = "src changed"
    if "pyproject.toml" in changed:
        tiers["T2"] = "deps changed"
    return tiers


def fake_build_checks(tier: str, ctx: policy.Context) -> list[policy.Check]:
    if tier == "T0":
        return [
            policy.Check("t0.ok", "T0", cmd="true"),
            policy.Check("t0.flag", "T0", cmd="test ! -f FAIL_T0"),
        ]
    if tier == "T1":
        return [
            policy.Check("t1.ok", "T1", cmd="true"),
            policy.Check(
                "t1.mutate", "T1", cmd='if [ -f MUTATE ]; then echo x >> "$(cat MUTATE)"; fi'
            ),
        ]
    if tier == "T2":
        return [policy.Check("t2.ok", "T2", cmd="true")]
    return []


@pytest.fixture
def fake_policy() -> SimpleNamespace:
    return SimpleNamespace(
        TIERS=policy.TIERS,
        Context=policy.Context,
        Check=policy.Check,
        select=fake_select,
        build_checks=fake_build_checks,
        MIRRORED_CI_JOBS={},
        CI_FINGERPRINTS={},
        CI_ONLY_LANES=("Windows (CI only)",),
        ASCII_GUARD_PATHS=["src/"],
        VOLATILE_OUTPUTS=("CONFORMANCE.json", ".github/mcp.json"),
        UVLOCK_ALLOWED_HOSTS=policy.UVLOCK_ALLOWED_HOSTS,
    )
