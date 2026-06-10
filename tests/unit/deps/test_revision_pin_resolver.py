"""Tests for revision-pin update resolution."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.deps.git_remote_ops import parse_ls_remote_output
from apm_cli.deps.revision_pins import (
    RevisionPinResolutionError,
    RevisionPinUpdate,
    apply_revision_pin_updates,
    find_latest_annotated_tag,
)
from apm_cli.models.dependency.types import GitReferenceType, RemoteRef

OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
TAG_OBJECT_SHA = "c" * 40


def test_parse_ls_remote_marks_only_peeled_tags_as_annotated() -> None:
    output = "\n".join(
        [
            f"{TAG_OBJECT_SHA}\trefs/tags/v1.0.0",
            f"{NEW_SHA}\trefs/tags/v1.0.0^{{}}",
            f"{'d' * 40}\trefs/tags/v1.1.0",
            f"{'e' * 40}\trefs/heads/main",
        ]
    )

    refs = parse_ls_remote_output(output)

    annotated = next(ref for ref in refs if ref.name == "v1.0.0")
    lightweight = next(ref for ref in refs if ref.name == "v1.1.0")
    assert annotated.commit_sha == NEW_SHA
    assert annotated.annotated is True
    assert lightweight.annotated is False


def test_latest_revision_pin_tag_ignores_branches_and_lightweight_tags() -> None:
    refs = [
        RemoteRef("v9.9.9", GitReferenceType.BRANCH, "9" * 40),
        RemoteRef("v2.0.0", GitReferenceType.TAG, NEW_SHA, annotated=False),
    ]

    with pytest.raises(RevisionPinResolutionError, match="annotated tag"):
        find_latest_annotated_tag(refs, package_name="pkg")


def test_latest_revision_pin_tag_returns_highest_annotated_semver() -> None:
    refs = [
        RemoteRef("v1.0.0", GitReferenceType.TAG, OLD_SHA, annotated=True),
        RemoteRef("v2.0.0", GitReferenceType.TAG, NEW_SHA, annotated=True),
        RemoteRef("not-a-release", GitReferenceType.TAG, "d" * 40, annotated=True),
    ]

    candidate = find_latest_annotated_tag(refs, package_name="pkg")

    assert candidate.tag == "v2.0.0"
    assert candidate.commit_sha == NEW_SHA


def test_latest_revision_pin_tag_ignores_prereleases_by_default() -> None:
    refs = [
        RemoteRef("v1.5.0", GitReferenceType.TAG, OLD_SHA, annotated=True),
        RemoteRef("v2.0.0-rc.1", GitReferenceType.TAG, NEW_SHA, annotated=True),
    ]

    candidate = find_latest_annotated_tag(refs, package_name="pkg")

    assert candidate.tag == "v1.5.0"
    assert candidate.commit_sha == OLD_SHA


def test_apply_revision_pin_updates_annotates_manifest_atomically(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        f"name: demo\nversion: 1.0.0\ndependencies:\n  apm:\n    - org/pkg#{OLD_SHA} # v1.0.0\n",
        encoding="utf-8",
    )

    apply_revision_pin_updates(
        manifest,
        [RevisionPinUpdate("org/pkg", OLD_SHA, NEW_SHA, "v2.0.0", "org/pkg")],
    )

    assert manifest.read_text(encoding="utf-8").splitlines()[-1] == (
        f"    - org/pkg#{NEW_SHA} # v2.0.0"
    )


def test_apply_revision_pin_updates_keeps_old_pin_when_replace_fails(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    original = f"name: demo\nversion: 1.0.0\ndependencies:\n  apm:\n    - org/pkg#{OLD_SHA}\n"
    manifest.write_text(original, encoding="utf-8")

    with patch("apm_cli.utils.yaml_io.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            apply_revision_pin_updates(
                manifest,
                [RevisionPinUpdate("org/pkg", OLD_SHA, NEW_SHA, "v2.0.0", "org/pkg")],
            )

    assert manifest.read_text(encoding="utf-8") == original
    stale_tmp = manifest.with_name(f".{manifest.name}.apm-update-pins.tmp")
    assert not stale_tmp.exists()


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

    with patch("apm_cli.utils.yaml_io.os.replace", side_effect=capture_replace):
        apply_revision_pin_updates(
            manifest,
            [RevisionPinUpdate("org/pkg", OLD_SHA, NEW_SHA, "v2.0.0", "org/pkg")],
        )

    assert seen_tmp_paths == [str(manifest.with_name(f".{manifest.name}.apm-update-pins.tmp"))]
