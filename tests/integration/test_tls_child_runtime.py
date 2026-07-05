"""C1 verifier: OS-trust must reach a FOREIGN child venv via the .pth bootstrap.

The flagship ``llm`` runtime runs in its own venv (``~/.apm/runtimes/llm-venv``)
that has NEITHER ``apm_cli`` NOR (historically) ``truststore``. The round-1
design re-ran ``configure_tls_trust`` in the child by prepending a
``sitecustomize`` shim dir to the child ``PYTHONPATH``; in the real ``llm`` venv
that import failed silently, so the child fell back to ``certifi`` and ``apm
run`` still failed behind a proxy. It also shadowed any user ``sitecustomize``.

The round-2 mechanism delivers trust at venv-setup time instead: APM installs
``truststore`` into the runtime venv and copies a self-contained ``.pth``
bootstrap into its site-packages, so the child interpreter injects the OS trust
store at startup with no ``apm_cli`` dependency and no ``PYTHONPATH`` mutation.

These tests spawn a genuine FOREIGN venv (created with ``python -m venv``, WITH
NO ``apm_cli`` installed) and prove:

* C1 -- with the shipped ``_apm_tls_bootstrap.py`` + ``_apm_tls.pth`` dropped in,
  the child's ``ssl.SSLContext`` becomes truststore-backed; remove the ``.pth``
  and it reverts to stdlib ``ssl`` (the asymmetry is the proof).
* T2 -- the ``.pth`` is additive: a pre-existing user ``sitecustomize.py`` in the
  same venv still runs AND truststore still injects.

Offline-by-design: ``truststore`` is copied from the running dev environment
into the foreign venv rather than ``pip install``-ed, so the tests need no
network. The interpreter under test is still a foreign venv without ``apm_cli``.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from apm_cli.core.tls_trust import _child_bootstrap_dir, _venv_site_packages

pytestmark = pytest.mark.integration

_truststore_missing = importlib.util.find_spec("truststore") is None
_requires_truststore = pytest.mark.skipif(
    _truststore_missing, reason="truststore not importable in this environment"
)

# Child that reports which module owns ssl.SSLContext -- truststore-backed after
# the bootstrap runs, plain "ssl" otherwise.
_SSL_MODULE_PROBE = "import ssl; print(ssl.SSLContext.__module__)"

_TRUST_ENV_VARS = (
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "APM_DISABLE_TRUSTSTORE",
    "APM_SSL_CERT_FILE_IS_BUNDLED_DEFAULT",
    "PYTHONPATH",
)


def _clean_env() -> dict[str, str]:
    """os.environ copy with every trust-related var stripped (pristine start)."""
    return {k: v for k, v in os.environ.items() if k not in _TRUST_ENV_VARS}


def _venv_python(venv: Path) -> Path:
    """Return the interpreter path inside *venv* for the current platform."""
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _make_foreign_venv(root: Path) -> tuple[Path, Path]:
    """Create a foreign venv (no apm_cli) and return (venv_python, site_packages).

    ``truststore`` is copied in from the running dev environment so the test is
    fully offline; ``apm_cli`` is deliberately NOT installed so the interpreter
    matches the real ``llm`` runtime venv.
    """
    venv = root / "foreign-venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
    )
    site_packages = _venv_site_packages(venv)
    assert site_packages is not None, "could not locate foreign venv site-packages"

    import truststore

    ts_src = Path(truststore.__file__).resolve().parent
    shutil.copytree(ts_src, site_packages / "truststore")

    return _venv_python(venv), site_packages


def _drop_bootstrap(site_packages: Path) -> None:
    """Copy the shipped bootstrap module + .pth into *site_packages*."""
    source = Path(_child_bootstrap_dir())
    shutil.copyfile(source / "_apm_tls_bootstrap.py", site_packages / "_apm_tls_bootstrap.py")
    shutil.copyfile(source / "_apm_tls.pth", site_packages / "_apm_tls.pth")


def _probe_ssl_module(venv_python: Path) -> str:
    result = subprocess.run(
        [str(venv_python), "-c", _SSL_MODULE_PROBE],
        env=_clean_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@_requires_truststore
def test_foreign_venv_bootstrap_injects_truststore(tmp_path):
    """C1: the shipped .pth bootstrap makes a foreign venv verify via the OS store."""
    venv_python, site_packages = _make_foreign_venv(tmp_path)

    # Control first: no bootstrap -> stdlib ssl. Proves the venv is foreign and
    # would otherwise verify against certifi (the field failure mode).
    assert _probe_ssl_module(venv_python) == "ssl", "foreign venv should start on stdlib ssl"

    # Drop the bootstrap -> the child's ssl becomes truststore-backed.
    _drop_bootstrap(site_packages)
    module = _probe_ssl_module(venv_python)
    assert module.startswith("truststore"), (
        f"child ssl module should be truststore-backed after bootstrap, got {module!r}"
    )


@_requires_truststore
def test_bootstrap_is_additive_to_user_sitecustomize(tmp_path):
    """T2: the .pth bootstrap does not shadow a user sitecustomize -- both run."""
    venv_python, site_packages = _make_foreign_venv(tmp_path)
    _drop_bootstrap(site_packages)

    sentinel = tmp_path / "sitecustomize-ran.txt"
    (site_packages / "sitecustomize.py").write_text(
        "\n".join(
            [
                "import os",
                "import pathlib",
                "pathlib.Path(os.environ['APM_TEST_SENTINEL']).write_text('ran', encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )

    env = _clean_env()
    env["APM_TEST_SENTINEL"] = str(sentinel)
    result = subprocess.run(
        [str(venv_python), "-c", _SSL_MODULE_PROBE],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    # The user sitecustomize ran (bootstrap did not shadow it)...
    assert sentinel.exists(), "user sitecustomize.py must still run alongside the .pth bootstrap"
    assert sentinel.read_text(encoding="utf-8") == "ran"
    # ...AND truststore still injected (the .pth is additive, not exclusive).
    assert result.stdout.strip().startswith("truststore"), (
        f"truststore must still inject with a user sitecustomize present, got {result.stdout!r}"
    )
