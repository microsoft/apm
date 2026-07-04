"""Tests for APM's process-wide TLS trust-store bootstrap."""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import Mock, patch

from apm_cli.core.tls import configure_system_trust_store, has_explicit_ca_override


def _repo_root() -> Path:
    current = Path(__file__).resolve().parent
    for parent in [current] + list(current.parents):  # noqa: RUF005
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("Cannot locate repository root")


def test_has_explicit_ca_override_detects_supported_env_vars() -> None:
    assert has_explicit_ca_override({"REQUESTS_CA_BUNDLE": "corp.pem"}) is True
    assert has_explicit_ca_override({"CURL_CA_BUNDLE": "corp.pem"}) is True
    assert has_explicit_ca_override({"SSL_CERT_FILE": "corp.pem"}) is True
    assert has_explicit_ca_override({"SSL_CERT_DIR": "/etc/ssl/certs"}) is True
    assert has_explicit_ca_override({"REQUESTS_CA_BUNDLE": "   "}) is False


def test_configure_system_trust_store_injects_without_override() -> None:
    fake_truststore = types.SimpleNamespace(inject_into_ssl=Mock())

    with patch.dict(sys.modules, {"truststore": fake_truststore}):
        assert configure_system_trust_store(env={}) is True

    fake_truststore.inject_into_ssl.assert_called_once_with()


def test_configure_system_trust_store_respects_explicit_override() -> None:
    fake_truststore = types.SimpleNamespace(inject_into_ssl=Mock())

    with patch.dict(sys.modules, {"truststore": fake_truststore}):
        assert configure_system_trust_store(env={"REQUESTS_CA_BUNDLE": "corp.pem"}) is False

    fake_truststore.inject_into_ssl.assert_not_called()


def test_configure_system_trust_store_graceful_when_unavailable() -> None:
    with patch.dict(sys.modules, {"truststore": None}):
        assert configure_system_trust_store(env={}) is False


def test_cli_import_injects_truststore_before_requests(tmp_path) -> None:
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
    for name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        env.pop(name, None)
    env["TRUSTSTORE_SENTINEL"] = str(sentinel)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(tmp_path),
            str(_repo_root() / "src"),
            env.get("PYTHONPATH", ""),
        ]
    )

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
