"""Independent round-2 verification of PR #2005 OS-trust child-runtime delivery.

Written FROM SCRATCH by the verifier (not the implementer) to prove -- on a
surface the implementer could not have gamed -- that the shipped ``.pth``
bootstrap delivers OS-trust into a FOREIGN venv that cannot import ``apm_cli``.
That foreign venv is the exact field scenario the round-1 ``PYTHONPATH``/
``sitecustomize`` shim failed on: the real ``llm`` runtime lives in
``~/.apm/runtimes/llm-venv`` which has neither ``apm_cli`` nor (historically)
``truststore``, so the round-1 ``from apm_cli...`` import silently no-op'd and
the child fell back to ``certifi``.

Assertions here are intentionally independent of the implementer's tests:

* V1  -- a genuine foreign venv (no ``apm_cli``) with the shipped bootstrap
  dropped in verifies HTTPS via ``truststore``; removing the ``.pth`` reverts to
  stdlib ``ssl`` (the asymmetry is the proof, not the presence).
* V2  -- the ``.pth`` is additive: a pre-existing user ``sitecustomize.py`` still
  runs (a distinct ``_VERIF_*`` sentinel) AND ``truststore`` still injects.
* V3  -- the real delivery helper ``ensure_child_tls_bootstrap`` lands both files
  in a real venv's site-packages and that interpreter then injects, with
  ``apm_cli`` still not importable in it.
* V4  -- ``configure_tls_trust`` clears the bundled-default marker on all five
  return paths, pops the bundled ``SSL_CERT_FILE`` before a successful inject,
  and restores it when inject raises.
* V6  -- ``build_child_tls_env`` performs no ``PYTHONPATH`` mutation and strips
  the internal marker.

Offline-by-design: ``truststore`` is copied from the running dev environment
into the foreign venv rather than ``pip install``-ed. The interpreter under test
is still a foreign venv with NO ``apm_cli``.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from apm_cli.core.tls_trust import (
    _BUNDLED_CERT_MARKER,
    _DISABLE_ENV_VAR,
    _child_bootstrap_dir,
    _venv_site_packages,
    build_child_tls_env,
    configure_tls_trust,
    ensure_child_tls_bootstrap,
)

pytestmark = pytest.mark.integration

_SSL_CERT_FILE = "SSL_CERT_FILE"

# Independent probe: report the owning module of ssl.SSLContext. truststore
# rewires this to "truststore._api"; a stock interpreter reports plain "ssl".
_SSL_OWNER_PROBE = "import ssl; print(ssl.SSLContext.__module__)"

# Sentinel used ONLY by this verifier's non-shadowing check (distinct name so it
# cannot collide with the implementer's APM_TEST_SENTINEL).
_SITECUSTOMIZE_SENTINEL = "_VERIF_SITECUSTOMIZE_RAN"

_truststore_unavailable = importlib.util.find_spec("truststore") is None
_needs_truststore = pytest.mark.skipif(
    _truststore_unavailable,
    reason="truststore not importable in the dev environment (needed to seed the foreign venv)",
)

# Env vars that must be cleared so the foreign-venv probe starts pristine.
_STRIP_ENV = (
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    _SSL_CERT_FILE,
    "SSL_CERT_DIR",
    _DISABLE_ENV_VAR,
    _BUNDLED_CERT_MARKER,
    "PYTHONPATH",
    _SITECUSTOMIZE_SENTINEL,
)


def _pristine_env() -> dict[str, str]:
    """A copy of os.environ with every trust-related variable removed."""
    return {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}


def _venv_interpreter(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _seed_foreign_venv(root: Path) -> tuple[Path, Path]:
    """Create a real venv WITHOUT apm_cli and seed truststore into it.

    Returns ``(interpreter, site_packages)``. Uses ``sys.executable`` (the dev
    interpreter, >=3.10) so the seeded truststore actually imports; the point is
    that ``apm_cli`` is absent, matching the shipped ``llm`` runtime venv.
    """
    venv = root / "foreign"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
    )
    site_packages = _venv_site_packages(venv)
    assert site_packages is not None, "could not locate foreign venv site-packages"

    import truststore

    truststore_pkg = Path(truststore.__file__).resolve().parent
    shutil.copytree(truststore_pkg, site_packages / "truststore")
    return _venv_interpreter(venv), site_packages


def _copy_shipped_bootstrap(site_packages: Path) -> Path:
    """Copy the SHIPPED bootstrap module + .pth into *site_packages*.

    Returns the path of the copied ``.pth`` so a test can delete it for the
    control probe.
    """
    source = Path(_child_bootstrap_dir())
    shutil.copyfile(source / "_apm_tls_bootstrap.py", site_packages / "_apm_tls_bootstrap.py")
    pth = site_packages / "_apm_tls.pth"
    shutil.copyfile(source / "_apm_tls.pth", pth)
    return pth


def _run_probe(interpreter: Path, probe: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        [str(interpreter), "-c", probe],
        env=_pristine_env() if env is None else env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"probe failed: {result.stderr}"
    return result.stdout.strip()


def _assert_no_apm_cli(interpreter: Path) -> None:
    """Fail unless ``import apm_cli`` genuinely fails in *interpreter*."""
    result = subprocess.run(
        [str(interpreter), "-c", "import apm_cli"],
        env=_pristine_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "foreign venv must NOT be able to import apm_cli"
    assert "ModuleNotFoundError" in result.stderr or "No module named" in result.stderr


# --------------------------------------------------------------------------- V1


@_needs_truststore
def test_v1_foreign_venv_without_apm_cli_gets_os_trust(tmp_path):
    """V1: the .pth bootstrap injects truststore in a venv that cannot import apm_cli."""
    interpreter, site_packages = _seed_foreign_venv(tmp_path)

    # The venv is genuinely foreign: apm_cli is unreachable (round-1 field case).
    _assert_no_apm_cli(interpreter)

    # Control BEFORE the bootstrap: stock stdlib ssl (would verify via certifi).
    assert _run_probe(interpreter, _SSL_OWNER_PROBE) == "ssl"

    # Drop the SHIPPED bootstrap -> the child's ssl becomes truststore-backed.
    pth = _copy_shipped_bootstrap(site_packages)
    owner = _run_probe(interpreter, _SSL_OWNER_PROBE)
    assert owner.startswith("truststore"), (
        f"expected truststore-backed ssl after bootstrap, got {owner!r}"
    )

    # Control AFTER: remove ONLY the .pth (leave the module) -> revert to stdlib
    # ssl. Proves the .pth is the driver, not incidental import side effects.
    pth.unlink()
    assert _run_probe(interpreter, _SSL_OWNER_PROBE) == "ssl"


def test_v1_bootstrap_has_zero_apm_cli_imports():
    """V1: the shipped bootstrap must carry no apm_cli import dependency."""
    source = Path(_child_bootstrap_dir()) / "_apm_tls_bootstrap.py"
    body = source.read_text(encoding="utf-8")
    for line in body.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("import apm_cli"), "bootstrap must not import apm_cli"
        assert not stripped.startswith("from apm_cli"), "bootstrap must not import from apm_cli"


# --------------------------------------------------------------------------- V2


@_needs_truststore
def test_v2_pth_does_not_shadow_user_sitecustomize(tmp_path):
    """V2: a pre-existing user sitecustomize.py still runs AND truststore injects."""
    interpreter, site_packages = _seed_foreign_venv(tmp_path)
    _copy_shipped_bootstrap(site_packages)

    # A user/corporate sitecustomize that records it ran via a distinct sentinel.
    (site_packages / "sitecustomize.py").write_text(
        f'import os\nos.environ["{_SITECUSTOMIZE_SENTINEL}"] = "1"\n',
        encoding="utf-8",
    )

    probe = (
        "import os, ssl;"
        f' print(os.environ.get("{_SITECUSTOMIZE_SENTINEL}"));'
        " print(ssl.SSLContext.__module__)"
    )
    out = _run_probe(interpreter, probe).splitlines()
    sentinel_value, ssl_owner = out[0].strip(), out[1].strip()

    assert sentinel_value == "1", "user sitecustomize.py must still run (no hijack)"
    assert ssl_owner.startswith("truststore"), "bootstrap must ALSO run alongside sitecustomize"


# --------------------------------------------------------------------------- V3


@_needs_truststore
def test_v3_delivery_helper_installs_and_injects(tmp_path):
    """V3: ensure_child_tls_bootstrap lands both files and the venv then injects."""
    interpreter, site_packages = _seed_foreign_venv(tmp_path)
    venv_root = tmp_path / "foreign"

    installed = ensure_child_tls_bootstrap(venv_root)
    assert installed is True, "ensure_child_tls_bootstrap should report success"
    assert (site_packages / "_apm_tls_bootstrap.py").is_file()
    assert (site_packages / "_apm_tls.pth").is_file()

    # The venv still cannot import apm_cli, yet its interpreter now injects.
    _assert_no_apm_cli(interpreter)
    owner = _run_probe(interpreter, _SSL_OWNER_PROBE)
    assert owner.startswith("truststore"), f"helper-installed bootstrap must inject, got {owner!r}"

    # Idempotent re-run and graceful failure on a non-venv path.
    assert ensure_child_tls_bootstrap(venv_root) is True
    assert ensure_child_tls_bootstrap(tmp_path / "does-not-exist") is False


# --------------------------------------------------------------------------- V4


def _fake_truststore(monkeypatch, inject):
    module = types.ModuleType("truststore")
    module.inject_into_ssl = inject
    monkeypatch.setitem(sys.modules, "truststore", module)


def _bundled_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {_BUNDLED_CERT_MARKER: "1", _SSL_CERT_FILE: "/bundled/certifi.pem"}
    if extra:
        env.update(extra)
    return env


def test_v4_marker_cleared_on_opt_out_branch():
    env = _bundled_env({_DISABLE_ENV_VAR: "1"})
    assert configure_tls_trust(env=env) is False
    assert _BUNDLED_CERT_MARKER not in env


def test_v4_marker_cleared_on_explicit_override_branch():
    env = _bundled_env({"REQUESTS_CA_BUNDLE": "/corp/ca.pem"})
    assert configure_tls_trust(env=env) is False
    assert _BUNDLED_CERT_MARKER not in env


def test_v4_marker_cleared_when_truststore_import_fails(monkeypatch):
    # Force `import truststore` to raise ImportError inside configure_tls_trust.
    monkeypatch.setitem(sys.modules, "truststore", None)
    env = _bundled_env()
    assert configure_tls_trust(env=env) is False
    assert _BUNDLED_CERT_MARKER not in env
    # Import fails before the pop-before-inject step, so SSL_CERT_FILE is kept.
    assert env.get(_SSL_CERT_FILE) == "/bundled/certifi.pem"


def test_v4_marker_cleared_and_bundled_popped_on_successful_inject(monkeypatch):
    calls = {"n": 0}

    def _inject():
        calls["n"] += 1

    _fake_truststore(monkeypatch, _inject)
    env = _bundled_env()
    assert configure_tls_trust(env=env) is True
    assert calls["n"] == 1
    assert _BUNDLED_CERT_MARKER not in env
    # B2 crypto: the bundled certifi path is popped before injection so the OS
    # store is consulted (a stale SSL_CERT_FILE would shadow it).
    assert _SSL_CERT_FILE not in env


def test_v4_marker_cleared_and_bundled_restored_when_inject_raises(monkeypatch):
    def _boom():
        raise RuntimeError("inject failure")

    _fake_truststore(monkeypatch, _boom)
    env = _bundled_env()
    assert configure_tls_trust(env=env) is False
    assert _BUNDLED_CERT_MARKER not in env
    # B2 crypto: on failure the bundled path is RESTORED so hosts without an OS
    # store still verify against certifi (never end with zero trust).
    assert env.get(_SSL_CERT_FILE) == "/bundled/certifi.pem"


# --------------------------------------------------------------------------- V6


def test_v6_build_child_tls_env_strips_marker_without_pythonpath_mutation():
    base = {
        _BUNDLED_CERT_MARKER: "1",
        "PATH": "/usr/bin",
        "HOME": "/home/verif",
    }
    child = build_child_tls_env(base)
    assert _BUNDLED_CERT_MARKER not in child, "internal marker must be stripped from child env"
    # No PYTHONPATH injection: the round-1 shim mechanism is gone.
    assert "PYTHONPATH" not in child
    # Non-marker vars pass through untouched.
    assert child["PATH"] == "/usr/bin"
    assert child["HOME"] == "/home/verif"
    # The input mapping is not mutated in place.
    assert _BUNDLED_CERT_MARKER in base


def test_v6_build_child_tls_env_preserves_existing_pythonpath():
    """A caller-set PYTHONPATH must pass through unchanged (no TLS shim prepend)."""
    base = {"PYTHONPATH": "/caller/libs", _BUNDLED_CERT_MARKER: "1"}
    child = build_child_tls_env(base)
    assert child.get("PYTHONPATH") == "/caller/libs"
