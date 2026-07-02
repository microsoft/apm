"""End-to-end: a malicious script FILE drives SSRF dispatch.

``d3-ssrf-e2e``. Confirms the SSRF reaches ``requests.post`` through the
real parse path (``parse_script_file`` -> ``ScriptEntry`` ->
``LifecycleScriptRunner.fire`` -> ``_execute_http``), not just a
hand-built entry. The fixture JSON mirrors what an admin-policy /
user-tier file can declare today.

SECURE expectation: an SSRF guard refuses every internal destination, so
``fire`` dispatches none of them. HEAD has no guard, so all are
dispatched -> this test FAILS on head (genuine break).

Hermetic: ``requests.post`` patched to record captured URLs.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from apm_cli.core.lifecycle_scripts import LifecycleScriptRunner, parse_script_file

from .conftest import make_event

FIXTURES = Path(__file__).parent / "fixtures"


def _record_post(captured: list[str]):
    def _fake_post(url: str, *_a, **_k) -> MagicMock:
        captured.append(url)
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        return resp

    return _fake_post


@pytest.mark.parametrize(
    "fixture_name",
    ["ssrf_metadata_scripts.json", "ssrf_internal_batch_scripts.json"],
)
def test_fixture_ssrf_not_dispatched(fixture_name: str) -> None:
    scripts = parse_script_file(FIXTURES / fixture_name, source="user")
    assert scripts, "fixture must parse into at least one http entry"

    runner = LifecycleScriptRunner(scripts=scripts)
    captured: list[str] = []

    with patch("requests.post", side_effect=_record_post(captured)):
        threads = runner.fire("post-install", make_event())
        for t in threads:
            if isinstance(t, threading.Thread):
                t.join(timeout=5)

    dispatched_hosts = {urlparse(u).hostname for u in captured}
    # SECURE expectation: nothing internal is dispatched.
    assert dispatched_hosts == set(), (
        f"SSRF: fixture {fixture_name} dispatched to internal hosts "
        f"{sorted(h for h in dispatched_hosts if h)}"
    )


def test_metadata_fixture_targets_link_local_host() -> None:
    """Sanity: the fixture really does encode the IMDS link-local target.

    Parses the configured URLs (never substring) so the parametrized
    SSRF assertion above is anchored to a real attack payload.
    """
    scripts = parse_script_file(FIXTURES / "ssrf_metadata_scripts.json", source="user")
    hosts = {urlparse(s.url).hostname for s in scripts}
    assert hosts == {"169.254.169.254", "metadata.google.internal"}
