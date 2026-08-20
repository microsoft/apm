"""T1 (H1 regression): the built wheel must ship the child-TLS delivery files.

The round-2 mechanism drops ``_apm_tls_bootstrap.py`` + ``_apm_tls.pth`` into a
child venv's site-packages. A red team found the ``.pth`` was silently DROPPED
from the wheel (setuptools' ``packages.find`` ships only ``.py`` and there was
no package-data), so ``ensure_child_tls_bootstrap`` could not copy it on the
PyPI channel -> child trust was a silent no-op.

Round-3 fixes this two ways: the ``.pth`` is generated inline (so delivery no
longer depends on packaging) AND ``[tool.setuptools.package-data]`` now ships
the ``.pth`` too. This test is the belt-and-suspenders regression guard: build
the wheel hermetically and assert BOTH files are present in the archive.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _repo_root() -> Path:
    current = Path(__file__).resolve().parent
    for parent in (current, *current.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("Cannot locate repository root")


def _build_wheel(out_dir: Path) -> Path:
    """Build a wheel into *out_dir* and return its path. Prefer uv, fall back to build."""
    repo = _repo_root()
    if shutil.which("uv"):
        cmd = ["uv", "build", "--wheel", "--out-dir", str(out_dir)]
    else:
        cmd = [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)]
    result = subprocess.run(
        cmd,
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(
            f"wheel build unavailable in this environment: {result.stdout}\n{result.stderr}"
        )
    wheels = list(out_dir.glob("*.whl"))
    if not wheels:
        pytest.skip("wheel build produced no .whl artifact")
    return wheels[0]


def test_wheel_ships_child_tls_bootstrap_and_pth(tmp_path):
    wheel = _build_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()

    module = "apm_cli/core/_child_tls/_apm_tls_bootstrap.py"
    pth = "apm_cli/core/_child_tls/_apm_tls.pth"
    assert module in names, f"{module} missing from wheel: {names}"
    assert pth in names, f"{pth} missing from wheel: {names}"


def _build_sdist(out_dir: Path) -> Path:
    """Build an sdist into *out_dir* and return its path. Prefer uv, fall back to build."""
    repo = _repo_root()
    if shutil.which("uv"):
        cmd = ["uv", "build", "--sdist", "--out-dir", str(out_dir)]
    else:
        cmd = [sys.executable, "-m", "build", "--sdist", "--outdir", str(out_dir)]
    result = subprocess.run(
        cmd,
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(
            f"sdist build unavailable in this environment: {result.stdout}\n{result.stderr}"
        )
    sdists = list(out_dir.glob("*.tar.gz"))
    if not sdists:
        pytest.skip("sdist build produced no .tar.gz artifact")
    return sdists[0]


def test_sdist_ships_child_tls_bootstrap_and_pth(tmp_path):
    """LOW-1 (round-4): guard the sdist channel too.

    The bootstrap MODULE is a plain package ``.py`` (auto-shipped), but the
    ``.pth`` rides on ``package-data`` and can silently diverge from the wheel
    across setuptools versions. Assert BOTH members are in the tarball so a
    future backend bump that drops the ``.pth`` from the sdist is caught.
    """
    sdist = _build_sdist(tmp_path)
    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()

    # sdist members are prefixed with the top-level "<name>-<version>/" dir.
    module_tail = "src/apm_cli/core/_child_tls/_apm_tls_bootstrap.py"
    pth_tail = "src/apm_cli/core/_child_tls/_apm_tls.pth"
    assert any(n.endswith(module_tail) for n in names), f"{module_tail} missing from sdist: {names}"
    assert any(n.endswith(pth_tail) for n in names), f"{pth_tail} missing from sdist: {names}"
