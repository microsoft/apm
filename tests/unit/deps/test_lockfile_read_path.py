"""Owner tests for read-only lockfile path resolution (read-only-lockfile-path).

Table-driven at ``apm_cli.deps.lockfile`` (see tests/mutation/floors.json).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import (
    _normalize_exec_status,
    _normalize_lockfile_host_type,
    resolve_lockfile_path_for_read,
    resolve_reproducible_timestamp,
)

pytestmark = pytest.mark.component

_SYMLINKS = pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges")


def test_read_only_prefers_new_lockfile_and_never_migrates(tmp_path: Path) -> None:
    (tmp_path / "apm.lock").write_text("legacy", encoding="utf-8")

    assert resolve_lockfile_path_for_read(tmp_path, read_only=True) == tmp_path / "apm.lock"
    assert (tmp_path / "apm.lock").read_text(encoding="utf-8") == "legacy"
    assert not (tmp_path / "apm.lock.yaml").exists()

    (tmp_path / "apm.lock.yaml").write_text("new", encoding="utf-8")
    assert resolve_lockfile_path_for_read(tmp_path, read_only=True) == tmp_path / "apm.lock.yaml"


def test_read_only_without_any_lockfile_names_the_new_path(tmp_path: Path) -> None:
    assert resolve_lockfile_path_for_read(tmp_path, read_only=True) == tmp_path / "apm.lock.yaml"


@_SYMLINKS
def test_read_only_treats_dangling_symlinks_as_present(tmp_path: Path) -> None:
    (tmp_path / "apm.lock").symlink_to(tmp_path / "missing-legacy")
    assert resolve_lockfile_path_for_read(tmp_path, read_only=True) == tmp_path / "apm.lock"

    (tmp_path / "apm.lock.yaml").symlink_to(tmp_path / "missing-new")
    assert resolve_lockfile_path_for_read(tmp_path, read_only=True) == tmp_path / "apm.lock.yaml"


def test_writable_read_migrates_the_legacy_lockfile(tmp_path: Path) -> None:
    (tmp_path / "apm.lock").write_text("legacy", encoding="utf-8")

    resolved = resolve_lockfile_path_for_read(tmp_path, read_only=False)

    assert resolved == tmp_path / "apm.lock.yaml"
    assert resolved.read_text(encoding="utf-8") == "legacy"
    assert not (tmp_path / "apm.lock").exists()


@pytest.mark.parametrize(
    ("explicit", "epoch", "generated_at", "expected"),
    [
        ("2024-01-01T00:00:00+00:00", "0", "x", "2024-01-01T00:00:00+00:00"),
        (None, "86400", "x", "1970-01-02T00:00:00+00:00"),
        ("", "86400", None, "1970-01-02T00:00:00+00:00"),
        (None, "not-a-number", "2020-05-05", "2020-05-05"),
        (None, "99999999999999999999", None, "1970-01-01T00:00:00+00:00"),
        (None, None, "2020-05-05", "2020-05-05"),
        (None, "", None, "1970-01-01T00:00:00+00:00"),
    ],
)
def test_resolve_reproducible_timestamp_precedence(
    monkeypatch: pytest.MonkeyPatch,
    explicit: str | None,
    epoch: str | None,
    generated_at: str | None,
    expected: str,
) -> None:
    if epoch is None:
        monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    else:
        monkeypatch.setenv("SOURCE_DATE_EPOCH", epoch)

    assert resolve_reproducible_timestamp(explicit, generated_at) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("deployed", "deployed"), ("  denied ", "denied"), ("absent", "absent")],
)
def test_normalize_exec_status_accepts_known_values(raw: object, expected: str | None) -> None:
    assert _normalize_exec_status(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", 3])
def test_normalize_exec_status_rejects_empty_or_non_string(raw: object) -> None:
    with pytest.raises(ValueError) as caught:
        _normalize_exec_status(raw)
    assert str(caught.value) == "lockfile exec_status must be a non-empty string"


def test_normalize_exec_status_names_every_supported_value() -> None:
    with pytest.raises(ValueError) as caught:
        _normalize_exec_status("Deployed")
    assert str(caught.value) == (
        "Unsupported lockfile exec_status: Deployed. Supported values: "
        "absent, denied, deployed, gated_pending_approval"
    )


@pytest.mark.parametrize("raw", ["", "  ", 7])
def test_normalize_host_type_rejects_empty_or_non_string(raw: object) -> None:
    with pytest.raises(ValueError) as caught:
        _normalize_lockfile_host_type(raw)
    assert str(caught.value) == "lockfile host_type must be a non-empty string"


def test_normalize_host_type_lowercases_and_lists_supported_values() -> None:
    from apm_cli.deps.lockfile import _ALLOWED_HOST_TYPES

    assert _normalize_lockfile_host_type(None) is None
    assert _normalize_lockfile_host_type(" GitLab ") == "gitlab"
    with pytest.raises(ValueError) as caught:
        _normalize_lockfile_host_type("Nope")
    assert str(caught.value) == (
        "Unsupported lockfile host_type: Nope. Supported values: "
        + ", ".join(sorted(_ALLOWED_HOST_TYPES))
    )
