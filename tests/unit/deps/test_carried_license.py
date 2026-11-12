"""Carry the source licence with subdirectory and whole-repo packages (#3556)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.deps.carried_license import (
    CarriedLicense,
    carried_copies_intact,
    carried_license_paths,
    carry_licenses,
    is_license_file_name,
    records_from_lock_data,
    records_to_lock_data,
)
from apm_cli.utils.content_hash import compute_file_hash

MIT_TEXT = b"MIT License\n\nCopyright (c) 2026 Example\n"


def _write(path: Path, data: bytes = b"x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _materialize_subdir(repo: Path, subdir: str, target: Path) -> None:
    """Mirror download_subdirectory_package: copy only the subdirectory."""
    import shutil

    shutil.copytree(repo / subdir, target)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("LICENSE", True),
        ("license", True),
        ("LICENSE.md", True),
        ("LICENSE-MIT", False),
        ("COPYING", True),
        ("NOTICE", True),
        ("NOTICE.txt", False),
        ("COPYING.LESSER", False),
        ("LICENCE", False),
        ("SKILL.md", False),
    ],
)
def test_is_license_file_name(name: str, expected: bool) -> None:
    assert is_license_file_name(name) is expected


def test_subdirectory_package_carries_repository_root_license(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "LICENSE", MIT_TEXT)
    _write(repo / "skills" / "productivity" / "grilling" / "SKILL.md", b"---\nname: g\n---\n")
    target = tmp_path / "apm_modules" / "grilling"
    _materialize_subdir(repo, "skills/productivity/grilling", target)

    records = carry_licenses(
        target,
        repository_root=repo,
        package_subpath="skills/productivity/grilling",
    )

    assert (target / "LICENSE").read_bytes() == MIT_TEXT
    assert records == (
        CarriedLicense(
            path="LICENSE",
            source="LICENSE",
            hash=compute_file_hash(repo / "LICENSE"),
        ),
    )


def test_nearest_ancestor_license_wins(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "LICENSE", MIT_TEXT)
    _write(repo / "skills" / "NOTICE", b"skills notice\n")
    _write(repo / "skills" / "a" / "SKILL.md")
    target = tmp_path / "pkg"
    _materialize_subdir(repo, "skills/a", target)

    records = carry_licenses(target, repository_root=repo, package_subpath="skills/a")

    assert (target / "NOTICE").read_bytes() == b"skills notice\n"
    assert not (target / "LICENSE").exists()
    assert [record.source for record in records] == ["skills/NOTICE"]


def test_precedence_within_one_directory_is_deterministic(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "NOTICE", b"notice\n")
    _write(repo / "COPYING", b"copying\n")
    _write(repo / "LICENSE.txt", b"license txt\n")
    _write(repo / "pkg" / "SKILL.md")
    target = tmp_path / "pkg"
    _materialize_subdir(repo, "pkg", target)

    records = carry_licenses(target, repository_root=repo, package_subpath="pkg")

    assert [record.source for record in records] == ["LICENSE.txt"]


def test_package_with_its_own_license_carries_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "LICENSE", MIT_TEXT)
    _write(repo / "sub" / "SKILL.md")
    _write(repo / "sub" / "LICENSE.md", b"own\n")
    target = tmp_path / "pkg"
    _materialize_subdir(repo, "sub", target)

    records = carry_licenses(target, repository_root=repo, package_subpath="sub")

    assert records == ()
    assert sorted(path.name for path in target.iterdir()) == ["LICENSE.md", "SKILL.md"]


def test_package_without_any_license_behaves_as_today(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "README.md")
    _write(repo / "sub" / "SKILL.md")
    _write(repo / "sub" / "skills" / "inner" / "SKILL.md")
    target = tmp_path / "pkg"
    _materialize_subdir(repo, "sub", target)
    before = sorted(p.relative_to(target).as_posix() for p in target.rglob("*"))

    records = carry_licenses(target, repository_root=repo, package_subpath="sub")

    assert records == ()
    assert sorted(p.relative_to(target).as_posix() for p in target.rglob("*")) == before


def test_whole_repo_package_carries_root_license_next_to_each_skill(tmp_path: Path) -> None:
    package = tmp_path / "owner" / "repo"
    _write(package / "LICENSE", MIT_TEXT)
    _write(package / "skills" / "alpha" / "SKILL.md")
    _write(package / "skills" / "beta" / "SKILL.md")
    _write(package / "skills" / "beta" / "COPYING", b"beta own\n")
    _write(package / ".apm" / "skills" / "gamma" / "SKILL.md")

    records = carry_licenses(package)

    assert (package / "skills" / "alpha" / "LICENSE").read_bytes() == MIT_TEXT
    assert (package / ".apm" / "skills" / "gamma" / "LICENSE").read_bytes() == MIT_TEXT
    assert not (package / "skills" / "beta" / "LICENSE").exists()
    assert records == (
        CarriedLicense(
            ".apm/skills/gamma/LICENSE", "LICENSE", compute_file_hash(package / "LICENSE")
        ),
        CarriedLicense("skills/alpha/LICENSE", "LICENSE", compute_file_hash(package / "LICENSE")),
    )


def test_subdirectory_nested_skills_trace_back_to_repository_source(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "LICENSE", MIT_TEXT)
    _write(repo / "plugins" / "p" / "skills" / "one" / "SKILL.md")
    target = tmp_path / "pkg"
    _materialize_subdir(repo, "plugins/p", target)

    records = carry_licenses(target, repository_root=repo, package_subpath="plugins/p")

    assert {(record.path, record.source) for record in records} == {
        ("LICENSE", "LICENSE"),
        ("skills/one/LICENSE", "LICENSE"),
    }
    assert (target / "skills" / "one" / "LICENSE").read_bytes() == MIT_TEXT


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_symlinked_license_is_never_followed(tmp_path: Path) -> None:
    outside = _write(tmp_path / "secret.txt", b"secret\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "LICENSE").symlink_to(outside)
    _write(repo / "sub" / "SKILL.md")
    target = tmp_path / "pkg"
    _materialize_subdir(repo, "sub", target)

    assert carry_licenses(target, repository_root=repo, package_subpath="sub") == ()
    assert not (target / "LICENSE").exists()


def test_package_subpath_outside_repository_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "LICENSE", MIT_TEXT)
    target = tmp_path / "pkg"
    target.mkdir()

    with pytest.raises(ValueError):
        carry_licenses(target, repository_root=repo, package_subpath="../elsewhere")


def test_lock_data_round_trip_and_paths(tmp_path: Path) -> None:
    records = (
        CarriedLicense("LICENSE", "LICENSE", "sha256:" + "a" * 64),
        CarriedLicense("skills/x/LICENSE", "LICENSE", "sha256:" + "a" * 64),
    )

    data = records_to_lock_data(records)

    assert data == [
        {"path": "LICENSE", "source": "LICENSE", "hash": "sha256:" + "a" * 64},
        {"path": "skills/x/LICENSE", "source": "LICENSE", "hash": "sha256:" + "a" * 64},
    ]
    assert records_from_lock_data(data) == records
    assert carried_license_paths(records) == frozenset({"LICENSE", "skills/x/LICENSE"})


def test_carried_copies_keep_the_legacy_package_content_hash(tmp_path: Path) -> None:
    from apm_cli.utils.content_hash import compute_package_hash, verify_package_hash

    repo = tmp_path / "repo"
    _write(repo / "LICENSE", MIT_TEXT)
    _write(repo / "sub" / "SKILL.md")
    _write(repo / "sub" / "skills" / "inner" / "SKILL.md")
    target = tmp_path / "pkg"
    _materialize_subdir(repo, "sub", target)
    legacy_hash = compute_package_hash(target)

    records = carry_licenses(target, repository_root=repo, package_subpath="sub")
    carried = carried_license_paths(records)

    assert compute_package_hash(target) != legacy_hash
    assert compute_package_hash(target, exclude=carried) == legacy_hash
    assert verify_package_hash(target, legacy_hash, exclude=carried)
    assert carried_copies_intact(target, records)
    (target / "skills" / "inner" / "LICENSE").write_bytes(b"tampered\n")
    assert not carried_copies_intact(target, records)
    (target / "LICENSE").unlink()
    assert not carried_copies_intact(target, records)


@pytest.mark.parametrize(
    "bad",
    [
        [{"path": "../LICENSE", "source": "LICENSE", "hash": "sha256:00"}],
        [{"path": "skills/SKILL.md", "source": "LICENSE", "hash": "sha256:00"}],
        [{"path": "LICENSE", "source": "LICENSE"}],
        [{"path": "LICENSE", "source": "LICENSE", "hash": "sha256:00"}],
        [{"path": "/LICENSE", "source": "LICENSE", "hash": "sha256:" + "a" * 64}],
        ["LICENSE"],
        "LICENSE",
    ],
)
def test_untrusted_lock_data_is_dropped(bad) -> None:
    assert records_from_lock_data(bad) == ()


def test_locked_dependency_round_trips_carried_licenses() -> None:
    from apm_cli.deps.lockfile import LockedDependency

    record = CarriedLicense("LICENSE", "LICENSE", "sha256:" + "b" * 64)
    dep = LockedDependency(repo_url="owner/repo", carried_licenses=(record,))

    data = dep.to_dict()

    assert data["carried_licenses"] == [
        {"path": "LICENSE", "source": "LICENSE", "hash": "sha256:" + "b" * 64}
    ]
    restored = LockedDependency.from_dict(data)
    assert restored.carried_licenses == (record,)
    assert "carried_licenses" not in restored._unknown_fields
    assert "carried_licenses" not in LockedDependency(repo_url="owner/repo").to_dict()
