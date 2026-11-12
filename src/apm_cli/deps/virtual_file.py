"""Materialize one selected primitive as an ordinary APM package."""

from pathlib import Path

from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency import DependencyReference
from apm_cli.models.dependency.host_virtual import dependency_repository_owner
from apm_cli.utils.atomic_io import atomic_write_text
from apm_cli.utils.path_security import ensure_path_within, validate_path_segments
from apm_cli.utils.yaml_io import yaml_to_str


def virtual_file_subdirectory(filename: str) -> str:
    """Return the ordinary primitive directory for a supported file."""
    validate_path_segments(filename, context="single-file package", reject_empty=True)
    for extension, directory in DependencyReference.VIRTUAL_FILE_SUBDIRECTORIES.items():
        if filename.endswith(extension):
            return directory
    raise ValueError(
        f"Unsupported single-file dependency: {filename}. "
        f"Use {', '.join(DependencyReference.VIRTUAL_FILE_EXTENSIONS)}, "
        "or a directory package with apm.yml for companion assets."
    )


def _description(filename: str, content: bytes) -> str:
    """Keep the existing remote virtual-package description projection."""
    description = f"Virtual package containing {filename}"
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return description
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end > 0:
            for line in text[4:end].split("\n"):
                if line.startswith("description:"):
                    return line.split(":", 1)[1].strip().strip("\"'")
    return description


def materialize_virtual_file_package(
    dep_ref: DependencyReference,
    target_path: Path,
    filename: str,
    content: bytes,
) -> APMPackage:
    """Write the shared single-file layout without acquiring or identifying a source."""
    subdirectory = virtual_file_subdirectory(filename)
    primitive_path = target_path / ".apm" / subdirectory / filename
    ensure_path_within(primitive_path, target_path)
    manifest_path = target_path / "apm.yml"
    ensure_path_within(manifest_path, target_path)

    name = dep_ref.get_virtual_package_name()
    description = _description(filename, content)
    author = None if dep_ref.is_local else dependency_repository_owner(dep_ref)
    manifest = {"name": name, "version": "1.0.0", "description": description}
    if author is not None:
        manifest["author"] = author
    manifest_text = yaml_to_str(manifest)

    primitive_path.parent.mkdir(parents=True, exist_ok=True)
    primitive_path.write_bytes(content)
    atomic_write_text(manifest_path, manifest_text)
    return APMPackage(
        name=name,
        version="1.0.0",
        description=description,
        author=author,
        source=dep_ref.local_path if dep_ref.is_local else dep_ref.to_github_url(),
        package_path=target_path,
    )
