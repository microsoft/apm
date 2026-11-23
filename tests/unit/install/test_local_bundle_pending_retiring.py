"""A local bundle's retiring note makes its uninstall record update resumable (#4987)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.install.local_bundle_pending import (
    drop_bundle_exec_type,
    mark_bundle_retiring,
    record_bundle_executables,
    retiring_bundle_slugs,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I7")]

_RECORD = Path("apm_modules") / ".local-bundle-pending.json"


def _bundles(root: Path) -> dict:
    path = root / _RECORD
    return json.loads(path.read_text(encoding="utf-8"))["bundles"] if path.is_file() else {}


def test_a_note_marks_only_a_recorded_type_and_the_drop_clears_it(tmp_path: Path) -> None:
    record_bundle_executables(tmp_path, "pkg", "pkg#1@sha256:aa", ["hooks", "mcp"])
    record_bundle_executables(tmp_path, "parked", "parked#1@sha256:bb", ["hooks"])

    mark_bundle_retiring(tmp_path, "pkg", "hooks")
    mark_bundle_retiring(tmp_path, "pkg", "lsp")

    assert retiring_bundle_slugs(tmp_path, "hooks") == {"pkg"}
    assert _bundles(tmp_path)["pkg"]["retiring"] == ["hooks"]

    drop_bundle_exec_type(tmp_path, "pkg", "hooks")

    assert _bundles(tmp_path) == {
        "parked": {"approval_key": "parked#1@sha256:bb", "exec_types": ["hooks"]},
        "pkg": {"approval_key": "pkg#1@sha256:aa", "exec_types": ["mcp"]},
    }
    assert retiring_bundle_slugs(tmp_path, "hooks") == set()


def test_a_reinstall_replaces_the_note(tmp_path: Path) -> None:
    record_bundle_executables(tmp_path, "pkg", "pkg#1@sha256:aa", ["hooks"])
    mark_bundle_retiring(tmp_path, "pkg", "hooks")

    record_bundle_executables(tmp_path, "pkg", "pkg#1@sha256:aa", ["hooks"])

    assert retiring_bundle_slugs(tmp_path, "hooks") == set()
    assert "retiring" not in _bundles(tmp_path)["pkg"]
