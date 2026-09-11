"""Consumer manifest planning: preserve author metadata; never edit sources."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apm_cli.deps.path_anchoring import _join
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency import DependencyReference
from apm_cli.models.dependency.object_fields import local_path_apm_yml_entry
from apm_cli.utils.yaml_io import (
    load_yaml_roundtrip,
    write_yaml_text_atomic,
    yaml_roundtrip_to_str,
)

from .safety import checked_file, checked_path


def read_manifest(path: Path, root: Path) -> tuple[Any, list[DependencyReference], bytes | None]:
    """Read and validate existing metadata; do not silently replace invalid YAML."""
    checked_path(path, root)
    if not path.exists():
        return {"name": "onboarded-project", "version": "1.0.0"}, [], None
    checked_file(path, root)
    original = path.read_bytes()
    data = load_yaml_roundtrip(path)
    if not isinstance(data, dict):
        raise ValueError("Invalid apm.yml: expected a mapping. Repair the manifest and retry.")
    for group in ("dependencies", "devDependencies"):
        dependencies = data.get(group, {})
        if not isinstance(dependencies, dict):
            raise ValueError(f"Invalid apm.yml: {group} must be a mapping.")
        for kind, entries in dependencies.items():
            if not isinstance(entries, list) or any(
                not isinstance(entry, (str, dict)) for entry in entries
            ):
                raise ValueError(f"Invalid apm.yml: {group}.{kind} must be a list of references.")
    package = APMPackage.from_mapping(data, package_path=path.parent, create_config=False)
    references = package.get_all_apm_dependencies()
    return data, references, original


def local_directory(reference: DependencyReference, consumer: Path) -> Path:
    """Resolve a direct local reference through the existing path anchoring owner."""
    return _join(consumer, reference.local_path)


def package_entry(source: Path, consumer: Path, *, user_scope: bool) -> dict[str, object]:
    """Emit the canonical local object form, absolute at user scope."""
    raw = source.as_posix() if user_scope else "./" + source.relative_to(consumer).as_posix()
    reference = DependencyReference.parse(raw)
    return local_path_apm_yml_entry(reference.local_path, None, None, None)


def write_manifest(
    path: Path,
    root: Path,
    data: Any,
    additions: list[dict[str, object]],
    original: bytes | None,
) -> bool:
    """Commit only missing dependency entries, atomically, after revalidation."""
    if not additions:
        return False
    checked_path(path, root)
    current = path.read_bytes() if path.exists() else None
    if current != original:
        raise ValueError("apm.yml changed during discovery. Review and retry.")
    data.setdefault("dependencies", {}).setdefault("apm", []).extend(additions)
    content = yaml_roundtrip_to_str(data)
    checked_path(path.parent, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml_text_atomic(path, content)
    return True
