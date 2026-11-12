"""Tests for ``stamp_plugin_version`` helper.

Extracted from the duplicate inline blocks that used to live in
``download_package`` and ``download_subdirectory_package``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apm_cli.deps.package_validator import stamp_plugin_version
from apm_cli.models.validation import PackageType


def _pkg(version="0.0.0"):
    return SimpleNamespace(version=version)


def test_stamps_short_sha_when_marketplace_plugin_and_zero_version(tmp_path):
    pkg = _pkg("0.0.0")
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text("name: foo\nversion: 0.0.0\n", encoding="utf-8")

    stamp_plugin_version(
        pkg,
        PackageType.MARKETPLACE_PLUGIN,
        "abcdef1234567890aabbccddeeff00112233abcd",
        tmp_path,
    )

    assert pkg.version == "abcdef1"
    assert "version: abcdef1" in apm_yml.read_text(encoding="utf-8")


def test_no_op_when_package_type_is_not_marketplace_plugin(tmp_path):
    pkg = _pkg("0.0.0")
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text("name: foo\nversion: 0.0.0\n", encoding="utf-8")

    stamp_plugin_version(
        pkg,
        PackageType.APM_PACKAGE,
        "abcdef1234567890aabbccddeeff00112233abcd",
        tmp_path,
    )

    assert pkg.version == "0.0.0"
    assert "version: 0.0.0" in apm_yml.read_text(encoding="utf-8")


def test_no_op_when_version_is_already_set(tmp_path):
    pkg = _pkg("1.2.3")
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text("name: foo\nversion: 1.2.3\n", encoding="utf-8")

    stamp_plugin_version(
        pkg,
        PackageType.MARKETPLACE_PLUGIN,
        "abcdef1234567890aabbccddeeff00112233abcd",
        tmp_path,
    )

    assert pkg.version == "1.2.3"


@pytest.mark.parametrize("commit", ["", None, "unknown"])
def test_no_op_when_commit_is_unusable(tmp_path, commit):
    pkg = _pkg("0.0.0")
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text("name: foo\nversion: 0.0.0\n", encoding="utf-8")

    stamp_plugin_version(pkg, PackageType.MARKETPLACE_PLUGIN, commit, tmp_path)

    assert pkg.version == "0.0.0"


def test_no_op_when_apm_yml_is_missing(tmp_path):
    pkg = _pkg("0.0.0")
    # The in-memory package version is still updated for the lockfile.
    stamp_plugin_version(
        pkg,
        PackageType.MARKETPLACE_PLUGIN,
        "abcdef1234567890aabbccddeeff00112233abcd",
        tmp_path,
    )
    assert pkg.version == "abcdef1"
    assert not (tmp_path / "apm.yml").exists()


@pytest.mark.windows_compat
def test_stamped_manifest_bytes_are_lf_only(tmp_path):
    """The stamped apm.yml lands inside a package tree hashed raw by
    ``compute_package_hash``, so its bytes must be LF-only on every OS
    (apm#2619) -- a platform-native CRLF rewrite on Windows made the
    lockfile ``content_hash`` diverge from POSIX."""
    pkg = _pkg("0.0.0")
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_bytes(b"name: foo\nversion: 0.0.0\n")

    stamp_plugin_version(
        pkg,
        PackageType.MARKETPLACE_PLUGIN,
        "abcdef1234567890aabbccddeeff00112233abcd",
        tmp_path,
    )

    raw = apm_yml.read_bytes()
    assert b"version: abcdef1" in raw
    assert b"\r" not in raw
    assert raw.endswith(b"\n")


def test_no_op_when_package_is_none(tmp_path):
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text("name: foo\nversion: 0.0.0\n", encoding="utf-8")

    # Should not raise.
    stamp_plugin_version(
        None,
        PackageType.MARKETPLACE_PLUGIN,
        "abcdef1234567890aabbccddeeff00112233abcd",
        tmp_path,
    )

    assert "version: 0.0.0" in apm_yml.read_text(encoding="utf-8")


@pytest.mark.windows_compat
def test_stamp_invalidates_from_apm_yml_cache(tmp_path):
    """Stamping rewrites apm.yml, so cached from_apm_yml instances for that
    path must be dropped -- a stale pre-stamp instance elsewhere in the run
    would disagree with the on-disk bytes (apm#2619 migration fallout)."""
    from apm_cli.models.apm_package import APMPackage

    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_bytes(b"name: foo\nversion: 0.0.0\n")
    first = APMPackage.from_apm_yml(apm_yml)  # populate the cache
    assert first.version == "0.0.0"

    stamp_plugin_version(
        _pkg("0.0.0"),
        PackageType.MARKETPLACE_PLUGIN,
        "abcdef1234567890aabbccddeeff00112233abcd",
        tmp_path,
    )

    reloaded = APMPackage.from_apm_yml(apm_yml)
    assert reloaded is not first
    assert reloaded.version == "abcdef1"


@pytest.mark.windows_compat
def test_restamp_after_resynthesis_is_not_skipped_by_stale_cache(tmp_path):
    """The apm#2619 double-download interplay, distilled.

    Real flow: download #1 synthesizes apm.yml (0.0.0), stamps it to the
    short SHA; a content-hash mismatch forces a re-download in the SAME
    process; download #2 re-synthesizes a fresh 0.0.0 manifest. Without
    cache invalidation, from_apm_yml returned the STALE stamped instance,
    the ``version == "0.0.0"`` stamp guard skipped, and the tree stayed
    unstamped on disk -- hashing to a value no lockfile ever recorded.
    """
    from apm_cli.deps.plugin_parser import synthesize_apm_yml_from_plugin
    from apm_cli.models.apm_package import APMPackage

    sha = "2c7ec5e78b8e5d43ea02e90bb8826f6b9f147b0c"
    plugin = tmp_path / "plug"
    skill = plugin / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(b"---\nname: demo\ndescription: Demo\n---\n\n# Demo\n")

    # Download #1: synthesize + stamp.
    apm_yml = synthesize_apm_yml_from_plugin(plugin, {"name": "plug"})
    pkg = APMPackage.from_apm_yml(apm_yml)
    stamp_plugin_version(pkg, PackageType.MARKETPLACE_PLUGIN, sha, plugin)
    assert b"version: 2c7ec5e" in apm_yml.read_bytes()

    # Re-download: pristine tree, fresh synthesis (no existing manifest).
    apm_yml.unlink()
    synthesize_apm_yml_from_plugin(plugin, {"name": "plug"})

    fresh = APMPackage.from_apm_yml(apm_yml)
    assert fresh.version == "0.0.0"  # stale cache would still say 2c7ec5e

    # The second stamp must therefore actually run.
    stamp_plugin_version(fresh, PackageType.MARKETPLACE_PLUGIN, sha, plugin)
    assert fresh.version == "2c7ec5e"
    assert b"version: 2c7ec5e" in apm_yml.read_bytes()


def test_cache_invalidation_is_safe_under_concurrent_inserts(tmp_path):
    """apm#2619 round-3: invalidate_apm_yml_cache_entry iterates the shared
    manifest cache while parallel download/resolver worker threads insert
    into it. Without the module lock this raised 'RuntimeError: dictionary
    changed size during iteration' and failed installs intermittently."""
    import threading

    from apm_cli.models.apm_package import (
        APMPackage,
        clear_apm_yml_cache,
        invalidate_apm_yml_cache_entry,
    )

    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_bytes(b"name: foo\nversion: 0.0.0\n")

    stop = threading.Event()
    errors: list[BaseException] = []

    def inserter() -> None:
        # Distinct source_path anchors create distinct cache keys, growing
        # the dict while the main thread iterates it.
        i = 0
        while not stop.is_set():
            i += 1
            anchor = tmp_path / f"anchor-{i}"
            anchor.mkdir(exist_ok=True)
            try:
                APMPackage.from_apm_yml(apm_yml, source_path=anchor)
            except BaseException as exc:  # pragma: no cover - failure capture
                errors.append(exc)
                return

    thread = threading.Thread(target=inserter, daemon=True)
    thread.start()
    try:
        for _ in range(300):
            invalidate_apm_yml_cache_entry(apm_yml)
    except BaseException as exc:  # pragma: no cover - failure capture
        errors.append(exc)
    finally:
        stop.set()
        thread.join(timeout=10)
        # Drop the many unique-keyed entries this test added, even when an
        # assertion below fails.
        clear_apm_yml_cache()

    assert not thread.is_alive(), "inserter thread wedged"
    assert not errors, errors


def test_in_flight_parse_cannot_repoison_cache_after_invalidation(tmp_path):
    """Round-4 review: invalidation is a barrier for in-flight parses.

    A thread that read PRE-rewrite bytes must not insert its stale result
    into the cache AFTER a rewrite's invalidation ran -- otherwise the
    exact staleness the invalidation exists to fix comes back, dependent
    on thread interleaving. from_apm_yml snapshots a generation counter
    before reading the file and skips the insert if it moved.
    """
    from unittest.mock import patch

    from apm_cli.models.apm_package import (
        APMPackage,
        invalidate_apm_yml_cache_entry,
    )

    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_bytes(b"name: foo\nversion: 0.0.0\n")

    import apm_cli.utils.yaml_io as yaml_io_mod

    real_load = yaml_io_mod.load_yaml

    def load_with_concurrent_rewrite(path):
        data = real_load(path)
        # Simulate a concurrent rewrite + invalidation landing while this
        # "thread" is still holding the pre-rewrite parse result.
        apm_yml.write_bytes(b"name: foo\nversion: 2c7ec5e\n")
        invalidate_apm_yml_cache_entry(apm_yml)
        return data

    with patch(
        "apm_cli.utils.yaml_io.load_yaml",
        side_effect=load_with_concurrent_rewrite,
    ):
        stale = APMPackage.from_apm_yml(apm_yml)
    assert stale.version == "0.0.0"  # the in-flight parse itself is fine

    # The stale result must NOT have been cached: a fresh load sees the
    # rewritten bytes.
    fresh = APMPackage.from_apm_yml(apm_yml)
    assert fresh.version == "2c7ec5e"


def _write_marketplace_plugin(root):
    """A Claude marketplace plugin without its own apm.yml (no version)."""
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_bytes(
        b'{"name": "plug", "description": "Demo plugin"}\n'
    )
    skill = root / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(b"---\nname: demo\ndescription: Demo\n---\n\n# Demo\n")


@pytest.mark.windows_compat
def test_same_process_redownload_hash_matches_lockfile_and_fresh_install(tmp_path):
    """#3585: a same-process re-download (e.g. ``apm install --update``
    re-acquiring a package) must land a stamped tree whose content hash
    equals the hash recorded by the first download AND the hash a later
    fresh process computes, so the lockfile integrity check passes.

    Drives the real materialization consumers: ``validate_apm_package``
    synthesizes apm.yml (0.0.0) and loads it via ``from_apm_yml``, then
    ``stamp_plugin_version`` rewrites it with the short SHA. Without
    cache invalidation the second load returned the stale stamped
    instance, the stamp guard skipped, and the tree hashed unstamped.
    """
    import shutil

    from apm_cli.models.apm_package import clear_apm_yml_cache
    from apm_cli.models.validation import validate_apm_package
    from apm_cli.utils.content_hash import compute_package_hash

    sha = "2c7ec5e78b8e5d43ea02e90bb8826f6b9f147b0c"
    pristine = tmp_path / "pristine"
    _write_marketplace_plugin(pristine)
    target = tmp_path / "apm_modules" / "plug"

    def materialize() -> str:
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(pristine, target)
        result = validate_apm_package(target)
        assert result.is_valid, result.errors
        assert result.package_type == PackageType.MARKETPLACE_PLUGIN
        stamp_plugin_version(result.package, result.package_type, sha, target)
        assert b"version: 2c7ec5e" in (target / "apm.yml").read_bytes()
        return compute_package_hash(target)

    recorded = materialize()  # first download: hash written to apm.lock.yaml
    redownloaded = materialize()  # same-process re-acquisition
    clear_apm_yml_cache()  # a later, fresh install process
    fresh = materialize()

    assert redownloaded == recorded
    assert fresh == recorded
