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
    record_carried_package_hash,
)
from apm_cli.utils.content_hash import compute_package_hash

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
    assert "Use 'apm update' to accept new content" in str(exc_info.value)
    assert not package.exists()


@pytest.mark.parametrize(
    ("scope", "command"),
    [(InstallScope.PROJECT, "apm update"), (InstallScope.USER, "apm update -g")],
)
def test_carried_mismatch_hint_names_targeted_update_for_direct_dep(
    tmp_path: Path, scope, command
) -> None:
    package, _legacy_hash, records = _package(tmp_path)
    forged = (CarriedLicense("LICENSE", "LICENSE", "sha256:" + "0" * 64),)
    ctx = _ctx(
        tmp_path,
        package_carried_licenses={"owner/repo/sub": records},
        declared_direct_refs={"owner/repo/sub": ()},
        scope=scope,
    )

    with pytest.raises(DirectDependencyError) as exc_info:
        ensure_carried_licenses_match(
            ctx, "owner/repo/sub", package, SimpleNamespace(carried_licenses=forged)
        )
    assert (
        f"Use '{command} owner/repo/sub' to accept new content for this package "
        "and re-pin it in the lockfile."
    ) in str(exc_info.value)


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
