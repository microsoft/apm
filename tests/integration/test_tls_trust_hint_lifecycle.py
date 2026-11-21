"""The TLS failure hint names the trust mode in effect (#2602, #3649).

The loopback GitHub fixture is reached through ``HTTPS_PROXY`` but the child
is NOT told to trust the fixture CA, so package validation fails TLS. The
hint must name the trust mode actually in effect, and the failed positional
install must leave ``apm.yml`` byte-identical with no lockfile.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration._policy_scope_kit import PolicyScopeWorld, file_digest, output, run_step
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.local_github_https_fixture import (
    local_github_https_fixture,
    openssl_executable,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]


def _bogus_bundle(path: Path, real_ca: Path) -> Path:
    """A syntactically valid bundle that does not contain the fixture CA."""
    import certifi

    path.write_bytes(Path(certifi.where()).read_bytes())
    assert real_ca.read_bytes() not in path.read_bytes()
    return path


@pytest.mark.parametrize("mode", ["requests-bundle", "disable-truststore", "default"])
def test_tls_hint_matches_trust_mode_and_install_is_atomic(
    tmp_path: Path, apm_binary_path: Path, mode: str
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg = world.publish_package("pkg-a")
    project = world.consumer()
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=300)
    manifest_before = file_digest(project / "apm.yml")

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        env.pop("REQUESTS_CA_BUNDLE", None)
        env.pop("SSL_CERT_FILE", None)
        if mode == "requests-bundle":
            env["REQUESTS_CA_BUNDLE"] = str(_bogus_bundle(tmp_path / "bogus.pem", fx.ca_path))
        elif mode == "disable-truststore":
            env["APM_DISABLE_TRUSTSTORE"] = "1"
        r = run_step(
            runner,
            ("install", f"{pkg.slug}#v1.0.0", "--no-policy"),
            cwd=project,
            env=env,
            step=f"tls-{mode}",
            expected=1,
        )

    text = " ".join(output(r).split())
    assert file_digest(project / "apm.yml") == manifest_before
    assert not (project / "apm.lock.yaml").exists()
    assert "TLS" in text or "certificate" in text.lower(), text
    if mode == "requests-bundle":
        assert "REQUESTS_CA_BUNDLE" in text, text
        assert "set APM_EXTRA_CA_BUNDLE" not in text, text
    elif mode == "disable-truststore":
        assert "APM_DISABLE_TRUSTSTORE" in text, text
