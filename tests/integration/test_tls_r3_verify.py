"""Independent round-3 verification of PR #2005 OS-trust delivery (issue #2004).

Written FROM SCRATCH by the verifier (not the implementer) to prove -- on the
one surface the HIGH bug lived on -- that the child-trust bootstrap survives a
real setuptools wheel build/install and reaches a foreign interpreter.

Round-2 shipped a ``.pth`` + bootstrap module into a child venv's
site-packages, but a red team found the ``.pth`` was silently DROPPED from the
wheel (``packages.find`` ships only ``.py`` and there was no package-data), so
``ensure_child_tls_bootstrap`` had nothing to copy on the PyPI channel and child
trust was a no-op. Round-3 (a) generates the ``.pth`` inline so delivery no
longer depends on packaging, and (b) adds ``package-data`` so the wheel ships it
anyway.

The gates here are deliberately independent of the implementer's tests:

* V1 -- build the wheel, assert BOTH child-TLS files are in the archive, pip
  install the wheel into a clean venv, then from THAT venv deliver the bootstrap
  into a SECOND foreign venv (no ``apm_cli``) and prove its interpreter injects
  ``truststore`` (with the ``.pth`` removed as the negative control). This is the
  exact end-to-end chain the wheel bug broke.
* V2 -- ``build_child_tls_env`` drops a bundled-certifi ``SSL_CERT_FILE`` (so the
  child reaches the OS store on Linux) but preserves a genuine user value.
* V3 -- ``ensure_child_tls_bootstrap`` writes atomically, leaves no partial file
  when the atomic replace fails, and writes the module BEFORE the ``.pth``.
* V4 -- the runtime setup scripts install ``truststore`` best-effort (a failure
  never aborts setup) and pin ``truststore>=0.10.0``.
* V5 -- the ssl-issues doc surfaces the Node/Codex scope caveat early with the
  ``NODE_EXTRA_CA_CERTS`` workaround, keeps the ``PIP_CERT`` and stale-bundle
  notes, and the CHANGELOG scopes coverage to the Python-based paths.

Offline-by-design: ``truststore`` is copied from the running test environment
into the foreign venv rather than pip-installed. The interpreter under test is
still a foreign venv with NO ``apm_cli``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration]


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _repo_root() -> Path:
    current = Path(__file__).resolve().parent
    for parent in (current, *current.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("Cannot locate repository root")


def _uv() -> str | None:
    return shutil.which("uv")


def _venv_python(venv: Path) -> Path:
    posix = venv / "bin" / "python"
    windows = venv / "Scripts" / "python.exe"
    return posix if posix.exists() else windows


def _make_venv(venv: Path) -> None:
    """Create a venv at *venv* using the same interpreter running the tests.

    Prefers ``uv venv`` (the repo toolchain) with the current interpreter
    pinned, falling back to the stdlib ``venv`` module.
    """
    uv = _uv()
    if uv:
        subprocess.run(
            [uv, "venv", "--python", sys.executable, str(venv)],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            check=True,
            capture_output=True,
            text=True,
        )


def _pip_install(target_python: Path, spec: str) -> None:
    uv = _uv()
    if uv:
        subprocess.run(
            [uv, "pip", "install", "--python", str(target_python), spec],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        subprocess.run(
            [str(target_python), "-m", "pip", "install", spec],
            check=True,
            capture_output=True,
            text=True,
        )


def _site_packages(venv: Path) -> Path:
    candidates = list(venv.glob("lib/python*/site-packages"))
    candidates.extend(venv.glob("Lib/site-packages"))
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise AssertionError(f"no site-packages under {venv}")


def _run_python(python: Path, code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(python), "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )


def _build_wheel(out_dir: Path) -> Path:
    repo = _repo_root()
    uv = _uv()
    if uv:
        cmd = [uv, "build", "--wheel", "--out-dir", str(out_dir)]
    else:
        cmd = [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)]
    result = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.skip(f"wheel build unavailable: {result.stdout}\n{result.stderr}")
    wheels = list(out_dir.glob("*.whl"))
    if not wheels:
        pytest.skip("wheel build produced no .whl artifact")
    return wheels[0]


def _copy_truststore_into(site_packages: Path) -> None:
    """Copy the pure-Python ``truststore`` package offline into *site_packages*."""
    try:
        import truststore
    except Exception:  # pragma: no cover - environment guard
        pytest.skip("truststore not importable in the test environment to copy offline")
    src = Path(truststore.__file__).resolve().parent
    shutil.copytree(src, site_packages / "truststore", dirs_exist_ok=True)


# --------------------------------------------------------------------------- #
# V1 (H1): wheel build + install + foreign-venv delivery + injection
# --------------------------------------------------------------------------- #
@pytest.mark.slow
def test_v1_wheel_delivers_os_trust_end_to_end(tmp_path):
    # 1. Build the wheel and 2. assert BOTH child-TLS files are archived.
    wheel = _build_wheel(tmp_path / "wheel")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    module_member = "apm_cli/core/_child_tls/_apm_tls_bootstrap.py"
    pth_member = "apm_cli/core/_child_tls/_apm_tls.pth"
    assert module_member in names, f"wheel missing bootstrap module: {names}"
    assert pth_member in names, f"H1 regression: wheel missing .pth: {names}"

    # 3. Install the BUILT WHEEL into a clean venv (not editable, not source).
    apm_venv = tmp_path / "apm"
    _make_venv(apm_venv)
    apm_python = _venv_python(apm_venv)
    _pip_install(apm_python, str(wheel))
    located = _run_python(
        apm_python,
        "import apm_cli.core.tls_trust as t; print(t.__file__)",
    )
    assert located.returncode == 0, located.stderr
    installed_file = Path(located.stdout.strip())
    assert apm_venv in installed_file.parents, (
        f"apm_cli must import from the wheel venv, got {installed_file}"
    )

    # 4. From the wheel venv, deliver the bootstrap into a SECOND foreign venv.
    foreign_venv = tmp_path / "foreign"
    _make_venv(foreign_venv)
    foreign_python = _venv_python(foreign_venv)
    no_apm = _run_python(foreign_python, "import apm_cli")
    assert no_apm.returncode != 0, "foreign venv must NOT have apm_cli"

    delivered = _run_python(
        apm_python,
        (
            "from apm_cli.core.tls_trust import ensure_child_tls_bootstrap;"
            f"print(ensure_child_tls_bootstrap({str(foreign_venv)!r}))"
        ),
    )
    assert delivered.returncode == 0, delivered.stderr
    assert delivered.stdout.strip() == "True", delivered.stdout
    foreign_sp = _site_packages(foreign_venv)
    assert (foreign_sp / "_apm_tls_bootstrap.py").is_file()
    assert (foreign_sp / "_apm_tls.pth").is_file()

    # 5. Make truststore importable in the foreign venv and prove injection.
    _copy_truststore_into(foreign_sp)
    with_pth = _run_python(foreign_python, "import ssl; print(ssl.SSLContext.__module__)")
    assert with_pth.returncode == 0, with_pth.stderr
    assert with_pth.stdout.strip().startswith("truststore"), with_pth.stdout

    # Negative control: remove the .pth -> stdlib ssl is back.
    (foreign_sp / "_apm_tls.pth").unlink()
    without_pth = _run_python(foreign_python, "import ssl; print(ssl.SSLContext.__module__)")
    assert without_pth.returncode == 0, without_pth.stderr
    assert without_pth.stdout.strip() == "ssl", without_pth.stdout

    # apm_cli is still absent from the foreign interpreter under test.
    still_no_apm = _run_python(foreign_python, "import apm_cli")
    assert still_no_apm.returncode != 0


# --------------------------------------------------------------------------- #
# V2 (M2): child env drops bundled certifi, preserves a user value
# --------------------------------------------------------------------------- #
def test_v2_build_child_env_drops_bundled_certifi():
    import certifi

    from apm_cli.core.tls_trust import build_child_tls_env

    marker = "APM_SSL_CERT_FILE_IS_BUNDLED_DEFAULT"

    # Bundled certifi (== certifi.where()) is dropped; marker is popped.
    bundled = build_child_tls_env({"SSL_CERT_FILE": certifi.where(), marker: "1", "PATH": "/keep"})
    assert "SSL_CERT_FILE" not in bundled
    assert marker not in bundled
    assert bundled.get("PATH") == "/keep"

    # A genuine user SSL_CERT_FILE is preserved; marker still popped.
    user = build_child_tls_env({"SSL_CERT_FILE": "/etc/pki/corp-ca.pem", marker: "1"})
    assert user.get("SSL_CERT_FILE") == "/etc/pki/corp-ca.pem"
    assert marker not in user


def test_v2_build_child_env_drops_frozen_certifi_shape():
    from apm_cli.core.tls_trust import build_child_tls_env

    # The frozen hook sets SSL_CERT_FILE to a certifi/cacert.pem path under
    # _MEIPASS whose prefix differs from the live certifi.where(); the tail
    # match must still classify it as bundled and drop it. Cover both slashes.
    posix = build_child_tls_env({"SSL_CERT_FILE": "/var/_MEIabc/certifi/cacert.pem"})
    assert "SSL_CERT_FILE" not in posix
    windows = build_child_tls_env({"SSL_CERT_FILE": "C:\\Temp\\_MEI9\\certifi\\cacert.pem"})
    assert "SSL_CERT_FILE" not in windows


# --------------------------------------------------------------------------- #
# V3 (M3): atomic write, no partial file on failure, module before .pth
# --------------------------------------------------------------------------- #
def test_v3_no_partial_file_when_replace_fails(tmp_path, monkeypatch):
    from apm_cli.core import tls_trust

    site_packages = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)

    def boom(src, dst, *args, **kwargs):
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(os, "replace", boom)
    result = tls_trust.ensure_child_tls_bootstrap(tmp_path / "venv")

    assert result is False
    leftovers = sorted(p.name for p in site_packages.iterdir())
    assert not (site_packages / "_apm_tls_bootstrap.py").exists(), leftovers
    assert not (site_packages / "_apm_tls.pth").exists(), leftovers
    assert not any(name.endswith(".tmp") or name.startswith(".apm_tls_") for name in leftovers), (
        f"stray temp file left behind: {leftovers}"
    )


def test_v3_writes_module_before_pth(tmp_path, monkeypatch):
    from apm_cli.core import tls_trust

    site_packages = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)

    order: list[str] = []
    original = tls_trust._atomic_write

    def spy(target, data):
        order.append(Path(target).name)
        return original(target, data)

    monkeypatch.setattr(tls_trust, "_atomic_write", spy)
    result = tls_trust.ensure_child_tls_bootstrap(tmp_path / "venv")

    assert result is True
    assert order == ["_apm_tls_bootstrap.py", "_apm_tls.pth"], order
    assert (site_packages / "_apm_tls_bootstrap.py").is_file()
    assert (site_packages / "_apm_tls.pth").is_file()
    assert (site_packages / "_apm_tls.pth").read_text(encoding="ascii") == (
        "import _apm_tls_bootstrap\n"
    )


# --------------------------------------------------------------------------- #
# V4 (M1): runtime setup scripts install truststore best-effort + pinned
# --------------------------------------------------------------------------- #
def test_v4_setup_llm_truststore_is_best_effort_and_pinned():
    root = _repo_root()
    sh = (root / "scripts" / "runtime" / "setup-llm.sh").read_text(encoding="utf-8")
    ps1 = (root / "scripts" / "runtime" / "setup-llm.ps1").read_text(encoding="utf-8")

    # Both scripts pin the floor so the child gets an OS-trust-capable truststore.
    assert "truststore>=0.10.0" in sh
    assert "truststore>=0.10.0" in ps1
    assert "sys.version_info < (3, 10)" in sh
    assert "sys.version_info < (3, 10)" in ps1
    assert "PIP_CERT" in sh
    assert "PIP_CERT" in ps1
    docs_url = "https://microsoft.github.io/apm/troubleshooting/ssl-issues/"
    assert docs_url in sh
    assert docs_url in ps1

    # Bash: the pip install is guarded by an if block, so a failure under
    # `set -euo pipefail` does not abort setup.
    assert 'if ! "$llm_venv/bin/pip" install "truststore>=0.10.0"; then' in sh

    # PowerShell: the install lives inside a try/catch that downgrades to a
    # warning rather than surfacing under `$ErrorActionPreference = 'Stop'`.
    tail = ps1[ps1.index("truststore>=0.10.0") :]
    assert "} catch {" in tail
    assert "Write-WarningText" in tail


def test_v4_best_effort_control_flow_does_not_abort(tmp_path):
    # Dynamically prove the bash guard: a failing `pip` under `set -euo
    # pipefail` guarded by `|| log_warning` still exits 0 and runs later steps.
    script = tmp_path / "probe.sh"
    script.write_text(
        "set -euo pipefail\n"
        'log_warning() { echo "[!] $*"; }\n'
        "pip() { echo 'simulated failure' >&2; return 1; }\n"
        "pip install 'truststore>=0.10.0' || log_warning 'truststore install failed'\n"
        "echo CONTINUED\n",
        encoding="ascii",
    )
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "CONTINUED" in result.stdout


# --------------------------------------------------------------------------- #
# V5 (M5/L1/M4-docs): docs + changelog scope honesty
# --------------------------------------------------------------------------- #
def test_v5_ssl_docs_early_caveat_and_notes():
    docs = (
        _repo_root() / "docs" / "src" / "content" / "docs" / "troubleshooting" / "ssl-issues.md"
    ).read_text(encoding="utf-8")

    lines = docs.splitlines()
    heading = "## Default behaviour: the OS trust store"
    heading_idx = next(i for i, line in enumerate(lines) if line.strip() == heading)
    caveat_idx = next(i for i, line in enumerate(lines) if "Scope caveat" in line)
    assert caveat_idx > heading_idx
    # The caveat must be near the top of the section, not buried far below.
    gap = sum(1 for line in lines[heading_idx + 1 : caveat_idx] if line.strip())
    assert gap <= 2, f"Node/Codex caveat must sit within ~2 content lines, got {gap}"

    known_limits = docs.index("### Known limitations")
    early_region = docs[docs.index(heading) : known_limits]
    assert "NODE_EXTRA_CA_CERTS" in early_region

    # M4-docs: pip's own cert resolution caveat during setup.
    assert "PIP_CERT" in docs
    # L1: REQUESTS_CA_BUNDLE replaces (not augments) + the stale-bundle note.
    assert "*replaces*" in docs
    assert "stale `REQUESTS_CA_BUNDLE`" in docs


def test_v5_changelog_scopes_python_based():
    changelog = (_repo_root() / "CHANGELOG.md").read_text(encoding="utf-8")
    start = changelog.index("## [Unreleased]")
    rest = changelog[start + len("## [Unreleased]") :]
    end = rest.find("\n## [")
    block = rest if end == -1 else rest[:end]

    assert "#2005" in block
    assert "Python-based" in block
    assert "#2034" in block
    assert "not yet covered" in block
    # The stale round-1 joint claim (child runtimes covered) must be gone.
    assert "and `apm run` (child runtimes)" not in block
