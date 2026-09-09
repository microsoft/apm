"""Real CLI installs over private-CA HTTPS with independent default-root proof.

Only the test process's certifi default is seeded with a synthetic root. The
CLI, truststore, Requests, registry downloader, extraction, and integration are
real. The isolated environment permits only the two loopback servers.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

from .test_tls_custom_ca import _OPENSSL_EXECUTABLE, _TRUST_ENV_VARS, private_ca_https_server

pytestmark = [
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.skipif(_OPENSSL_EXECUTABLE is None, reason="openssl CLI not available"),
]

_GUIDE = "---\napplyTo: '**'\ndescription: TLS install fixture\n---\n# Corporate package\n"


def test_apm_install_trusts_private_ca_and_retains_default_root(tmp_path, apm_engine_command):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(
            "apm.yml",
            "name: testrepo\nversion: 1.0.0\ndescription: TLS install fixture\n",
        )
        package.writestr(".apm/instructions/guide.instructions.md", _GUIDE)
    archive_bytes = archive.getvalue()
    versions = json.dumps(
        {
            "versions": [
                {
                    "version": "1.0.0",
                    "digest": hashlib.sha256(archive_bytes).hexdigest(),
                    "published_at": "2026-01-01T00:00:00Z",
                }
            ]
        }
    ).encode()
    requests_seen = []

    class ArchiveHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/v1/packages/fixture/testrepo/versions":
                body, content_type = versions, "application/json"
            elif path == "/v1/packages/fixture/testrepo/versions/1.0.0/download":
                body, content_type = archive_bytes, "application/zip"
            else:
                self.send_error(404)
                return
            requests_seen.append(self.server.server_port)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with (
        private_ca_https_server(
            tmp_path / "baseline", "Existing Root", handler=ArchiveHandler
        ) as baseline,
        private_ca_https_server(
            tmp_path / "extra", "Corporate Root", handler=ArchiveHandler
        ) as extra,
    ):
        base_env = {key: value for key, value in os.environ.items() if key not in _TRUST_ENV_VARS}
        isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=base_env)
        env = isolated.subprocess_env(
            overrides={
                "APM_E2E_TESTS": "1",
                "APM_TEST_DEFAULT_CA": baseline.ca_path,
            }
        )
        env["APM_TEST_LOOPBACK_PORTS"] = f"{baseline.port},{extra.port}"
        guard = Path(env["PYTHONPATH"]) / "sitecustomize.py"
        # Model an existing default root without changing the machine trust
        # store or supplying a Requests replacement override to APM.
        with guard.open("a", encoding="utf-8") as stream:
            stream.write(
                "\nimport certifi\ncertifi.where = lambda: os.environ['APM_TEST_DEFAULT_CA']\n"
            )
        runner = ApmLifecycleRunner(apm_engine_command, timeout_seconds=45)
        enabled = runner.run(
            ("experimental", "enable", "registries"), cwd=isolated.work_root, env=env
        )
        assert enabled.returncode == 0, enabled.stdout + enabled.stderr
        cases = (
            ("baseline-control", baseline, {}, True),
            ("private-untrusted", extra, {}, False),
            ("private-additive", extra, {"APM_EXTRA_CA_BUNDLE": extra.ca_path}, True),
            ("baseline-retained", baseline, {"APM_EXTRA_CA_BUNDLE": extra.ca_path}, True),
            ("replacement-control", baseline, {"REQUESTS_CA_BUNDLE": extra.ca_path}, False),
        )
        for name, server, trust_env, succeeds in cases:
            project = isolated.work_root / name
            project.mkdir()
            dump_yaml(
                {
                    "name": "tls-consumer",
                    "version": "1.0.0",
                    "description": "TLS acceptance fixture",
                    "registries": {
                        "fixture": {"url": f"https://127.0.0.1:{server.port}"},
                        "default": "fixture",
                    },
                    "dependencies": {"apm": ["fixture/testrepo#1.0.0"]},
                },
                project / "apm.yml",
            )
            child_env = {
                **env,
                **trust_env,
            }
            count_before = len(requests_seen)
            result = runner.run(
                ("install", "--target", "copilot"),
                scenario_id=name,
                cwd=project,
                env=child_env,
            )
            diagnostics = result.stdout + result.stderr
            installed = (
                project / "apm_modules/fixture/testrepo/.apm/instructions/guide.instructions.md"
            )
            if succeeds:
                assert result.returncode == 0, diagnostics
                assert installed.read_text(encoding="utf-8") == _GUIDE
                assert (project / "apm.lock.yaml").is_file()
                assert requests_seen[count_before:] == [server.port, server.port]
                assert any(
                    path.read_text(encoding="utf-8").endswith("# Corporate package\n")
                    for path in (project / ".github/instructions").glob("*.md")
                )
            else:
                assert result.returncode != 0, diagnostics
                assert "SSLCertVerificationError" in diagnostics, diagnostics
                assert not installed.exists()
                assert len(requests_seen) == count_before
