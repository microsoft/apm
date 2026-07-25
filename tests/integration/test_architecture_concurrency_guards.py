"""Integration guardrails for runtime deadlines and registry concurrency."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


def test_streaming_runtime_is_killed_at_wall_clock_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process holding stdout open must not defer timeout enforcement."""
    from apm_cli.runtime import base

    real_popen = subprocess.Popen
    captured: list[subprocess.Popen] = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        captured.append(process)
        return process

    monkeypatch.setattr(base.subprocess, "Popen", recording_popen)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        base._stream_subprocess_output(
            [
                sys.executable,
                "-c",
                "import sys,time; print('ready', flush=True); time.sleep(30)",
            ],
            timeout=0.2,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 2
    assert len(captured) == 1
    assert captured[0].poll() is not None


def test_concurrent_marketplace_writers_preserve_every_entry(tmp_path: Path) -> None:
    """Independent processes must serialize registry load-modify-save."""
    code = (
        "import sys;"
        "from apm_cli.marketplace.models import MarketplaceSource;"
        "from apm_cli.marketplace.registry import add_marketplace;"
        "name=sys.argv[1];"
        "add_marketplace(MarketplaceSource(name=name,url='https://example.test/'+name+'.git'))"
    )
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    names = [f"catalog-{index}" for index in range(8)]
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, name],
            cwd=Path(__file__).parents[2],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for name in names
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, f"{stdout}\n{stderr}"

    registry_path = tmp_path / ".apm" / "marketplaces.json"
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    recorded = {entry["name"] for entry in data["marketplaces"]}
    assert recorded == set(names)
