"""Install-side guards for carried licence copies (#3556)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.deps.carried_license import CarriedLicense, carry_licenses
from apm_cli.install.errors import DirectDependencyError
from apm_cli.install.phases._redownload import _should_skip_redownload
from apm_cli.install.source_helpers import (
    ensure_carried_licenses_match,
    locked_commit_unchanged,
    record_carried_package_hash,
    update_accepts_new_content,
)
from apm_cli.utils.content_hash import compute_package_hash

pytestmark = pytest.mark.component

MIT = b"MIT License\n"


def _package(tmp_path: Path) -> tuple[Path, str, tuple[CarriedLicense, ...]]:
    """A subdirectory package with its repository licence carried in."""
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "LICENSE").write_bytes(MIT)
    (repo / "sub" / "SKILL.md").write_text("# s\n", encoding="utf-8")
    package = tmp_path / "apm_modules" / "sub"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("# s\n", encoding="utf-8")
    legacy_hash = compute_package_hash(package)
    records = carry_licenses(package, repository_root=repo, package_subpath="sub")
    assert records
    return package, legacy_hash, records


def _ctx(tmp_path: Path, **overrides) -> SimpleNamespace:
    values = {
        "package_carried_licenses": {},
        "package_hashes": {},
        "package_framed_hashes": {},
        "update_refs": False,
        "expected_hash_change_deps": set(),
        "apm_modules_dir": tmp_path / "apm_modules",
        "scope": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_fresh_download_hash_excludes_carried_copies(tmp_path: Path) -> None:
    package, legacy_hash, records = _package(tmp_path)
    ctx = _ctx(tmp_path)

    excluded = record_carried_package_hash(
        ctx, "k", package, SimpleNamespace(carried_licenses=records)
    )

    assert excluded == frozenset({"LICENSE"})
    assert ctx.package_hashes["k"] == legacy_hash
    assert ctx.package_carried_licenses["k"] == records


def test_cached_package_reuses_intact_lock_records(tmp_path: Path) -> None:
    package, legacy_hash, records = _package(tmp_path)
    ctx = _ctx(tmp_path)

    record_carried_package_hash(
        ctx, "k", package, locked_dep=SimpleNamespace(carried_licenses=records)
    )

    assert ctx.package_carried_licenses["k"] == records
    assert ctx.package_hashes["k"] == legacy_hash


def test_cached_package_drops_lock_records_for_tampered_copies(tmp_path: Path) -> None:
    package, legacy_hash, records = _package(tmp_path)
    (package / "LICENSE").write_bytes(b"edited\n")
    ctx = _ctx(tmp_path)

    record_carried_package_hash(
        ctx, "k", package, locked_dep=SimpleNamespace(carried_licenses=records)
    )

    assert ctx.package_carried_licenses["k"] == ()
    assert ctx.package_hashes["k"] != legacy_hash


def test_carried_mismatch_with_lock_fails_closed(tmp_path: Path) -> None:
    package, _legacy_hash, records = _package(tmp_path)
    forged = (CarriedLicense("LICENSE", "LICENSE", "sha256:" + "0" * 64),)
    ctx = _ctx(tmp_path, package_carried_licenses={"k": records})

    with pytest.raises(DirectDependencyError, match="Licence file mismatch") as exc_info:
        ensure_carried_licenses_match(ctx, "k", package, SimpleNamespace(carried_licenses=forged))
    assert "remove the 'k' entry from" in str(exc_info.value)
    assert not package.exists()


@pytest.mark.parametrize(
    ("scope", "command"),
    [(InstallScope.PROJECT, "apm install"), (InstallScope.USER, "apm install -g")],
)
def test_carried_mismatch_hint_names_the_deliberate_re_pin(tmp_path: Path, scope, command) -> None:
    package, _legacy_hash, records = _package(tmp_path)
    forged = (CarriedLicense("LICENSE", "LICENSE", "sha256:" + "0" * 64),)
    ctx = _ctx(
        tmp_path,
        package_carried_licenses={"owner/repo/sub": records},
        scope=scope,
        apm_dir=tmp_path / "apm-root",
    )

    with pytest.raises(DirectDependencyError) as exc_info:
        ensure_carried_licenses_match(
            ctx, "owner/repo/sub", package, SimpleNamespace(carried_licenses=forged)
        )
    message = " ".join(str(exc_info.value).split())
    lockfile = tmp_path / "apm-root" / "apm.lock.yaml"
    assert f"remove the 'owner/repo/sub' entry from {lockfile}, then run '{command}'" in message


def test_update_keeps_the_carried_check_for_an_unchanged_commit(tmp_path: Path) -> None:
    package, _legacy_hash, records = _package(tmp_path)
    forged = (CarriedLicense("LICENSE", "LICENSE", "sha256:" + "0" * 64),)
    ctx = _ctx(tmp_path, package_carried_licenses={"k": records}, update_refs=True)
    locked = SimpleNamespace(carried_licenses=forged, resolved_commit="a" * 40)

    with pytest.raises(DirectDependencyError, match="Licence file mismatch"):
        ensure_carried_licenses_match(ctx, "k", package, locked, "a" * 40)


@pytest.mark.parametrize(
    ("locked", "overrides"),
    [
        ((), {}),
        ("forged", {"update_refs": True}),
        ("forged", {"expected_hash_change_deps": {"k"}}),
        ("same", {}),
    ],
    ids=["legacy-lock", "update", "expected-change", "matching"],
)
def test_carried_check_passes_when_lock_has_no_baseline_or_change_is_expected(
    tmp_path: Path, locked, overrides
) -> None:
    package, _legacy_hash, records = _package(tmp_path)
    forged = (CarriedLicense("LICENSE", "LICENSE", "sha256:" + "0" * 64),)
    lock_records = {"forged": forged, "same": records}.get(locked, ())
    ctx = _ctx(tmp_path, package_carried_licenses={"k": records}, **overrides)

    ensure_carried_licenses_match(ctx, "k", package, SimpleNamespace(carried_licenses=lock_records))

    assert (package / "LICENSE").is_file()


def test_skip_redownload_requires_intact_carried_copies(tmp_path: Path) -> None:
    package, legacy_hash, records = _package(tmp_path)
    locked = SimpleNamespace(content_hash=legacy_hash, carried_licenses=records)

    assert _should_skip_redownload(locked, package) is True
    (package / "LICENSE").write_bytes(b"edited\n")
    assert _should_skip_redownload(locked, package) is False
    (package / "LICENSE").unlink()
    assert _should_skip_redownload(locked, package) is False


def test_skip_redownload_with_legacy_lock_never_trusts_unrecorded_copies(tmp_path: Path) -> None:
    package, legacy_hash, _records = _package(tmp_path)

    assert _should_skip_redownload(SimpleNamespace(content_hash=legacy_hash), package) is False


_SHA_A, _SHA_B = "a" * 40, "b" * 40


@pytest.mark.parametrize(
    ("locked", "commit", "unchanged"),
    [
        (_SHA_A, _SHA_A, True),
        (_SHA_A, _SHA_B, False),
        ("cached", "cached", False),
        (None, None, False),
        ("", "", False),
    ],
    ids=["same", "moved", "cached-sentinel", "no-commit", "empty"],
)
def test_locked_commit_unchanged_truth_table(locked, commit, unchanged) -> None:
    row = SimpleNamespace(resolved_commit=locked)
    assert locked_commit_unchanged(row, commit) is unchanged
    update = SimpleNamespace(update_refs=True)
    install = SimpleNamespace(update_refs=False)
    assert update_accepts_new_content(update, row, commit) is not unchanged
    assert update_accepts_new_content(install, row, commit) is False


@pytest.mark.parametrize(
    ("apm_version", "package_type", "accepted"),
    [
        ("0.28.0", "marketplace_plugin", True),
        ("0.28.0", "apm_package", False),
        ("0.37.0", "marketplace_plugin", False),
    ],
)
def test_update_re_locks_an_unchanged_commit_only_for_a_0_28_plugin_row(
    apm_version: str, package_type: str, accepted: bool
) -> None:
    row = SimpleNamespace(resolved_commit=_SHA_A, package_type=package_type)
    lockfile = SimpleNamespace(apm_version=apm_version)
    ctx = SimpleNamespace(update_refs=True, existing_lockfile=lockfile)
    assert update_accepts_new_content(ctx, row, _SHA_A) is accepted
    install = SimpleNamespace(update_refs=False, existing_lockfile=lockfile)
    assert update_accepts_new_content(install, row, _SHA_A) is False


def test_update_accepts_new_carried_licences_for_a_moved_commit(tmp_path: Path) -> None:
    package, _legacy_hash, records = _package(tmp_path)
    forged = (CarriedLicense("LICENSE", "LICENSE", "sha256:" + "0" * 64),)
    ctx = _ctx(tmp_path, package_carried_licenses={"k": records}, update_refs=True)
    locked = SimpleNamespace(carried_licenses=forged, resolved_commit=_SHA_A)

    ensure_carried_licenses_match(ctx, "k", package, locked, _SHA_B)

    assert (package / "LICENSE").is_file()
