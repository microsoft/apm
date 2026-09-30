"""Dependency readers honor canonical alias placement without crossing stores."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.commands.deps.cli import _resolve_scope_deps
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.utils.path_security import PathTraversalError
from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.artifact_snapshot import ArtifactSnapshotSet, assert_snapshot_set_unchanged

pytestmark = pytest.mark.component


def _installed_store(root: Path, alias: str, declarations: str, owner: str = "owner") -> Path:
    """Create expected physical paths independently of the materialization helper."""
    installed = root / "apm_modules" / alias
    ordinary = root / "apm_modules" / "other" / "ordinary"
    for path, name in ((installed, "reader-bundle"), (ordinary, "ordinary")):
        path.mkdir(parents=True)
        dump_yaml({"name": name, "version": "1.0.0"}, path / "apm.yml")
    nested = installed / "skills" / "nested"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_bytes(b"---\nname: nested\n---\nNested package content.\n")
    if declarations in {"manifest", "both"}:
        dump_yaml(
            {
                "name": "consumer",
                "version": "1.0.0",
                "dependencies": {
                    "apm": [
                        {"git": f"{owner}/bundle", "alias": alias},
                        "other/ordinary",
                    ]
                },
            },
            root / "apm.yml",
        )
    if declarations in {"lock", "both"}:
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(repo_url=f"{owner}/bundle", alias=alias, resolved_commit="a" * 40)
        )
        lock.add_dependency(LockedDependency(repo_url="other/ordinary", resolved_commit="b" * 40))
        lock.save(root / "apm.lock.yaml")
    return installed


@pytest.mark.parametrize("alias", ["reader-kit", "reader.v2", ".reader-kit"])
@pytest.mark.parametrize("declarations", ["manifest", "lock", "both"])
def test_alias_readers_use_authorized_placement_and_preserve_nested_packages(
    tmp_path: Path, alias: str, declarations: str
) -> None:
    installed = _installed_store(tmp_path, alias, declarations)
    before = ArtifactSnapshotSet.capture({"store": tmp_path})
    packages, orphans = _resolve_scope_deps(tmp_path, MagicMock())
    assert orphans == []
    assert {package["name"] for package in packages} == {"owner/bundle", "other/ordinary"}
    aliased = next(package for package in packages if package["name"] == "owner/bundle")
    assert aliased["path"] == str(installed)
    assert aliased["source"] == "github"
    assert aliased["version"] == "1.0.0"
    assert aliased["is_orphaned"] is False
    assert not (tmp_path / "apm_modules" / "owner" / "bundle").exists()
    assert_snapshot_set_unchanged(before, ArtifactSnapshotSet.capture({"store": tmp_path}))


def test_alias_reader_uses_only_selected_store(tmp_path: Path) -> None:
    project = tmp_path / "project"
    user = tmp_path / "user"
    _installed_store(project, "same-alias", "both", owner="project")
    expected = _installed_store(user, "same-alias", "both", owner="user")
    before = ArtifactSnapshotSet.capture({"project": project, "user": user})
    packages, orphans = _resolve_scope_deps(user, MagicMock())
    assert orphans == []
    assert {package["name"] for package in packages} == {"user/bundle", "other/ordinary"}
    aliased = next(package for package in packages if package["name"] == "user/bundle")
    assert aliased["path"] == str(expected)
    assert_snapshot_set_unchanged(
        before, ArtifactSnapshotSet.capture({"project": project, "user": user})
    )


@pytest.mark.parametrize("declarations", ["manifest", "lock", "both"])
def test_alias_reader_refuses_escape_without_inspecting_external_package(
    tmp_path: Path, declarations: str
) -> None:
    store = tmp_path / "store"
    external = tmp_path / "external"
    external.mkdir()
    dump_yaml({"name": "private", "version": "1.0.0"}, external / "apm.yml")
    modules = store / "apm_modules"
    modules.mkdir(parents=True)
    (modules / "escaped").symlink_to(external, target_is_directory=True)
    if declarations in {"manifest", "both"}:
        dump_yaml(
            {
                "name": "consumer",
                "version": "1.0.0",
                "dependencies": {"apm": [{"git": "owner/bundle", "alias": "escaped"}]},
            },
            store / "apm.yml",
        )
    if declarations in {"lock", "both"}:
        lock = LockFile()
        lock.add_dependency(
            LockedDependency(repo_url="owner/bundle", alias="escaped", resolved_commit="a" * 40)
        )
        lock.save(store / "apm.lock.yaml")
    before = ArtifactSnapshotSet.capture({"store": store, "external": external})
    with pytest.raises(PathTraversalError):
        _resolve_scope_deps(store, MagicMock())
    assert_snapshot_set_unchanged(
        before, ArtifactSnapshotSet.capture({"store": store, "external": external})
    )
