"""Tests for revision-pin update resolution."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from git.exc import GitCommandError

from apm_cli.deps.git_remote_ops import RemoteRefParseError, parse_ls_remote_output
from apm_cli.deps.revision_pins import (
    RevisionPinResolutionError,
    RevisionPinResolutionResult,
    RevisionPinSkip,
    RevisionPinUpdate,
    apply_revision_pin_updates,
    find_latest_annotated_tag,
    package_name,
    resolve_revision_pin_updates,
)
from apm_cli.models.dependency.reference import DependencyReference
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


def test_latest_revision_pin_tag_breaks_equal_precedence_ties_by_tag_name() -> None:
    refs = [
        RemoteRef("v2.0.0+alpha", GitReferenceType.TAG, "b" * 40, annotated=True),
        RemoteRef("v2.0.0+zeta", GitReferenceType.TAG, "c" * 40, annotated=True),
    ]

    latest = find_latest_annotated_tag(refs, package_name="pkg")

    assert latest.tag == "v2.0.0+zeta"
    assert latest.commit_sha == "c" * 40


def test_revision_pin_package_name_strips_git_suffix() -> None:
    dependency = DependencyReference(repo_url="org/pkg.git")

    assert package_name(dependency) == "pkg"


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
    reason="Windows does not honor POSIX file modes; st_mode & 0o777 is not 0o600",
)
def test_apply_revision_pin_updates_writes_restrictive_permissions(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        f"name: demo\nversion: 1.0.0\ndependencies:\n  apm:\n    - org/pkg#{OLD_SHA}\n",
        encoding="utf-8",
    )

    apply_revision_pin_updates(
        manifest,
        [RevisionPinUpdate("org/pkg", OLD_SHA, NEW_SHA, "v2.0.0", "org/pkg")],
    )

    assert manifest.stat().st_mode & 0o777 == 0o600


def test_resolve_revision_pin_updates_uses_tags_only_fetch() -> None:
    class TagsOnlyDownloader:
        def list_remote_refs(self, _dep_ref: DependencyReference) -> list[RemoteRef]:
            raise AssertionError("revision-pin resolver should not fetch branch heads")

        def list_remote_tag_refs(self, _dep_ref: DependencyReference) -> list[RemoteRef]:
            return [RemoteRef("v2.0.0", GitReferenceType.TAG, NEW_SHA, annotated=True)]

    result = resolve_revision_pin_updates(
        [DependencyReference(repo_url="org/pkg", reference=OLD_SHA)],
        TagsOnlyDownloader(),
    )

    assert result == RevisionPinResolutionResult(
        updates=(RevisionPinUpdate("org/pkg", OLD_SHA, NEW_SHA, "v2.0.0", "org/pkg"),)
    )


def test_resolve_revision_pin_updates_skips_only_missing_annotated_tags() -> None:
    """A missing tag leaves its SHA in place without blocking another pin."""

    class MixedTagDownloader:
        def list_remote_tag_refs(self, dep_ref: DependencyReference) -> list[RemoteRef]:
            if dep_ref.repo_url == "org/no-release":
                return []
            return [RemoteRef("v2.0.0", GitReferenceType.TAG, NEW_SHA, annotated=True)]

    retained = DependencyReference(repo_url="org/no-release", reference=OLD_SHA)
    updated = DependencyReference(repo_url="org/released", reference=OLD_SHA)

    result = resolve_revision_pin_updates(
        [retained, updated],
        MixedTagDownloader(),
        max_workers=1,
    )

    assert result.updates == (
        RevisionPinUpdate("org/released", OLD_SHA, NEW_SHA, "v2.0.0", "org/released"),
    )
    assert result.skips == (RevisionPinSkip("org/no-release", OLD_SHA, "org/no-release"),)


@pytest.mark.parametrize("max_workers", [1, 4])
def test_resolve_revision_pin_updates_propagates_transport_failures(max_workers: int) -> None:
    """Transport failures remain fatal in both serial and concurrent resolution."""

    class FailingDownloader:
        def list_remote_tag_refs(self, _dep_ref: DependencyReference) -> list[RemoteRef]:
            raise GitCommandError("ls-remote", 128, stderr="network down")

    with pytest.raises(GitCommandError, match="network down"):
        resolve_revision_pin_updates(
            [
                DependencyReference(repo_url="org/a", reference=OLD_SHA),
                DependencyReference(repo_url="org/b", reference=OLD_SHA),
            ],
            FailingDownloader(),
            max_workers=max_workers,
        )


def test_resolve_revision_pin_updates_propagates_malformed_remote_output() -> None:
    """Malformed remote output cannot be converted into a retained-pin skip."""

    class MalformedOutputDownloader:
        def list_remote_tag_refs(self, _dep_ref: DependencyReference) -> list[RemoteRef]:
            raise RemoteRefParseError("Malformed git ls-remote tag output.")

    with pytest.raises(
        RevisionPinResolutionError,
        match="Malformed remote tag data for org/pkg",
    ):
        resolve_revision_pin_updates(
            [DependencyReference(repo_url="org/pkg", reference=OLD_SHA)],
            MalformedOutputDownloader(),
        )


def test_resolve_revision_pin_updates_rejects_invalid_remote_sha() -> None:
    class InvalidShaDownloader:
        def list_remote_tag_refs(self, _dep_ref: DependencyReference) -> list[RemoteRef]:
            return [RemoteRef("v2.0.0", GitReferenceType.TAG, "not-a-sha", annotated=True)]

    with pytest.raises(RevisionPinResolutionError, match="invalid SHA"):
        resolve_revision_pin_updates(
            [DependencyReference(repo_url="org/pkg", reference=OLD_SHA)],
            InvalidShaDownloader(),
        )


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
