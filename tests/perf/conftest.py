"""Shared fixtures for opt-in perf scenarios.

Skips ALL tests in ``tests/perf`` unless ``PYTEST_PERF=1`` is set.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

PERF_ENABLED = os.environ.get("PYTEST_PERF") == "1"


def pytest_collection_modifyitems(config, items):
    """Apply the opt-in skip marker to every test in this directory."""
    skip = pytest.mark.skipif(
        not PERF_ENABLED, reason="Perf scenarios are opt-in: set PYTEST_PERF=1 to run"
    )
    for item in items:
        item.add_marker(skip)


CLONE_ROOT = Path("/tmp/perf-atlas-clones")


@dataclass(frozen=True)
class CloneSpec:
    name: str
    url: str


def _clone(spec: CloneSpec) -> Path:
    CLONE_ROOT.mkdir(parents=True, exist_ok=True)
    target = CLONE_ROOT / spec.name
    if target.exists():
        return target
    subprocess.run(
        ["git", "clone", "--depth", "1", spec.url, str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    return target


@pytest.fixture(scope="session")
def kubernetes_clone() -> Path:
    return _clone(CloneSpec(name="kubernetes", url="https://github.com/kubernetes/kubernetes.git"))


@pytest.fixture(scope="session")
def typescript_clone() -> Path:
    return _clone(CloneSpec(name="typescript", url="https://github.com/microsoft/TypeScript.git"))


def write_manifest(root: Path, body: str) -> Path:
    p = root / "apm.yml"
    p.write_text(body, encoding="utf-8")
    return p


def make_local_packages(root: Path, names: Iterable[str]) -> list[Path]:
    """Create N local packages under *root*/packages/<name>/ with one .agent.md each."""
    out: list[Path] = []
    pkg_root = root / "packages"
    pkg_root.mkdir(parents=True, exist_ok=True)
    for n in names:
        pkg = pkg_root / n
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "apm.yml").write_text(
            f"name: {n}\nversion: 0.0.1\ndescription: synthetic perf fixture\n",
            encoding="utf-8",
        )
        agents_dir = pkg / ".apm" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / f"{n}.agent.md").write_text(
            f"---\nname: {n}\n---\n# {n}\n",
            encoding="utf-8",
        )
        out.append(pkg)
    return out


def clean_apm_modules(root: Path) -> None:
    for sub in ("apm_modules", ".agents", ".github/agents", ".github/instructions"):
        path = root / sub
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    lock = root / "apm.lock.yaml"
    if lock.exists():
        lock.unlink()
