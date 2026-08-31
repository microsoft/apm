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
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from apm_cli.core.tls_trust import (
    _DERIVED_REQUESTS_CA_MARKER,
    _DISABLE_ENV_VAR,
    _EXTRA_CA_ENV_VAR,
    _NODE_EXTRA_CA_ENV_VAR,
    _child_bootstrap_dir,
    _venv_site_packages,
    build_child_tls_env,
)
from apm_cli.runtime.manager import RuntimeManager

from ._tls_ca_server import private_ca_https_server
from .test_tls_custom_ca import _OPENSSL_EXECUTABLE

pytestmark = pytest.mark.integration

_truststore_missing = importlib.util.find_spec("truststore") is None
_requires_truststore = pytest.mark.skipif(
    _truststore_missing, reason="truststore not importable in this environment"
)
_requires_openssl = pytest.mark.skipif(
    _OPENSSL_EXECUTABLE is None, reason="openssl CLI not available"
)
_NODE_EXECUTABLE = shutil.which("node")
_requires_node = pytest.mark.skipif(_NODE_EXECUTABLE is None, reason="node is not available")

# Child that reports which module owns ssl.SSLContext -- truststore-backed after
# the bootstrap runs, plain "ssl" otherwise.
_SSL_MODULE_PROBE = "import ssl; print(ssl.SSLContext.__module__)"

_TRUST_ENV_VARS = (
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "APM_DISABLE_TRUSTSTORE",
    "APM_EXTRA_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "APM_NODE_EXTRA_CA_CERTS_IS_DERIVED_ADDITIVE",
    "APM_REQUESTS_CA_BUNDLE_IS_DERIVED_ADDITIVE",
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


@_requires_truststore
@_requires_openssl
def test_foreign_child_bootstrap_adds_private_ca_on_real_loopback(tmp_path):
    """A foreign child keeps OS truststore and adds the selected private root."""
    venv_python, site_packages = _make_foreign_venv(tmp_path)
    _drop_bootstrap(site_packages)
    server_dir = tmp_path / "private-ca-server"
    server_dir.mkdir()

    child_probe = (
        "import ssl, sys, urllib.request\n"
        "print(ssl.SSLContext.__module__)\n"
        "print(urllib.request.urlopen(sys.argv[1], timeout=5).read().decode('ascii'))\n"
    )
    with private_ca_https_server(server_dir) as server:
        # Control: OS trust alone must reject this fresh private root.
        rejected = subprocess.run(
            [str(venv_python), "-c", child_probe, server.url],
            env=_clean_env(),
            capture_output=True,
            text=True,
        )
        assert rejected.returncode != 0

        env = _clean_env()
        env["APM_EXTRA_CA_BUNDLE"] = server.ca_path
        accepted = subprocess.run(
            [str(venv_python), "-c", child_probe, server.url],
            env=env,
            capture_output=True,
            text=True,
        )

    assert accepted.returncode == 0, accepted.stderr
    output = accepted.stdout.splitlines()
    assert output[0].startswith("truststore")
    assert output[-1] == "ok"


@_requires_truststore
@_requires_openssl
def test_bootstrap_preserves_derived_marker_for_python_descendants(tmp_path):
    """A managed child and its same-venv descendant both keep OS-plus-extra trust."""
    venv_python, site_packages = _make_foreign_venv(tmp_path)
    _drop_bootstrap(site_packages)
    server_dir = tmp_path / "two-generation-private-ca"
    server_dir.mkdir()

    inner_probe = (
        "import json, os, ssl, sys, urllib.request; "
        "body = urllib.request.urlopen(sys.argv[1], timeout=5).read().decode('ascii'); "
        "print(json.dumps({'module': ssl.SSLContext.__module__, 'body': body, "
        "'marker_matches': os.environ.get('APM_REQUESTS_CA_BUNDLE_IS_DERIVED_ADDITIVE') "
        "== os.environ.get('REQUESTS_CA_BUNDLE')}))"
    )
    outer_probe = (
        "import json, os, ssl, subprocess, sys, urllib.request\n"
        f"inner_probe = {inner_probe!r}\n"
        "body = urllib.request.urlopen(sys.argv[1], timeout=5).read().decode('ascii')\n"
        "inner = subprocess.run([sys.executable, '-c', inner_probe, sys.argv[1]], "
        "env=os.environ.copy(), capture_output=True, text=True)\n"
        "print(json.dumps({'module': ssl.SSLContext.__module__, 'body': body, "
        "'marker_matches': os.environ.get('APM_REQUESTS_CA_BUNDLE_IS_DERIVED_ADDITIVE') "
        "== os.environ.get('REQUESTS_CA_BUNDLE'), 'inner_returncode': inner.returncode, "
        "'inner_stdout': inner.stdout, 'inner_stderr': inner.stderr}))\n"
    )

    with private_ca_https_server(server_dir) as server:
        env = build_child_tls_env({**_clean_env(), _EXTRA_CA_ENV_VAR: server.ca_path})
        result = subprocess.run(
            [str(venv_python), "-c", outer_probe, server.url],
            env=env,
            capture_output=True,
            text=True,
        )

    assert result.returncode == 0, result.stderr
    outer = json.loads(result.stdout.splitlines()[-1])
    assert outer["module"].startswith("truststore")
    assert outer["body"] == "ok"
    assert outer["marker_matches"] is True
    assert outer["inner_returncode"] == 0, outer["inner_stderr"]
    inner = json.loads(outer["inner_stdout"].splitlines()[-1])
    assert inner["module"].startswith("truststore")
    assert inner["body"] == "ok"
    assert inner["marker_matches"] is True


@_requires_openssl
def test_bootstrap_rolls_back_after_post_injection_failure(tmp_path):
    """A late child-bootstrap failure restores stdlib TLS and keeps fallback env."""
    import certifi

    venv_python, site_packages = _make_foreign_venv(tmp_path)
    (site_packages / "truststore" / "__init__.py").write_text(
        """\
import ssl

_ORIGINAL_CONTEXT = ssl.SSLContext

class SSLContext(_ORIGINAL_CONTEXT):
    def __init__(self, protocol=None):
        super().__init__(protocol)
        self.check_hostname = False

def inject_into_ssl():
    ssl.SSLContext = SSLContext
""",
        encoding="ascii",
    )
    _drop_bootstrap(site_packages)
    fallback_path = certifi.where()
    env = _clean_env()
    env.update(
        {
            _EXTRA_CA_ENV_VAR: fallback_path,
            "REQUESTS_CA_BUNDLE": fallback_path,
            _DERIVED_REQUESTS_CA_MARKER: fallback_path,
        }
    )
    probe = (
        "import json, os, ssl; "
        "print(json.dumps({'module': ssl.SSLContext.__module__, "
        "'marker_matches': os.environ.get('APM_REQUESTS_CA_BUNDLE_IS_DERIVED_ADDITIVE') "
        "== os.environ.get('REQUESTS_CA_BUNDLE')}))"
    )

    result = subprocess.run(
        [str(venv_python), "-c", probe],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout.splitlines()[-1])
    assert evidence == {"module": "ssl", "marker_matches": True}


@_requires_node
@_requires_openssl
def test_node_child_consumes_derived_extra_ca_on_real_loopback(tmp_path):
    """Node rejects the private root by default and accepts APM's stable snapshot."""
    assert _NODE_EXECUTABLE is not None
    server_dir = tmp_path / "node ca path with spaces"
    server_dir.mkdir()
    node_probe = """
const https = require('https');
https.get(process.env.APM_TEST_URL, response => {
  let body = '';
  response.on('data', chunk => { body += chunk; });
  response.on('end', () => { console.log(body); });
}).on('error', error => {
  console.error(error.message);
  process.exitCode = 2;
});
"""

    with private_ca_https_server(server_dir) as server:
        control_env = {**_clean_env(), "APM_TEST_URL": server.url}
        rejected = subprocess.run(
            [_NODE_EXECUTABLE, "-e", node_probe],
            env=control_env,
            capture_output=True,
            text=True,
        )
        assert rejected.returncode != 0

        derived_env = build_child_tls_env({**control_env, _EXTRA_CA_ENV_VAR: server.ca_path})
        accepted = subprocess.run(
            [_NODE_EXECUTABLE, "-e", node_probe],
            env=derived_env,
            capture_output=True,
            text=True,
        )
        disabled_env = build_child_tls_env({**derived_env, _DISABLE_ENV_VAR: "1"})
        disabled = subprocess.run(
            [_NODE_EXECUTABLE, "-e", node_probe],
            env=disabled_env,
            capture_output=True,
            text=True,
        )

        explicit_env = build_child_tls_env(
            {
                **control_env,
                _EXTRA_CA_ENV_VAR: server.ca_path,
                _NODE_EXTRA_CA_ENV_VAR: server.ca_path,
            }
        )
        explicit = subprocess.run(
            [_NODE_EXECUTABLE, "-e", node_probe],
            env=explicit_env,
            capture_output=True,
            text=True,
        )

    assert Path(derived_env[_NODE_EXTRA_CA_ENV_VAR]).is_file()
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.strip() == "ok"
    assert _NODE_EXTRA_CA_ENV_VAR not in disabled_env
    assert disabled.returncode != 0
    assert explicit_env[_NODE_EXTRA_CA_ENV_VAR] == server.ca_path
    assert explicit.returncode == 0, explicit.stderr
    assert explicit.stdout.strip() == "ok"


@_requires_openssl
def test_generic_python_child_uses_stable_snapshot_after_source_mutation(tmp_path):
    """Ordinary Requests children use merged stable bytes, without a .pth bootstrap."""
    server_dir = tmp_path / "generic-child-server"
    server_dir.mkdir()
    probe = (
        "import sys, requests\n"
        "response = requests.get(sys.argv[1], timeout=5)\n"
        "print(response.text)\n"
    )

    with private_ca_https_server(server_dir) as server:
        rejected = subprocess.run(
            [sys.executable, "-c", probe, server.url],
            env=_clean_env(),
            capture_output=True,
            text=True,
        )
        assert rejected.returncode != 0

        source = Path(server.ca_path)
        original_bytes = source.read_bytes()
        base_env = {**_clean_env(), _EXTRA_CA_ENV_VAR: str(source)}
        child_env = build_child_tls_env(base_env)
        extra_snapshot = Path(child_env[_EXTRA_CA_ENV_VAR])
        requests_snapshot = Path(child_env["REQUESTS_CA_BUNDLE"])

        assert base_env[_EXTRA_CA_ENV_VAR] == str(source)
        assert extra_snapshot.is_absolute() and extra_snapshot != source
        assert requests_snapshot.is_absolute() and requests_snapshot != source
        assert child_env[_NODE_EXTRA_CA_ENV_VAR] == str(extra_snapshot)
        assert child_env[_DERIVED_REQUESTS_CA_MARKER] == str(requests_snapshot)
        assert extra_snapshot.read_bytes() == original_bytes
        assert original_bytes in requests_snapshot.read_bytes()

        source.write_text("replaced after child env construction\n", encoding="ascii")
        accepted = subprocess.run(
            [sys.executable, "-c", probe, server.url],
            env=child_env,
            capture_output=True,
            text=True,
        )

    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.strip() == "ok"
    assert extra_snapshot.read_bytes() == original_bytes


def test_runtime_manager_setup_llm_installs_tls_bootstrap(tmp_path, monkeypatch):
    """Runtime setup must deliver both bootstrap files into the created venv."""
    manager = RuntimeManager()
    manager.runtime_dir = tmp_path / "runtimes"
    monkeypatch.setattr(manager, "get_embedded_script", lambda _name: "")
    monkeypatch.setattr(manager, "get_common_script", lambda: "")

    def _create_llm_venv(_script, _common, _args):
        site_packages = (
            manager.runtime_dir
            / "llm-venv"
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        site_packages.mkdir(parents=True)
        return True

    monkeypatch.setattr(manager, "run_embedded_script", _create_llm_venv)

    assert manager.setup_runtime("llm") is True
    site_packages = _venv_site_packages(manager.runtime_dir / "llm-venv")
    assert site_packages is not None
    assert (site_packages / "_apm_tls_bootstrap.py").is_file()
    assert (site_packages / "_apm_tls.pth").read_text(encoding="ascii") == (
        "import _apm_tls_bootstrap\n"
    )


def test_runtime_manager_bootstrap_warning_is_actionable(tmp_path, capsys):
    """A best-effort delivery failure must tell proxy users how to recover."""
    manager = RuntimeManager()
    manager.runtime_dir = tmp_path / "runtimes"

    manager._install_llm_tls_bootstrap()

    output = capsys.readouterr().out
    assert "PIP_CERT" in output
    assert "Python 3.10+" in output
    assert "https://microsoft.github.io/apm/troubleshooting/ssl-issues/" in output


def test_runtime_manager_bootstrap_exception_is_visible_in_debug_log(tmp_path, monkeypatch, caplog):
    """Unexpected helper failures must remain visible under verbose logging."""
    import apm_cli.runtime.manager as manager_module

    manager = RuntimeManager()
    manager.runtime_dir = tmp_path / "runtimes"

    def _raise(_venv_path):
        raise RuntimeError("unexpected bootstrap failure")

    monkeypatch.setattr(manager_module, "ensure_child_tls_bootstrap", _raise)
    with caplog.at_level(logging.DEBUG, logger="apm_cli.runtime.manager"):
        manager._install_llm_tls_bootstrap()

    assert "unexpected bootstrap failure" in caplog.text
