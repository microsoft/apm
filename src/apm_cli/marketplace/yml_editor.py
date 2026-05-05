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

import re
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
    "add_plugin_entry",
    "add_upstream_entry",
    "add_upstream_package_entry",
    "list_upstream_entries",
    "remove_plugin_entry",
    "remove_upstream_entry",
    "update_plugin_entry",
]


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


def add_plugin_entry(
    yml_path: Path,
    *,
    source: str,
    name: str | None = None,
    version: str | None = None,
    ref: str | None = None,
    subdir: str | None = None,
    tag_pattern: str | None = None,
    tags: list[str] | None = None,
    include_prerelease: bool = False,
) -> str:
    """Append a new entry to ``packages[]``.

    Returns the resolved package name.
    """
    # --- input validation ---
    _validate_source(source)

    if version is not None and ref is not None:
        raise MarketplaceYmlError("Cannot specify both 'version' and 'ref' -- pick one")
    if version is None and ref is None:
        raise MarketplaceYmlError("At least one of 'version' or 'ref' must be provided")

    if subdir is not None:
        _validate_subdir(subdir)

    # Derive name from source repo if not provided.
    if name is None:
        name = source.split("/", 1)[1]

    # --- load ---
    data, original_text = _load_rt(yml_path)
    container = _get_marketplace_container(data)
    packages = container.get("packages")
    if packages is None:
        from ruamel.yaml.comments import CommentedSeq

        packages = CommentedSeq()
        container["packages"] = packages

    # Duplicate check (case-insensitive).
    lower = name.lower()
    for entry in packages:
        entry_name = entry.get("name", "")
        if isinstance(entry_name, str) and entry_name.lower() == lower:
            raise MarketplaceYmlError(f"Package '{name}' already exists")

    # --- build entry mapping ---
    from ruamel.yaml.comments import CommentedMap

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

    packages.append(new_entry)

    # --- write + validate ---
    _write_and_validate(yml_path, data, original_text)
    return name


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

    # Version / ref mutual exclusion: setting one clears the other.
    has_version = "version" in fields and fields["version"] is not None
    has_ref = "ref" in fields and fields["ref"] is not None

    if has_version and has_ref:
        raise MarketplaceYmlError("Cannot specify both 'version' and 'ref' -- pick one")

    if has_version:
        entry["version"] = fields["version"]
        # Clear ref if present.
        if "ref" in entry:
            del entry["ref"]

    if has_ref:
        entry["ref"] = fields["ref"]
        # Clear version if present.
        if "version" in entry:
            del entry["version"]

    # Simple scalar fields.
    _SIMPLE_FIELDS = ("subdir", "tag_pattern")
    for key in _SIMPLE_FIELDS:
        if key in fields and fields[key] is not None:
            if key == "subdir":
                _validate_subdir(fields[key])
            entry[key] = fields[key]

    # Boolean field: include_prerelease.
    if "include_prerelease" in fields and fields["include_prerelease"] is not None:
        entry["include_prerelease"] = fields["include_prerelease"]

    # List field: tags.
    if "tags" in fields and fields["tags"] is not None:
        entry["tags"] = fields["tags"]

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


# -------------------------------------------------------------------
# Upstream entries
# -------------------------------------------------------------------


_UPSTREAM_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_UPSTREAM_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def _validate_upstream_alias(alias: str) -> None:
    if not _UPSTREAM_ALIAS_RE.match(alias):
        raise MarketplaceYmlError(
            f"Upstream alias '{alias}' must start with a letter or digit and contain "
            f"only letters, digits, '_' or '-' (max 64 chars)."
        )


def _validate_upstream_repo(repo: str) -> None:
    if not _UPSTREAM_REPO_RE.match(repo):
        raise MarketplaceYmlError(
            f"Upstream 'repo' must match '<owner>/<repo>' shape, got '{repo}'"
        )


def _find_upstream_index(upstreams, alias: str) -> int:
    """Return the index of the upstream whose ``alias`` matches.

    Aliases are case-sensitive (unlike package names) because they are
    used as primary keys throughout the upstream resolution and
    lockfile pipeline. Raises ``MarketplaceYmlError`` if not found.
    """
    for idx, entry in enumerate(upstreams):
        entry_alias = entry.get("alias", "")
        if isinstance(entry_alias, str) and entry_alias == alias:
            return idx
    raise MarketplaceYmlError(f"Upstream '{alias}' not found")


def add_upstream_entry(
    yml_path: Path,
    *,
    alias: str,
    repo: str,
    ref: str | None = None,
    branch: str | None = None,
    path: str | None = None,
    host: str | None = None,
    allow_head: bool = False,
) -> None:
    """Append a new entry to ``upstreams[]``.

    Either ``ref`` (commit SHA, tag) or ``branch`` (with ``allow_head``
    when mutable) must be provided -- mirroring the strict-parser
    contract.  Raises ``MarketplaceYmlError`` on validation failure or
    duplicate alias.
    """
    _validate_upstream_alias(alias)
    _validate_upstream_repo(repo)

    if ref is None and branch is None:
        raise MarketplaceYmlError("Upstream must specify either 'ref' (commit/tag) or 'branch'.")
    if path is not None:
        _validate_subdir(path)

    data, original_text = _load_rt(yml_path)
    container = _get_marketplace_container(data)
    upstreams = container.get("upstreams")
    if upstreams is None:
        from ruamel.yaml.comments import CommentedSeq

        upstreams = CommentedSeq()
        container["upstreams"] = upstreams

    # Duplicate-alias check.
    for entry in upstreams:
        entry_alias = entry.get("alias", "")
        if isinstance(entry_alias, str) and entry_alias == alias:
            raise MarketplaceYmlError(f"Upstream '{alias}' already exists")

    from ruamel.yaml.comments import CommentedMap

    new_entry = CommentedMap()
    new_entry["alias"] = alias
    new_entry["repo"] = repo
    if ref is not None:
        new_entry["ref"] = ref
    if branch is not None:
        new_entry["branch"] = branch
    if path is not None:
        new_entry["path"] = path
    if host is not None:
        new_entry["host"] = host
    if allow_head:
        new_entry["allow_head"] = True

    upstreams.append(new_entry)
    _write_and_validate(yml_path, data, original_text)


def remove_upstream_entry(yml_path: Path, alias: str, *, dry_run: bool = False) -> None:
    """Remove an ``upstreams[]`` entry by alias.

    Raises ``MarketplaceYmlError`` when the alias is in use by any
    ``packages[]`` entry -- removing it would leave the manifest with
    dangling references.

    When *dry_run* is True, all validation is performed but the file is
    not written. Useful for pre-validating before prompting the user.
    """
    data, original_text = _load_rt(yml_path)
    container = _get_marketplace_container(data)
    upstreams = container.get("upstreams")
    if upstreams is None:
        raise MarketplaceYmlError(f"Upstream '{alias}' not found")

    # Reject removal if any package still references this alias.
    packages = container.get("packages") or []
    for entry in packages:
        entry_alias = entry.get("upstream", None)
        if isinstance(entry_alias, str) and entry_alias == alias:
            entry_name = entry.get("name", "<unnamed>")
            raise MarketplaceYmlError(
                f"Upstream '{alias}' is still referenced by package "
                f"'{entry_name}'. Remove the package first."
            )

    idx = _find_upstream_index(upstreams, alias)
    if dry_run:
        return
    del upstreams[idx]
    _write_and_validate(yml_path, data, original_text)


def list_upstream_entries(yml_path: Path) -> list[dict]:
    """Return the list of upstream entries as plain dicts.

    The values are read-only snapshots; modifying them does not write
    back to disk. Returns an empty list when no ``upstreams:`` block is
    present.
    """
    data, _ = _load_rt(yml_path)
    container = _get_marketplace_container(data)
    upstreams = container.get("upstreams") or []
    result: list[dict] = []
    for entry in upstreams:
        if not isinstance(entry, dict):
            continue
        # Materialise to a plain dict (drop ruamel CommentedMap wrapping).
        result.append({k: v for k, v in entry.items()})
    return result


def add_upstream_package_entry(
    yml_path: Path,
    *,
    upstream: str,
    plugin: str | None = None,
    name: str | None = None,
    version: str | None = None,
    ref: str | None = None,
    tag_pattern: str | None = None,
    tags: list[str] | None = None,
    include_prerelease: bool = False,
    allow_head: bool = False,
    description: str | None = None,
) -> str:
    """Append an upstream-sourced ``packages[]`` entry.

    Distinct from :func:`add_plugin_entry`: this writes ``upstream`` and
    ``plugin`` keys instead of ``source``. The referenced upstream alias
    must already exist in the ``upstreams:`` block.

    Returns the resolved package name (``name`` or ``plugin``).
    """
    if version is not None and ref is not None:
        raise MarketplaceYmlError("Cannot specify both 'version' and 'ref' -- pick one")

    if not isinstance(upstream, str) or not upstream:
        raise MarketplaceYmlError("'upstream' alias is required")

    effective_plugin = plugin if plugin is not None else name
    if not effective_plugin:
        raise MarketplaceYmlError(
            "Either 'plugin' or 'name' must be provided so the upstream plugin can be located"
        )

    if name is None:
        name = effective_plugin

    data, original_text = _load_rt(yml_path)
    container = _get_marketplace_container(data)

    # Verify the upstream alias is registered.
    upstreams = container.get("upstreams") or []
    alias_known = any(isinstance(u, dict) and u.get("alias") == upstream for u in upstreams)
    if not alias_known:
        raise MarketplaceYmlError(
            f"Upstream alias '{upstream}' is not registered. "
            f"Run 'apm marketplace upstream add <repo> --alias {upstream} ...' first."
        )

    packages = container.get("packages")
    if packages is None:
        from ruamel.yaml.comments import CommentedSeq

        packages = CommentedSeq()
        container["packages"] = packages

    # Cross-shape duplicate-name check (case-insensitive).
    lower = name.lower()
    for entry in packages:
        entry_name = entry.get("name", "")
        if isinstance(entry_name, str) and entry_name.lower() == lower:
            raise MarketplaceYmlError(f"Package '{name}' already exists")

    # (upstream, plugin) tuple uniqueness check.
    for entry in packages:
        if (
            isinstance(entry, dict)
            and entry.get("upstream") == upstream
            and (entry.get("plugin") or entry.get("name")) == effective_plugin
        ):
            raise MarketplaceYmlError(
                f"Plugin '{effective_plugin}' from upstream '{upstream}' is already exposed"
            )

    from ruamel.yaml.comments import CommentedMap

    new_entry = CommentedMap()
    new_entry["name"] = name
    new_entry["upstream"] = upstream
    if plugin is not None and plugin != name:
        new_entry["plugin"] = plugin

    if version is not None:
        new_entry["version"] = version
    if ref is not None:
        new_entry["ref"] = ref
    if tag_pattern is not None:
        new_entry["tag_pattern"] = tag_pattern
    if include_prerelease:
        new_entry["include_prerelease"] = True
    if allow_head:
        new_entry["allow_head"] = True
    if tags is not None and len(tags) > 0:
        new_entry["tags"] = tags
    if description is not None:
        new_entry["description"] = description

    packages.append(new_entry)
    _write_and_validate(yml_path, data, original_text)
    return name
