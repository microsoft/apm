"""Unit tests for apm_cli.core.tls_trust.configure_tls_trust.

Covers every branch:
- opt-out via APM_DISABLE_TRUSTSTORE
- explicit CA bundle env vars win (REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE)
- SSL_CERT_FILE / SSL_CERT_DIR do NOT suppress injection
- truststore missing -> graceful certifi fallback
- injection failure -> graceful certifi fallback
- happy path -> inject_into_ssl called exactly once
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from apm_cli.core.tls_trust import (
    _BUNDLED_CERT_MARKER,
    _DISABLE_ENV_VAR,
    _EXPLICIT_CA_ENV_VARS,
    build_child_tls_env,
    configure_tls_trust,
    ensure_child_tls_bootstrap,
    has_explicit_ca_override,
)

_NON_REQUESTS_CA_ENV_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR")
_ALL_TRUST_ENV = (_DISABLE_ENV_VAR, *_NON_REQUESTS_CA_ENV_VARS, *_EXPLICIT_CA_ENV_VARS)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start each test from a pristine env (no override / opt-out set)."""
    for var in _ALL_TRUST_ENV:
        monkeypatch.delenv(var, raising=False)


def _install_fake_truststore(monkeypatch, inject=None):
    """Put a fake ``truststore`` module in sys.modules and return its inject mock."""
    calls = {"n": 0}

    def _default_inject():
        calls["n"] += 1

    module = types.ModuleType("truststore")
    module.inject_into_ssl = inject or _default_inject  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "truststore", module)
    return calls


def test_opt_out_disables_injection(monkeypatch):
    calls = _install_fake_truststore(monkeypatch)

    assert configure_tls_trust(env={_DISABLE_ENV_VAR: "1"}) is False
    assert calls["n"] == 0


@pytest.mark.parametrize("var", _EXPLICIT_CA_ENV_VARS)
def test_explicit_ca_bundle_wins(monkeypatch, var):
    calls = _install_fake_truststore(monkeypatch)

    assert has_explicit_ca_override(env={var: "/etc/ssl/certs/custom-ca.pem"}) is True
    assert configure_tls_trust(env={var: "/etc/ssl/certs/custom-ca.pem"}) is False
    assert calls["n"] == 0


@pytest.mark.parametrize("var", _NON_REQUESTS_CA_ENV_VARS)
def test_non_requests_ca_env_does_not_suppress_injection(monkeypatch, var):
    # SSL_CERT_FILE and SSL_CERT_DIR are not requests CA overrides. The frozen
    # runtime hook sets SSL_CERT_FILE to bundled certifi, so these vars must not
    # disable OS-trust injection in the shipped artifact.
    calls = _install_fake_truststore(monkeypatch)
    env = {var: "/etc/ssl/certs/ca-certificates.crt"}

    assert has_explicit_ca_override(env=env) is False
    assert configure_tls_trust(env=env) is True
    assert calls["n"] == 1


def test_missing_truststore_falls_back(monkeypatch):
    # A None entry in sys.modules makes ``import truststore`` raise ImportError.
    monkeypatch.setitem(sys.modules, "truststore", None)

    assert configure_tls_trust() is False


def test_injection_failure_falls_back(monkeypatch):
    def _boom():
        raise RuntimeError("platform trust API unavailable")

    _install_fake_truststore(monkeypatch, inject=_boom)

    assert configure_tls_trust() is False


def test_happy_path_injects_once(monkeypatch):
    calls = _install_fake_truststore(monkeypatch)

    assert configure_tls_trust() is True
    assert calls["n"] == 1


def _repo_root() -> Path:
    current = Path(__file__).resolve().parent
    for parent in (current, *current.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("Cannot locate repository root")


def test_cli_bootstrap_injects_before_requests_import(tmp_path):
    sentinel = tmp_path / "sentinel.txt"
    fake_truststore = tmp_path / "truststore.py"
    fake_truststore.write_text(
        "\n".join(
            [
                "import os",
                "import pathlib",
                "import sys",
                "",
                "def inject_into_ssl():",
                "    pathlib.Path(os.environ['TRUSTSTORE_SENTINEL']).write_text(",
                "        'requests_imported=' + str('requests' in sys.modules),",
                "        encoding='utf-8',",
                "    )",
            ]
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    for name in (
        _DISABLE_ENV_VAR,
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = f"{tmp_path}{os.pathsep}{_repo_root() / 'src'}"
    env["TRUSTSTORE_SENTINEL"] = str(sentinel)

    result = subprocess.run(
        [sys.executable, "-c", "import apm_cli.cli"],
        cwd=_repo_root(),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert sentinel.read_text(encoding="utf-8") == "requests_imported=False"


def test_cli_bootstrap_is_idempotent_across_import_and_main(tmp_path):
    sentinel = tmp_path / "sentinel.txt"
    fake_truststore = tmp_path / "truststore.py"
    fake_truststore.write_text(
        "\n".join(
            [
                "import os",
                "import pathlib",
                "",
                "def inject_into_ssl():",
                "    path = pathlib.Path(os.environ['TRUSTSTORE_SENTINEL'])",
                "    count = int(path.read_text(encoding='utf-8') or '0') if path.exists() else 0",
                "    path.write_text(str(count + 1), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    for name in (
        _DISABLE_ENV_VAR,
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = f"{tmp_path}{os.pathsep}{_repo_root() / 'src'}"
    env["TRUSTSTORE_SENTINEL"] = str(sentinel)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import apm_cli.cli as c\n"
            "def fake_cli(*, obj):\n"
            "    return None\n"
            "c.cli = fake_cli\n"
            "c.main()\n",
        ],
        cwd=_repo_root(),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert sentinel.read_text(encoding="utf-8") == "1"


# ---------------------------------------------------------------------------
# H2 -- visible trust-source diagnostic. Each branch of configure_tls_trust must
# emit an ASCII "[i] TLS: ..." line at DEBUG naming which trust source is in
# effect, so an operator can tell OS-store vs certifi-fallback vs explicit
# bundle vs opt-out from the logs alone.
# ---------------------------------------------------------------------------


def _trust_source_messages(caplog):
    """Rendered log messages emitted by configure_tls_trust that name a trust source."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "apm_cli.core.tls_trust" and "TLS:" in record.getMessage()
    ]


def test_diag_default_inject_names_os_trust_store(monkeypatch, caplog):
    _install_fake_truststore(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="apm_cli.core.tls_trust"):
        assert configure_tls_trust() is True

    messages = _trust_source_messages(caplog)
    assert "[i] TLS: verifying against OS trust store (truststore)" in messages
    for message in messages:
        message.encode("ascii")  # must not raise


def test_diag_disabled_names_opt_out(caplog):
    with caplog.at_level(logging.DEBUG, logger="apm_cli.core.tls_trust"):
        assert configure_tls_trust(env={_DISABLE_ENV_VAR: "1"}) is False

    messages = _trust_source_messages(caplog)
    assert "[i] TLS: OS trust-store injection disabled (APM_DISABLE_TRUSTSTORE)" in messages
    for message in messages:
        message.encode("ascii")


def test_diag_explicit_bundle_names_the_path(caplog):
    ca_path = "/etc/ssl/certs/corp-root.pem"
    with caplog.at_level(logging.DEBUG, logger="apm_cli.core.tls_trust"):
        assert configure_tls_trust(env={"REQUESTS_CA_BUNDLE": ca_path}) is False

    messages = _trust_source_messages(caplog)
    assert f"[i] TLS: explicit CA bundle in use: {ca_path}" in messages
    for message in messages:
        message.encode("ascii")


def test_diag_import_failure_names_certifi_fallback(monkeypatch, caplog):
    # A None entry makes ``import truststore`` raise -> certifi-fallback branch.
    monkeypatch.setitem(sys.modules, "truststore", None)
    with caplog.at_level(logging.DEBUG, logger="apm_cli.core.tls_trust"):
        assert configure_tls_trust() is False

    messages = _trust_source_messages(caplog)
    # The branch appends the captured exception in brackets; match the stable core.
    assert any(
        m.startswith("[i] TLS: verifying against bundled CA (certifi fallback)") for m in messages
    ), messages
    for message in messages:
        message.encode("ascii")


# ---------------------------------------------------------------------------
# T4 -- the internal bundled-default marker must NEVER leak out of
# configure_tls_trust, on ANY of its return branches. A leaked marker would tell
# a child interpreter to pop a SSL_CERT_FILE that is not actually a bundled
# default, silently weakening trust.
# ---------------------------------------------------------------------------


def _marker_env(**extra):
    env = {_BUNDLED_CERT_MARKER: "1"}
    env.update(extra)
    return env


def test_marker_cleared_on_opt_out_branch(monkeypatch):
    _install_fake_truststore(monkeypatch)
    env = _marker_env(**{_DISABLE_ENV_VAR: "1"})
    assert configure_tls_trust(env=env) is False
    assert _BUNDLED_CERT_MARKER not in env


def test_marker_cleared_on_explicit_override_branch(monkeypatch):
    _install_fake_truststore(monkeypatch)
    env = _marker_env(REQUESTS_CA_BUNDLE="/etc/ssl/corp.pem")
    assert configure_tls_trust(env=env) is False
    assert _BUNDLED_CERT_MARKER not in env


def test_marker_cleared_on_truststore_import_failure(monkeypatch):
    monkeypatch.setitem(sys.modules, "truststore", None)
    env = _marker_env(SSL_CERT_FILE="/bundled/certifi.pem")
    assert configure_tls_trust(env=env) is False
    assert _BUNDLED_CERT_MARKER not in env


def test_marker_cleared_on_inject_failure(monkeypatch):
    def _boom():
        raise RuntimeError("platform trust API unavailable")

    _install_fake_truststore(monkeypatch, inject=_boom)
    env = _marker_env(SSL_CERT_FILE="/bundled/certifi.pem")
    assert configure_tls_trust(env=env) is False
    assert _BUNDLED_CERT_MARKER not in env
    # certifi fallback restored (never zero trust).
    assert env.get("SSL_CERT_FILE") == "/bundled/certifi.pem"


def test_marker_cleared_on_inject_success(monkeypatch):
    _install_fake_truststore(monkeypatch)
    env = _marker_env(SSL_CERT_FILE="/bundled/certifi.pem")
    assert configure_tls_trust(env=env) is True
    assert _BUNDLED_CERT_MARKER not in env
    # bundled default popped so the OS store is consulted.
    assert "SSL_CERT_FILE" not in env


# ---------------------------------------------------------------------------
# build_child_tls_env is now an env-hygiene pass: it strips the bundled-default
# marker and does NOT mutate PYTHONPATH (no more sitecustomize shim hijack).
# ---------------------------------------------------------------------------


def test_build_child_tls_env_strips_marker():
    base = {_BUNDLED_CERT_MARKER: "1", "PATH": "/usr/bin", "FOO": "bar"}
    child = build_child_tls_env(base)
    assert _BUNDLED_CERT_MARKER not in child
    assert child["PATH"] == "/usr/bin"
    assert child["FOO"] == "bar"


def test_build_child_tls_env_does_not_touch_pythonpath():
    base = {"PYTHONPATH": "/user/site"}
    child = build_child_tls_env(base)
    # No shim dir prepended -- a user/corporate PYTHONPATH survives untouched.
    assert child["PYTHONPATH"] == "/user/site"


def test_build_child_tls_env_returns_independent_copy():
    base = {"PATH": "/usr/bin"}
    child = build_child_tls_env(base)
    child["PATH"] = "/mutated"
    assert base["PATH"] == "/usr/bin"


# ---------------------------------------------------------------------------
# T7 -- ensure_child_tls_bootstrap drops both delivery artifacts into a venv's
# site-packages so the child interpreter can import the bootstrap.
# ---------------------------------------------------------------------------


def _fake_venv(tmp_path: Path) -> Path:
    """Create a POSIX-style venv skeleton with an empty site-packages dir."""
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    return tmp_path / "venv"


def test_ensure_child_tls_bootstrap_installs_both_files(tmp_path):
    venv = _fake_venv(tmp_path)
    assert ensure_child_tls_bootstrap(venv) is True

    site = venv / "lib" / "python3.12" / "site-packages"
    module = site / "_apm_tls_bootstrap.py"
    pth = site / "_apm_tls.pth"
    assert module.is_file()
    assert pth.is_file()
    # The .pth is exactly the one-line import that triggers the bootstrap.
    assert pth.read_text(encoding="utf-8").strip() == "import _apm_tls_bootstrap"
    # The bootstrap has no apm_cli dependency (self-contained).
    assert "import apm_cli" not in module.read_text(encoding="utf-8")


def test_ensure_child_tls_bootstrap_is_idempotent(tmp_path):
    venv = _fake_venv(tmp_path)
    assert ensure_child_tls_bootstrap(venv) is True
    assert ensure_child_tls_bootstrap(venv) is True


def test_ensure_child_tls_bootstrap_returns_false_for_missing_site_packages(tmp_path):
    # A path with no venv site-packages layout -> best-effort False, no raise.
    assert ensure_child_tls_bootstrap(tmp_path / "does-not-exist") is False
