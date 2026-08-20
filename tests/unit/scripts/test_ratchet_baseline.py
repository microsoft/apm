from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

# Entire module: proves the ratchet baseline writer stays LF-only and
# ASCII-safe under Windows text-mode newline translation
# (microsoft/apm#2233). Selected by the PR-time Windows Compatibility
# Gate via `pytest -m windows_compat`; also runs on every other OS.
pytestmark = pytest.mark.windows_compat

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "ratchet_baseline.py"

spec = importlib.util.spec_from_file_location("ratchet_baseline", SCRIPT)
assert spec is not None
ratchet_baseline = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(ratchet_baseline)


def test_write_baseline_uses_lf_bytes_when_text_mode_translates_newlines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write_text = Path.write_text

    def windows_write_text(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if newline is None:
            return path.write_bytes(
                data.replace("\n", "\r\n").encode(encoding or "utf-8", errors or "strict")
            )
        return original_write_text(
            path,
            data,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "write_text", windows_write_text)
    baseline = tmp_path / "baseline.json"
    payload = {"z": [2, 1], "a": {"nested": True}}
    expected = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")

    ratchet_baseline.write_baseline(baseline, payload, label="test")

    content = baseline.read_bytes()
    assert content == expected
    assert b"\r\n" not in content
