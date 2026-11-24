"""The perf bench never records its real-network token in uploaded artifacts.

The CI job hands the harness a read-only token for the authenticated
real-network cells. A failing setup or timed run records an error tail in the
journey record, which is uploaded, so the token (raw, and as git's Basic
credential) must be redacted from it.
"""

from __future__ import annotations

import argparse
import base64
from types import SimpleNamespace

import pytest

from tests.benchmarks import journeys

pytestmark = [pytest.mark.unit]

_TOKEN = "ghs_" + "S3cr3tBenchT0ken" * 2


@pytest.fixture
def bench_token(monkeypatch):
    monkeypatch.setattr(journeys, "_BENCH_REAL_TOKEN", _TOKEN)
    return _TOKEN


def test_redact_removes_the_raw_and_basic_credential_forms(bench_token):
    basic = f"x-access-token:{bench_token}"
    text = f"a {bench_token} b {basic} c {base64.b64encode(basic.encode()).decode()} d"

    redacted = journeys._redact(text)

    assert bench_token not in redacted
    assert base64.b64encode(basic.encode()).decode() not in redacted
    assert redacted.startswith("a *** b ")


def test_a_setup_failure_records_no_token(bench_token):
    def failing_setup(_ctx):
        raise RuntimeError(f"git clone failed: Authorization: token {bench_token}")

    journey = journeys.Journey("install-warm-auth", failing_setup, lambda _ctx: [])
    ctx = SimpleNamespace(fixture=SimpleNamespace(name="real"), world=SimpleNamespace(rtt_ms=0.0))
    args = argparse.Namespace(runs=1, max_load=99.0, load_wait_min=0, profile=False)

    record = journeys._measure(ctx, journey, args)

    assert record["error"]
    assert bench_token not in record["error"]
