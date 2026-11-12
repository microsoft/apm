"""Prune preserves host-aware identity without changing materialized paths."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.commands._helpers import _build_expected_install_paths
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.models.dependency.reference import DependencyReference
from tests.helpers.prune_casing import (
    CaseSensitiveMaterializationReader,
    _prefix_project,
    _project,
    _snapshot,
)

pytestmark = pytest.mark.component


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("with_lock", [True, False])
@pytest.mark.parametrize(
    "declared,installed", [("Microsoft/APM", "microsoft/apm"), ("microsoft/apm", "Microsoft/APM")]
)
def test_prune_preserves_casing_equivalent_github_package(
    tmp_path, monkeypatch, dry_run, with_lock, declared, installed
):
    package, orphan = _project(tmp_path, declared, installed, with_lock=with_lock)
    before = {p.name: p.read_bytes() for p in package.iterdir()}
    manifest = (tmp_path / "apm.yml").read_bytes()
    lock_path = tmp_path / "apm.lock.yaml"
    lock_before = lock_path.read_bytes() if with_lock else None
    monkeypatch.chdir(tmp_path)
    try:
        result = CliRunner().invoke(cli, ["prune", *(["--dry-run"] if dry_run else [])])
    finally:
        clear_apm_yml_cache()
    assert result.exit_code == 0, result.output
    assert {p.name: p.read_bytes() for p in package.iterdir()} == before
    assert (tmp_path / "apm.yml").read_bytes() == manifest
    assert "other/orphan" in result.output
    if dry_run:
        assert installed not in result.output
    else:
        assert f"Removed {installed}" not in result.output
    assert orphan.exists() is dry_run
    if with_lock:
        if dry_run:
            assert lock_path.read_bytes() == lock_before
        else:
            lock = LockFile.read(lock_path)
            assert set(lock.dependencies) == {installed.lower()}
            assert lock.dependencies[installed.lower()].resolved_commit == "a" * 40


def test_prune_retains_case_equivalent_transitive_dependency(tmp_path, monkeypatch):
    package, orphan = _project(tmp_path, "direct/package", "MixedOrg/Transitive", with_lock=False)
    LockFile(
        dependencies={
            "mixedorg/transitive": LockedDependency(repo_url="mixedorg/transitive", depth=2)
        }
    ).write(tmp_path / "apm.lock.yaml")
    monkeypatch.chdir(tmp_path)
    try:
        result = CliRunner().invoke(cli, ["prune"])
    finally:
        clear_apm_yml_cache()
    assert result.exit_code == 0, result.output
    assert (package / "notes.txt").read_bytes() == b"user content\n"
    assert not orphan.exists()
    assert "mixedorg/transitive" in LockFile.read(tmp_path / "apm.lock.yaml").dependencies


@pytest.mark.parametrize(
    "dependency",
    [
        DependencyReference(repo_url="MixedOrg/Repo", host="gitlab.com"),
        DependencyReference(repo_url="MixedOrg/Repo", host="github.com", alias="ExactAlias"),
        DependencyReference(repo_url="", is_local=True, local_path="./MixedLocal"),
    ],
)
def test_prune_does_not_casefold_sensitive_materialization(dependency, tmp_path):
    modules = tmp_path / "apm_modules"
    source_path = dependency.get_install_path(modules)
    wrong_case = modules / source_path.relative_to(modules).as_posix().lower()
    wrong_case.mkdir(parents=True)
    (wrong_case / "notes.txt").write_bytes(b"retained")
    with patch("apm_cli.commands._helpers.find_case_equivalent_materialization_path") as lookup:
        expected = _build_expected_install_paths(
            [dependency], None, modules, preserve_installed_case=True
        )
    lookup.assert_not_called()
    assert (wrong_case / "notes.txt").read_bytes() == b"retained"
    assert expected == {source_path.relative_to(modules).as_posix()}


def test_prune_casing_lookup_is_opt_in(tmp_path):
    dependency = DependencyReference.parse("Microsoft/APM")
    installed = tmp_path / "microsoft" / "apm"
    installed.mkdir(parents=True)
    assert _build_expected_install_paths([dependency], None, tmp_path) == {"Microsoft/APM"}
    assert _build_expected_install_paths(
        [dependency], None, tmp_path, preserve_installed_case=True
    ) == {"microsoft/apm"}


def test_prune_does_not_preserve_case_changed_unknown_host_package(tmp_path, monkeypatch):
    package, orphan = _project(
        tmp_path, "https://gitlab.com/MixedOrg/Repo", "mixedorg/repo", with_lock=False
    )
    monkeypatch.chdir(tmp_path)
    try:
        result = CliRunner().invoke(cli, ["prune"])
    finally:
        clear_apm_yml_cache()
    assert result.exit_code == 0, result.output
    assert not package.exists()
    assert not orphan.exists()


@pytest.mark.parametrize("virtual_path", ["Skills/Exact", "skills/exact"])
def test_prune_preserves_virtual_path_case(tmp_path, virtual_path):
    dependency = DependencyReference.parse_from_dict(
        {"git": "Microsoft/APM", "path": "Skills/Exact"}
    )
    (tmp_path / "microsoft" / "apm" / virtual_path).mkdir(parents=True)
    expected = (
        "microsoft/apm/Skills/Exact"
        if virtual_path == "Skills/Exact"
        else "Microsoft/APM/Skills/Exact"
    )
    assert _build_expected_install_paths(
        [dependency], None, tmp_path, preserve_installed_case=True
    ) == {expected}


@pytest.mark.parametrize("dry_run", [True, False])
def test_prune_ambiguous_casing_fails_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    package, orphan = _project(tmp_path, "Microsoft/APM", "microsoft/apm", with_lock=True)
    manifest = (tmp_path / "apm.yml").read_bytes()
    lock = (tmp_path / "apm.lock.yaml").read_bytes()
    monkeypatch.chdir(tmp_path)
    reader = CaseSensitiveMaterializationReader(
        Path("apm_modules"), ("microsoft/apm", "Microsoft/APM")
    )
    with patch("apm_cli.commands._helpers.CachedMaterializationPathReader", return_value=reader):
        try:
            result = CliRunner().invoke(cli, ["prune", *(["--dry-run"] if dry_run else [])])
        finally:
            clear_apm_yml_cache()
    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert "Found multiple package directories for one dependency:" in output
    assert "Failed to parse" not in output
    assert "Inspect the duplicates under apm_modules/" in output
    assert orphan.exists()
    assert (package / "notes.txt").read_bytes() == b"user content\n"
    assert (tmp_path / "apm.yml").read_bytes() == manifest
    assert (tmp_path / "apm.lock.yaml").read_bytes() == lock


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("duplicate", [False, True], ids=["different-repos", "same-repo"])
def test_prune_complete_paths_with_portable_reader_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool, duplicate: bool
) -> None:
    """A simulated case-sensitive reader proves command behavior on every OS."""
    orphan, relatives = _prefix_project(tmp_path, duplicate=duplicate)
    modules = tmp_path / "apm_modules"
    before = _snapshot(modules)
    manifest = (tmp_path / "apm.yml").read_bytes()
    reader = CaseSensitiveMaterializationReader(modules, relatives)
    monkeypatch.chdir(tmp_path)
    with (
        patch("apm_cli.commands._helpers.CachedMaterializationPathReader", return_value=reader),
        patch("apm_cli.commands.prune._scan_installed_packages", return_value=list(relatives)),
    ):
        try:
            result = CliRunner().invoke(cli, ["prune", *(["--dry-run"] if dry_run else [])])
        finally:
            clear_apm_yml_cache()
    assert result.exit_code == (1 if duplicate else 0), result.output
    assert orphan.exists() is (dry_run or duplicate)
    expected = {
        name: content
        for name, content in before.items()
        if dry_run or duplicate or not name.startswith("other/orphan/")
    }
    assert _snapshot(modules) == expected
    assert (tmp_path / "apm.yml").read_bytes() == manifest
    if duplicate:
        assert "Found multiple package directories for one dependency:" in " ".join(
            result.output.split()
        )


@pytest.mark.parametrize(
    "other",
    [
        DependencyReference(repo_url="other/source", host="github.com", alias="Microsoft"),
        DependencyReference(repo_url="Microsoft/Other", host="gitlab.com"),
    ],
    ids=["alias-owner-prefix", "case-sensitive-host"],
)
def test_prune_distinguishes_unrelated_owner_prefixes(
    tmp_path: Path, other: DependencyReference
) -> None:
    """Aliases and other hosts cannot make a GitHub owner prefix ambiguous."""
    github = DependencyReference.parse("microsoft/apm")
    relatives = ("microsoft/apm", other.get_install_path(tmp_path).relative_to(tmp_path).as_posix())
    reader = CaseSensitiveMaterializationReader(tmp_path, relatives)
    with patch("apm_cli.commands._helpers.CachedMaterializationPathReader", return_value=reader):
        assert _build_expected_install_paths(
            [github, other], None, tmp_path, preserve_installed_case=True
        ) == set(relatives)
