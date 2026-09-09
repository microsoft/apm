"""Dependency alias validation, containment, and durable replay conformance."""

from pathlib import Path
from urllib.parse import urlparse

import jsonschema
import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.deps.registry.resolver import RegistryResolution
from apm_cli.models.apm_package import DependencyReference
from apm_cli.utils.path_security import PathTraversalError
from tests.spec_conformance._helpers import (
    load_schema,
    load_yaml_fixture,
    sha256_hex,
    validate_against,
)

pytestmark = pytest.mark.component


@pytest.mark.parametrize(
    ("name", "digest"),
    [
        ("manifest", "7bdefbe443d3315d71add021c777d776c9cfd4942acb19750a799f46fa0d1344"),
        ("lockfile", "6c0dca9e7994035b55da17340b1f9f6c6673501c63ebd947400cf472d5723560"),
    ],
)
def test_published_schema_content_remains_immutable(
    repo_root: Path, name: str, digest: str
) -> None:
    """Preserve published LF content regardless of local Git checkout newlines."""
    path = repo_root / "docs" / "public" / "specs" / "schemas" / f"{name}-v0.1.schema.json"
    assert sha256_hex(path.read_text(encoding="utf-8").encode("utf-8")) == digest


@pytest.mark.parametrize("name", ["manifest", "lockfile"])
def test_alias_schema_revision_has_distinct_published_identity(name: str) -> None:
    """The alias-aware schema cannot overwrite a cached validator at the old ID."""
    old = load_schema(f"{name}-v0.1.schema.json")
    revised = load_schema(f"{name}-v0.1.41.schema.json")
    jsonschema.Draft202012Validator.check_schema(revised)
    old_id = urlparse(old["$id"])
    new_id = urlparse(revised["$id"])
    assert old_id.path == f"/apm/specs/schemas/{name}-v0.1.schema.json"
    assert new_id.path == f"/apm/specs/schemas/{name}-v0.1.41.schema.json"
    assert new_id.scheme == old_id.scheme == "https"
    assert new_id.hostname == old_id.hostname == "microsoft.github.io"
    assert new_id.query == new_id.fragment == ""


@pytest.mark.req("req-mf-025")
@pytest.mark.parametrize(
    "alias", [None, ".safe", "safe.", "foo..bar", "my-skill.v2", " \tMy.Safe\n"]
)
@pytest.mark.parametrize("source", ["git", "local", "registry"])
def test_alias_survives_lock_replay_without_changing_source(
    tmp_path: Path, alias: str | None, source: str
) -> None:
    """Lock serialization preserves placement independently of source identity."""
    dependency = DependencyReference(
        repo_url="owner/package",
        host="github.com",
        reference="v1",
        alias=alias,
        source=source,
        is_local=source == "local",
        local_path="../sibling" if source == "local" else None,
        declaring_parent="owner/parent" if source == "local" else None,
        anchored_local_path=str(tmp_path / "sibling") if source == "local" else None,
    )
    resolution = (
        RegistryResolution(
            resolved_url="https://registry.example.com/package.tgz",
            resolved_hash="sha256:" + "b" * 64,
            version="1.0.0",
        )
        if source == "registry"
        else None
    )
    locked = LockedDependency.from_dependency_ref(
        dependency,
        "a" * 40,
        depth=2,
        resolved_by="owner/parent",
        registry_resolution=resolution,
    )
    lockfile = LockFile()
    lockfile.add_dependency(locked)
    restored_lock = LockFile.from_yaml(lockfile.to_yaml())
    restored = restored_lock.get_dependency(locked.get_unique_key())
    assert restored is not None
    replay = restored.to_dependency_ref()
    assert replay.alias == (alias.strip() if alias is not None else None)
    assert replay.get_install_path(tmp_path / "apm_modules") == dependency.get_install_path(
        tmp_path / "apm_modules"
    )
    assert replay.get_unique_key() == dependency.get_unique_key()
    assert replay.local_path == dependency.local_path
    assert replay.anchored_local_path == dependency.anchored_local_path
    assert replay.declaring_parent == dependency.declaring_parent
    assert restored.resolved_commit == "a" * 40
    assert restored.to_dict().get("alias") == replay.alias
    assert "alias" not in restored._unknown_fields
    if resolution is not None:
        assert restored.source == "registry"
        assert restored.resolved_url == resolution.resolved_url
        assert restored.resolved_hash == resolution.resolved_hash


@pytest.mark.req("req-mf-025")
def test_absent_alias_is_not_inferred_from_inventory_name() -> None:
    """An old lock's display name does not become placement metadata."""
    locked = LockedDependency.from_dict(
        {"repo_url": "owner/package", "name": "different-inventory-name"}
    )
    assert locked.to_dependency_ref().alias is None
    assert "alias" not in locked.to_dict()


@pytest.mark.req("req-mf-025")
def test_invalid_lock_alias_guidance_repairs_the_lock_not_the_manifest() -> None:
    """A lock-only defect must not send users back to an already-valid manifest."""
    with pytest.raises(ValueError, match=r"Restore a known-good apm\.lock\.yaml") as caught:
        LockedDependency.from_dict({"repo_url": "owner/package", "alias": ".."})
    assert "Do not delete content" in str(caught.value)
    assert "in apm.yml" not in str(caught.value)


@pytest.mark.req("req-mf-025")
@pytest.mark.parametrize(
    ("alias", "manifest_valid", "lock_valid"),
    [
        (".safe", True, True),
        ("safe.", True, True),
        ("foo..bar", True, True),
        ("my-skill.v2", True, True),
        (" \tMy.Safe\n", True, False),
        ("safe\n", True, False),
        (".", False, False),
        ("..", False, False),
        ("..\n", False, False),
        (" \r..\t", False, False),
        ("safe\ninside", False, False),
        ("", False, False),
        (" \t\n", False, False),
        ("../escape", False, False),
        ("safe/name", False, False),
        (1, False, False),
    ],
)
def test_alias_schemas_distinguish_input_from_canonical_output(
    alias: object, manifest_valid: bool, lock_valid: bool
) -> None:
    """Whole schemas enforce canonical lock output without banning local sources."""
    manifest = {
        "name": "consumer",
        "version": "1.0.0",
        "dependencies": {"apm": [{"path": "../sibling", "alias": alias}]},
    }
    lock = load_yaml_fixture("lockfile", "v2-with-registry.yml")
    lock["dependencies"][0]["alias"] = alias
    for schema, document, valid in (
        ("manifest-v0.1.41.schema.json", manifest, manifest_valid),
        ("lockfile-v0.1.41.schema.json", lock, lock_valid),
    ):
        if valid:
            validate_against(schema, document)
        else:
            with pytest.raises(jsonschema.ValidationError):
                validate_against(schema, document)


@pytest.mark.req("req-mf-025")
@pytest.mark.parametrize("alias", [".", "..", "../escape", "foo/../bar", "%2e%2e", "", 1])
def test_lock_alias_rejects_invalid_destination_names(alias: object) -> None:
    """A tampered lock cannot bypass the manifest alias validator."""
    with pytest.raises(ValueError, match="alias"):
        LockedDependency.from_dict({"repo_url": "owner/package", "alias": alias})


@pytest.mark.req("req-mf-025")
@pytest.mark.parametrize("target", ["root", "outside"])
def test_alias_rejects_symlink_destination(tmp_path: Path, target: str) -> None:
    """Destination containment also applies when the lexical alias is valid."""
    modules = tmp_path / "apm_modules"
    modules.mkdir()
    destination = modules if target == "root" else tmp_path
    (modules / "safe").symlink_to(destination, target_is_directory=True)
    dependency = DependencyReference(repo_url="owner/package", alias="safe")
    with pytest.raises(PathTraversalError, match="alias"):
        dependency.get_install_path(modules)
