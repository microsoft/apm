"""Revision-pin updates written back to apm.yml."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.deps.revision_pins import (
    RevisionPinResolutionError,
    RevisionPinUpdate,
    apply_revision_pin_updates,
)

pytestmark = pytest.mark.component

OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
TAG_OBJECT_SHA = "c" * 40


@pytest.mark.windows_compat
def test_apply_revision_pin_updates_annotates_manifest_atomically_with_lf(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    manifest.write_bytes(
        (
            "name: demo\r\nversion: 1.0.0\r\ndependencies:\r\n"
            f"  apm:\r\n    - org/pkg#{OLD_SHA} # v1.0.0\r\n"
        ).encode()
    )

    apply_revision_pin_updates(
        manifest,
        [RevisionPinUpdate("org/pkg", OLD_SHA, NEW_SHA, "v2.0.0", "org/pkg")],
    )

    rendered = manifest.read_bytes()
    assert b"\r" not in rendered
    assert rendered.splitlines()[-1] == f"    - org/pkg#{NEW_SHA} # v2.0.0".encode()


def test_apply_revision_pin_updates_keeps_old_pin_when_replace_fails(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    original = f"name: demo\nversion: 1.0.0\ndependencies:\n  apm:\n    - org/pkg#{OLD_SHA}\n"
    manifest.write_text(original, encoding="utf-8")

    with patch(
        "apm_cli.utils.atomic_io._replace_atomic_file",
        side_effect=OSError("disk full"),
    ):
        with pytest.raises(OSError, match="disk full"):
            apply_revision_pin_updates(
                manifest,
                [RevisionPinUpdate("org/pkg", OLD_SHA, NEW_SHA, "v2.0.0", "org/pkg")],
            )

    assert manifest.read_text(encoding="utf-8") == original
    assert not list(manifest.parent.glob(f".{manifest.name}.*.apm-update-pins.tmp"))


def test_apply_revision_pin_updates_uses_project_sibling_temp_file(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        f"name: demo\nversion: 1.0.0\ndependencies:\n  apm:\n    - org/pkg#{OLD_SHA}\n",
        encoding="utf-8",
    )
    seen_tmp_paths: list[str] = []
    real_replace = os.replace

    def capture_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        seen_tmp_paths.append(str(src))
        real_replace(src, dst)

    with patch("apm_cli.utils.atomic_io._replace_atomic_file", side_effect=capture_replace):
        apply_revision_pin_updates(
            manifest,
            [RevisionPinUpdate("org/pkg", OLD_SHA, NEW_SHA, "v2.0.0", "org/pkg")],
        )

    assert len(seen_tmp_paths) == 1
    seen_tmp = Path(seen_tmp_paths[0])
    assert seen_tmp.parent == manifest.parent
    assert seen_tmp.name.startswith(f".{manifest.name}.")
    assert seen_tmp.name.endswith(".apm-update-pins.tmp")


def test_apply_revision_pin_updates_rewrites_uppercase_sha(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        f"name: demo\nversion: 1.0.0\ndependencies:\n  apm:\n    - org/pkg#{OLD_SHA.upper()}\n",
        encoding="utf-8",
    )

    apply_revision_pin_updates(
        manifest,
        [RevisionPinUpdate("org/pkg", OLD_SHA, NEW_SHA, "v2.0.0", "org/pkg")],
    )

    assert manifest.read_text(encoding="utf-8").splitlines()[-1] == (
        f"    - org/pkg#{NEW_SHA} # v2.0.0"
    )


def test_apply_revision_pin_updates_rejects_non_manifest_path(tmp_path: Path) -> None:
    not_manifest = tmp_path / "other.yml"
    not_manifest.write_text(
        f"name: demo\nversion: 1.0.0\ndependencies:\n  apm:\n    - org/pkg#{OLD_SHA}\n",
        encoding="utf-8",
    )

    with pytest.raises(RevisionPinResolutionError, match=r"apm\.yml manifest"):
        apply_revision_pin_updates(
            not_manifest,
            [RevisionPinUpdate("org/pkg", OLD_SHA, NEW_SHA, "v2.0.0", "org/pkg")],
        )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows does not honor POSIX file modes reliably",
)
def test_apply_revision_pin_updates_preserves_existing_permissions(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        f"name: demo\nversion: 1.0.0\ndependencies:\n  apm:\n    - org/pkg#{OLD_SHA}\n",
        encoding="utf-8",
    )
    manifest.chmod(0o644)

    apply_revision_pin_updates(
        manifest,
        [RevisionPinUpdate("org/pkg", OLD_SHA, NEW_SHA, "v2.0.0", "org/pkg")],
    )

    assert manifest.stat().st_mode & 0o777 == 0o644


def test_apply_revision_pin_updates_rejects_control_chars_in_tag(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    original = f"name: demo\nversion: 1.0.0\ndependencies:\n  apm:\n    - org/pkg#{OLD_SHA}\n"
    manifest.write_text(original, encoding="utf-8")

    with pytest.raises(RevisionPinResolutionError, match="control character"):
        apply_revision_pin_updates(
            manifest,
            [RevisionPinUpdate("org/pkg", OLD_SHA, NEW_SHA, "v2.0.0\nmalicious: true", "org/pkg")],
        )

    assert manifest.read_text(encoding="utf-8") == original


def test_apply_revision_pin_updates_uses_unique_temp_names(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        f"name: demo\nversion: 1.0.0\ndependencies:\n  apm:\n    - org/pkg#{OLD_SHA}\n",
        encoding="utf-8",
    )
    seen_tmp_paths: list[str] = []
    real_replace = os.replace

    def capture_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        seen_tmp_paths.append(str(src))
        real_replace(src, dst)

    with patch("apm_cli.utils.atomic_io._replace_atomic_file", side_effect=capture_replace):
        apply_revision_pin_updates(
            manifest,
            [RevisionPinUpdate("org/pkg", OLD_SHA, NEW_SHA, "v2.0.0", "org/pkg")],
        )
        apply_revision_pin_updates(
            manifest,
            [RevisionPinUpdate("org/pkg", NEW_SHA, OLD_SHA, "v1.0.0", "org/pkg")],
        )

    assert len(seen_tmp_paths) == 2
    assert seen_tmp_paths[0] != seen_tmp_paths[1]
