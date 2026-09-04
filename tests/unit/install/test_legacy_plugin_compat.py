"""Security boundaries for the cached legacy-plugin compatibility bridge."""

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.errors import DirectDependencyError
from apm_cli.install.legacy_plugin_compat import (
    matches_fresh_legacy_plugin_hash,
    preserve_normalized_marketplace_plugin_type,
    upgrade_cached_legacy_plugin,
)
from apm_cli.models.apm_package import APMPackage, PackageType
from apm_cli.models.validation import validate_legacy_marketplace_plugin
from apm_cli.utils.content_hash import compute_package_hash
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged


def _legacy_cache(root: Path) -> Path:
    package = root / "cached-plugin"
    (package / "skills" / "demo").mkdir(parents=True)
    (package / ".apm" / "skills" / "demo").mkdir(parents=True)
    (package / "apm.yml").write_text("name: cached-plugin\n", encoding="ascii")
    (package / "plugin.json").write_text(
        '{"name": "cached-plugin", "skills": ["./skills/"]}',
        encoding="ascii",
    )
    (package / ".apm" / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo\n---\n",
        encoding="ascii",
    )
    (package / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo\n---\n",
        encoding="ascii",
    )
    return package


def _legacy_lock(package: Path) -> tuple[LockFile, LockedDependency]:
    dependency = LockedDependency(
        repo_url="owner/cached-plugin",
        package_type="marketplace_plugin",
        content_hash=compute_package_hash(package),
    )
    lockfile = LockFile(apm_version="0.28.0")
    lockfile.add_dependency(dependency)
    return lockfile, dependency


def test_normalized_receipt_preserves_locked_marketplace_type(tmp_path: Path) -> None:
    package = _legacy_cache(tmp_path)
    receipt = package / ".apm" / ".plugin-skill-sources.json"
    receipt.write_text('{"declared":true,"sources":{}}', encoding="ascii")
    _lockfile, dependency = _legacy_lock(package)

    result = preserve_normalized_marketplace_plugin_type(
        package,
        dependency,
        PackageType.SKILL_BUNDLE,
    )

    assert result is PackageType.MARKETPLACE_PLUGIN


def test_unreceipted_package_keeps_detected_type(tmp_path: Path) -> None:
    package = _legacy_cache(tmp_path)
    _lockfile, dependency = _legacy_lock(package)

    result = preserve_normalized_marketplace_plugin_type(
        package,
        dependency,
        PackageType.SKILL_BUNDLE,
    )

    assert result is PackageType.SKILL_BUNDLE


@pytest.mark.parametrize(
    ("apm_version", "package_type", "fetched_this_run"),
    [
        ("0.29.0", "marketplace_plugin", False),
        ("0.28.0", "skill_bundle", False),
        pytest.param(
            "0.28.0",
            "apm_package",
            False,
            id="canonical-apm-yml-precedence",
        ),
        ("0.28.0", "marketplace_plugin", True),
    ],
)
def test_upgrade_requires_legacy_lock_provenance(
    tmp_path: Path,
    apm_version: str,
    package_type: str,
    fetched_this_run: bool,
) -> None:
    package = _legacy_cache(tmp_path)
    lockfile, dependency = _legacy_lock(package)
    lockfile.apm_version = apm_version
    dependency.package_type = package_type

    with patch("apm_cli.install.legacy_plugin_compat.gather_detection_evidence") as detect:
        result = upgrade_cached_legacy_plugin(
            package,
            "owner/cached-plugin",
            lockfile=lockfile,
            fetched_this_run=fetched_this_run,
        )

    assert result is None
    detect.assert_not_called()


def test_upgrade_requires_dependency_to_exist_in_legacy_lock(tmp_path: Path) -> None:
    package = _legacy_cache(tmp_path)
    lockfile = LockFile(apm_version="0.28.0")

    with patch("apm_cli.install.legacy_plugin_compat.gather_detection_evidence") as detect:
        result = upgrade_cached_legacy_plugin(
            package,
            "owner/cached-plugin",
            lockfile=lockfile,
            fetched_this_run=False,
        )

    assert result is None
    detect.assert_not_called()


def test_upgrade_rejects_hash_mismatch_before_normalization(tmp_path: Path) -> None:
    package = _legacy_cache(tmp_path)
    lockfile, dependency = _legacy_lock(package)
    dependency.content_hash = "sha256:" + ("0" * 64)

    with (
        patch(
            "apm_cli.install.legacy_plugin_compat.validate_legacy_marketplace_plugin"
        ) as validate,
        pytest.raises(DirectDependencyError, match="content hash mismatch"),
    ):
        upgrade_cached_legacy_plugin(
            package,
            "owner/cached-plugin",
            lockfile=lockfile,
            fetched_this_run=False,
        )

    validate.assert_not_called()


def test_upgrade_rejects_missing_locked_plugin_manifest(tmp_path: Path) -> None:
    package = _legacy_cache(tmp_path)
    (package / "plugin.json").unlink()
    lockfile, dependency = _legacy_lock(package)
    dependency.content_hash = compute_package_hash(package)

    with pytest.raises(DirectDependencyError, match="manifest is missing or unreadable"):
        upgrade_cached_legacy_plugin(
            package,
            "owner/cached-plugin",
            lockfile=lockfile,
            fetched_this_run=False,
        )


@pytest.mark.parametrize(
    ("missing_path", "message"),
    [
        ("apm.yml", "required apm.yml is missing"),
        (".apm", "required .apm directory is missing"),
    ],
)
def test_upgrade_rejects_missing_required_legacy_metadata(
    tmp_path: Path,
    missing_path: str,
    message: str,
) -> None:
    package = _legacy_cache(tmp_path)
    path = package / missing_path
    if path.is_dir():
        for child in sorted(path.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            else:
                child.rmdir()
        path.rmdir()
    else:
        path.unlink()
    lockfile, dependency = _legacy_lock(package)
    dependency.content_hash = compute_package_hash(package)

    with pytest.raises(DirectDependencyError, match=message):
        upgrade_cached_legacy_plugin(
            package,
            "owner/cached-plugin",
            lockfile=lockfile,
            fetched_this_run=False,
        )


def test_upgrade_rejects_missing_hash_before_normalization(tmp_path: Path) -> None:
    package = _legacy_cache(tmp_path)
    lockfile, dependency = _legacy_lock(package)
    dependency.content_hash = None

    with (
        patch(
            "apm_cli.install.legacy_plugin_compat.validate_legacy_marketplace_plugin"
        ) as validate,
        pytest.raises(DirectDependencyError, match="legacy lock entry has no content hash"),
    ):
        upgrade_cached_legacy_plugin(
            package,
            "owner/cached-plugin",
            lockfile=lockfile,
            fetched_this_run=False,
        )

    validate.assert_not_called()


@pytest.mark.parametrize("unsafe_path", ["apm.yml", ".apm"])
def test_upgrade_rejects_symlinked_metadata_without_external_write(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    package = _legacy_cache(tmp_path)
    target = tmp_path / f"external-{unsafe_path.replace('.', 'dot')}"
    source = package / unsafe_path
    if source.is_dir():
        source.rename(target)
        target_is_directory = True
    else:
        target.write_text("external sentinel\n", encoding="ascii")
        source.unlink()
        target_is_directory = False
    try:
        source.symlink_to(target, target_is_directory=target_is_directory)
    except OSError:
        pytest.skip("Symlinks not supported on this platform")
    before = target.read_bytes() if target.is_file() else tuple(target.iterdir())
    lockfile, _dependency = _legacy_lock(package)

    with pytest.raises(DirectDependencyError, match="cache metadata contains a symlink"):
        upgrade_cached_legacy_plugin(
            package,
            "owner/cached-plugin",
            lockfile=lockfile,
            fetched_this_run=False,
        )

    after = target.read_bytes() if target.is_file() else tuple(target.iterdir())
    assert after == before


def test_upgrade_rejects_symlinked_package_root_without_external_write(
    tmp_path: Path,
) -> None:
    package = _legacy_cache(tmp_path)
    external_package = tmp_path / "external-package"
    package.rename(external_package)
    try:
        package.symlink_to(external_package, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks not supported on this platform")
    lockfile, _dependency = _legacy_lock(external_package)
    before = ArtifactSnapshot.capture(external_package)
    link_target = package.readlink()

    with pytest.raises(DirectDependencyError, match="cache metadata contains a symlink"):
        upgrade_cached_legacy_plugin(
            package,
            "owner/cached-plugin",
            lockfile=lockfile,
            fetched_this_run=False,
        )

    assert package.is_symlink()
    assert package.readlink() == link_target
    assert_unchanged(before, ArtifactSnapshot.capture(external_package))


def test_failed_normalization_does_not_partially_mutate_cache(tmp_path: Path) -> None:
    package = _legacy_cache(tmp_path)
    commands = package / "commands"
    commands.mkdir()
    (commands / "demo.md").write_text("# demo\n", encoding="ascii")
    (package / "plugin.json").write_text(
        '{"name":"cached-plugin","skills":["./skills/"],"commands":["./commands/"]}',
        encoding="ascii",
    )
    external_prompts = tmp_path / "external-prompts"
    external_prompts.mkdir()
    (external_prompts / "sentinel.txt").write_text("outside cache\n", encoding="ascii")
    try:
        (package / ".apm" / "prompts").symlink_to(
            external_prompts,
            target_is_directory=True,
        )
    except OSError:
        pytest.skip("Symlinks not supported on this platform")
    lockfile, _dependency = _legacy_lock(package)
    before_cache = ArtifactSnapshot.capture(package)
    before_external = ArtifactSnapshot.capture(external_prompts)

    with pytest.raises(DirectDependencyError):
        upgrade_cached_legacy_plugin(
            package,
            "owner/cached-plugin",
            lockfile=lockfile,
            fetched_this_run=False,
        )

    assert_unchanged(before_cache, ArtifactSnapshot.capture(package))
    assert_unchanged(before_external, ArtifactSnapshot.capture(external_prompts))


def test_normalization_persists_live_plugin_root_paths(tmp_path: Path) -> None:
    package = _legacy_cache(tmp_path)
    (package / "plugin.json").write_text(
        (
            '{"name":"cached-plugin","skills":["./skills/"],'
            '"mcpServers":{"demo":{"command":"${CLAUDE_PLUGIN_ROOT}/server"}}}'
        ),
        encoding="ascii",
    )
    lockfile, dependency = _legacy_lock(package)
    dependency.content_hash = compute_package_hash(package)

    result = upgrade_cached_legacy_plugin(
        package,
        "owner/cached-plugin",
        lockfile=lockfile,
        fetched_this_run=False,
    )

    manifest = (package / "apm.yml").read_text(encoding="utf-8")
    assert result is not None
    assert ".apm-resolution-staging" not in manifest
    assert str(package.resolve()) in manifest


def test_fresh_hash_accepts_only_the_parser_receipt_delta(tmp_path: Path) -> None:
    package = _legacy_cache(tmp_path)
    established = validate_legacy_marketplace_plugin(
        package,
        package / "plugin.json",
        source_path=package,
    )
    assert established.is_valid
    receipt = package / ".apm" / ".plugin-skill-sources.json"
    assert receipt.is_file()
    receipt.unlink()
    lockfile, _dependency = _legacy_lock(package)
    upgrade_cached_legacy_plugin(
        package,
        "owner/cached-plugin",
        lockfile=lockfile,
        fetched_this_run=False,
    )

    assert matches_fresh_legacy_plugin_hash(
        package,
        "owner/cached-plugin",
        lockfile=lockfile,
        package_type=PackageType.MARKETPLACE_PLUGIN,
    )

    (package / "apm.yml").write_text("name: tampered\nversion: 9.9.9\n", encoding="ascii")
    assert not matches_fresh_legacy_plugin_hash(
        package,
        "owner/cached-plugin",
        lockfile=lockfile,
        package_type=PackageType.MARKETPLACE_PLUGIN,
    )


def test_live_normalization_failure_restores_pristine_backup(tmp_path: Path) -> None:
    package = _legacy_cache(tmp_path)
    lockfile, _dependency = _legacy_lock(package)
    before = ArtifactSnapshot.capture(package)

    def fail_only_at_live_path(
        candidate: Path,
        plugin_json: Path,
        *,
        source_path: Path,
    ):
        if candidate == package:
            (candidate / "apm.yml").write_text("partial mutation\n", encoding="ascii")
            raise OSError("live normalization failed")
        return validate_legacy_marketplace_plugin(
            candidate,
            plugin_json,
            source_path=source_path,
        )

    with (
        patch(
            "apm_cli.install.legacy_plugin_compat.validate_legacy_marketplace_plugin",
            side_effect=fail_only_at_live_path,
        ),
        pytest.raises(OSError, match="live normalization failed"),
    ):
        upgrade_cached_legacy_plugin(
            package,
            "owner/cached-plugin",
            lockfile=lockfile,
            fetched_this_run=False,
        )

    assert_unchanged(before, ArtifactSnapshot.capture(package))
    assert not (package.parent / ".apm-resolution-staging").exists()


def test_backup_copy_failure_cleans_staging_without_touching_cache(tmp_path: Path) -> None:
    package = _legacy_cache(tmp_path)
    lockfile, _dependency = _legacy_lock(package)
    before = ArtifactSnapshot.capture(package)
    real_copytree = shutil.copytree
    copy_count = 0

    def fail_second_copy(source, destination, *args, **kwargs):
        nonlocal copy_count
        if Path(source) == package:
            copy_count += 1
            if copy_count == 2:
                raise OSError("backup copy failed")
        return real_copytree(source, destination, *args, **kwargs)

    with (
        patch(
            "apm_cli.install.legacy_plugin_compat.shutil.copytree",
            side_effect=fail_second_copy,
        ),
        pytest.raises(OSError, match="backup copy failed"),
    ):
        upgrade_cached_legacy_plugin(
            package,
            "owner/cached-plugin",
            lockfile=lockfile,
            fetched_this_run=False,
        )

    assert_unchanged(before, ArtifactSnapshot.capture(package))
    assert not (package.parent / ".apm-resolution-staging").exists()


def test_verified_legacy_cache_routes_through_canonical_validator(tmp_path: Path) -> None:
    package = _legacy_cache(tmp_path)
    lockfile, _dependency = _legacy_lock(package)

    with patch(
        "apm_cli.install.legacy_plugin_compat.validate_legacy_marketplace_plugin",
        wraps=validate_legacy_marketplace_plugin,
    ) as validate:
        result = upgrade_cached_legacy_plugin(
            package,
            "owner/cached-plugin",
            lockfile=lockfile,
            fetched_this_run=False,
        )

    assert isinstance(result, APMPackage)
    assert result.package_path == package
    assert validate.call_count == 2
    assert validate.call_args_list[-1].args == (package, package / "plugin.json")
    assert validate.call_args_list[-1].kwargs == {"source_path": package}
