"""Round-trip YAML editor for ``marketplace.yml`` package entries.

Uses ``ruamel.yaml`` (round-trip mode) so that comments, key ordering,
and whitespace are preserved across edits.  All mutations follow an
atomic-write-then-revalidate pattern:

1. Read the file with ``ruamel.yaml``.
2. Mutate the in-memory ``CommentedMap``.
3. Write to a temp file, ``os.fsync()``, ``os.replace()`` over original.
4. Call ``load_marketplace_yml()`` to re-validate.
5. On validation failure, restore the original content and re-raise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML

from ..utils.path_security import PathTraversalError, validate_path_segments
from ._io import atomic_write
from .errors import MarketplaceYmlError
from .yml_schema import (
    SOURCE_RE,
    load_marketplace_from_apm_yml,
    load_marketplace_yml,
)

__all__ = [
    "PluginEntrySpec",
    "add_plugin_entry",
    "remove_plugin_entry",
    "update_plugin_entry",
]


@dataclass
class PluginEntrySpec:
    """Parameters for adding a plugin entry to a marketplace YAML file."""

    source: str
    name: str | None = None
    version: str | None = None
    ref: str | None = None
    subdir: str | None = None
    tag_pattern: str | None = None
    tags: list[str] | None = field(default=None)
    include_prerelease: bool = False


# -------------------------------------------------------------------
# Internal helpers
# -------------------------------------------------------------------


def _rt_yaml() -> YAML:
    """Return a round-trip ``YAML`` instance with consistent settings."""
    yml = YAML(typ="rt")
    yml.preserve_quotes = True
    return yml


def _load_rt(yml_path: Path):
    """Load *yml_path* with ruamel round-trip mode.

    Returns the ``CommentedMap`` root document.
    """
    text = yml_path.read_text(encoding="utf-8")
    return _rt_yaml().load(text), text


def _dump_rt(data) -> str:
    """Dump a ruamel ``CommentedMap`` back to a YAML string."""
    stream = StringIO()
    _rt_yaml().dump(data, stream)
    return stream.getvalue()


def _is_apm_yml_with_marketplace(data: object) -> bool:
    """Detect an apm.yml file that hosts a ``marketplace:`` block.

    The legacy ``marketplace.yml`` shape has marketplace fields (``owner``,
    ``packages``) at the root; the apm.yml shape nests them under
    ``marketplace:``.  We pick whichever shape the file actually has.

    Requires the ``marketplace`` value itself to be a mapping; otherwise
    downstream callers (e.g. :func:`_get_marketplace_container`) would
    return a non-dict and crash on ``container.get(...)``.
    """
    if not isinstance(data, dict):
        return False
    block = data.get("marketplace")
    if block is None:
        return False
    return isinstance(block, dict)


def _get_marketplace_container(data):
    """Return the dict-like container holding marketplace fields.

    For apm.yml: ``data["marketplace"]``.
    For legacy marketplace.yml: ``data`` itself.
    """
    if _is_apm_yml_with_marketplace(data):
        return data["marketplace"]
    return data


def _validate_after_write(yml_path: Path, data) -> None:
    """Re-validate *yml_path* using the loader matching its shape."""
    if _is_apm_yml_with_marketplace(data):
        load_marketplace_from_apm_yml(yml_path)
    else:
        load_marketplace_yml(yml_path)


def _write_and_validate(yml_path: Path, data, original_text: str) -> None:
    """Atomically write *data* and re-validate.

    If validation fails the original content is restored and the
    ``MarketplaceYmlError`` is re-raised.
    """
    new_text = _dump_rt(data)
    atomic_write(yml_path, new_text)
    try:
        _validate_after_write(yml_path, data)
    except MarketplaceYmlError:
        # Restore original content before propagating.
        atomic_write(yml_path, original_text)
        raise


def _find_entry_index(packages, name: str) -> int:
    """Return the index of the entry whose ``name`` matches (case-insensitive).

    Raises ``MarketplaceYmlError`` if not found.
    """
    lower = name.lower()
    for idx, entry in enumerate(packages):
        entry_name = entry.get("name", "")
        if isinstance(entry_name, str) and entry_name.lower() == lower:
            return idx
    raise MarketplaceYmlError(f"Package '{name}' not found")


def _validate_source(source: str) -> None:
    """Validate that *source* has ``owner/repo`` shape or ``./...`` local path."""
    if not SOURCE_RE.match(source):
        raise MarketplaceYmlError(
            f"'source' must match '<owner>/<repo>' or './<path>' shape, got '{source}'"
        )
    try:
        validate_path_segments(source, context="source", allow_current_dir=True)
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc


def _validate_subdir(subdir: str) -> None:
    """Validate *subdir* for path traversal."""
    try:
        validate_path_segments(subdir, context="subdir")
    except PathTraversalError as exc:
        raise MarketplaceYmlError(str(exc)) from exc


# -------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------


def _extract_entry_params(
    spec: PluginEntrySpec | None,
    kwargs: dict,
) -> tuple[
    str | None, str | None, str | None, str | None, str | None, str | None, list | None, bool
]:
    """Extract entry parameters from spec or kwargs."""
    if spec is not None:
        return (
            spec.source,
            spec.name,
            spec.version,
            spec.ref,
            spec.subdir,
            spec.tag_pattern,
            spec.tags,
            spec.include_prerelease,
        )
    return (
        kwargs.get("source"),
        kwargs.get("name"),
        kwargs.get("version"),
        kwargs.get("ref"),
        kwargs.get("subdir"),
        kwargs.get("tag_pattern"),
        kwargs.get("tags"),
        kwargs.get("include_prerelease", False),
    )


def _validate_entry_params(
    source: str | None,
    version: str | None,
    ref: str | None,
    subdir: str | None,
) -> None:
    """Validate entry parameters before adding."""
    if source is None:
        raise MarketplaceYmlError("'source' is required")
    _validate_source(source)

    if version is not None and ref is not None:
        raise MarketplaceYmlError("Cannot specify both 'version' and 'ref' -- pick one")
    if version is None and ref is None:
        raise MarketplaceYmlError("At least one of 'version' or 'ref' must be provided")

    if subdir is not None:
        _validate_subdir(subdir)


def _ensure_packages_list(container: dict) -> list:
    """Ensure packages list exists in container."""
    packages = container.get("packages")
    if packages is None:
        from ruamel.yaml.comments import CommentedSeq

        packages = CommentedSeq()
        container["packages"] = packages
    return packages


def _check_duplicate_entry(packages: list, name: str) -> None:
    """Check for duplicate entry (case-insensitive)."""
    lower = name.lower()
    for entry in packages:
        entry_name = entry.get("name", "")
        if isinstance(entry_name, str) and entry_name.lower() == lower:
            raise MarketplaceYmlError(f"Package '{name}' already exists")


def _build_new_entry(
    name: str,
    source: str,
    **kwargs,
) -> dict:
    """Build new entry mapping.

    Keyword Args:
        version: Package version.
        ref: Git ref.
        subdir: Subdirectory path.
        tag_pattern: Tag pattern for version extraction.
        tags: List of tags.
        include_prerelease: Whether to include prereleases.
    """
    from ruamel.yaml.comments import CommentedMap

    version = kwargs.get("version")
    ref = kwargs.get("ref")
    subdir = kwargs.get("subdir")
    tag_pattern = kwargs.get("tag_pattern")
    tags = kwargs.get("tags")
    include_prerelease = kwargs.get("include_prerelease", False)

    new_entry = CommentedMap()
    new_entry["name"] = name
    new_entry["source"] = source

    if version is not None:
        new_entry["version"] = version
    if ref is not None:
        new_entry["ref"] = ref
    if subdir is not None:
        new_entry["subdir"] = subdir
    if tag_pattern is not None:
        new_entry["tag_pattern"] = tag_pattern
    if include_prerelease:
        new_entry["include_prerelease"] = True
    if tags is not None and len(tags) > 0:
        new_entry["tags"] = tags

    return new_entry


def add_plugin_entry(
    yml_path: Path,
    spec: PluginEntrySpec | None = None,
    **kwargs,
) -> str:
    """Append a new entry to ``packages[]``.

    Returns the resolved package name.
    """
    source, name, version, ref, subdir, tag_pattern, tags, include_prerelease = (
        _extract_entry_params(spec, kwargs)
    )

    _validate_entry_params(source, version, ref, subdir)

    # Derive name from source repo if not provided.
    if name is None:
        name = source.split("/", 1)[1]  # type: ignore[union-attr]

    # --- load ---
    data, original_text = _load_rt(yml_path)
    container = _get_marketplace_container(data)
    packages = _ensure_packages_list(container)

    _check_duplicate_entry(packages, name)

    new_entry = _build_new_entry(
        name,
        source,  # type: ignore[arg-type]
        version=version,
        ref=ref,
        subdir=subdir,
        tag_pattern=tag_pattern,
        tags=tags,
        include_prerelease=include_prerelease,
    )
    packages.append(new_entry)

    # --- write + validate ---
    _write_and_validate(yml_path, data, original_text)
    return name


def _update_version_or_ref(entry: dict, fields: dict) -> None:
    """Update version or ref fields with mutual exclusion."""
    has_version = "version" in fields and fields["version"] is not None
    has_ref = "ref" in fields and fields["ref"] is not None

    if has_version and has_ref:
        raise MarketplaceYmlError("Cannot specify both 'version' and 'ref' -- pick one")

    if has_version:
        entry["version"] = fields["version"]
        entry.pop("ref", None)

    if has_ref:
        entry["ref"] = fields["ref"]
        entry.pop("version", None)


def _update_simple_fields(entry: dict, fields: dict) -> None:
    """Update simple scalar fields (subdir, tag_pattern)."""
    _SIMPLE_FIELDS = ("subdir", "tag_pattern")
    for key in _SIMPLE_FIELDS:
        if key in fields and fields[key] is not None:
            if key == "subdir":
                _validate_subdir(fields[key])
            entry[key] = fields[key]


def _update_optional_fields(entry: dict, fields: dict) -> None:
    """Update optional fields (include_prerelease, tags)."""
    if "include_prerelease" in fields and fields["include_prerelease"] is not None:
        entry["include_prerelease"] = fields["include_prerelease"]
    if "tags" in fields and fields["tags"] is not None:
        entry["tags"] = fields["tags"]


def update_plugin_entry(yml_path: Path, name: str, **fields) -> None:
    """Update fields on an existing ``packages[]`` entry by name.

    Only fields that are explicitly provided (not ``None``) are updated.
    """
    data, original_text = _load_rt(yml_path)
    container = _get_marketplace_container(data)
    packages = container.get("packages")
    if packages is None:
        raise MarketplaceYmlError(f"Package '{name}' not found")

    idx = _find_entry_index(packages, name)
    entry = packages[idx]

    _update_version_or_ref(entry, fields)
    _update_simple_fields(entry, fields)
    _update_optional_fields(entry, fields)

    # --- write + validate ---
    _write_and_validate(yml_path, data, original_text)


def remove_plugin_entry(yml_path: Path, name: str) -> None:
    """Remove a ``packages[]`` entry by name (case-insensitive match)."""
    data, original_text = _load_rt(yml_path)
    container = _get_marketplace_container(data)
    packages = container.get("packages")
    if packages is None:
        raise MarketplaceYmlError(f"Package '{name}' not found")

    idx = _find_entry_index(packages, name)
    del packages[idx]

    # --- write + validate ---
    _write_and_validate(yml_path, data, original_text)
