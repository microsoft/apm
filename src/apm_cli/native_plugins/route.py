"""The native plugin route decision: native or loose, per dependency (#4995).

One owner for whether a dependency may take the native plugin route on any
harness. Every native planner (Claude Code, Codex) asks it before any trust or
fingerprint work; a ``False`` answer projects the package loose, exactly as
before native plugins existed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_BOOL_TAG = "tag:yaml.org,2002:bool"


def _declared_native(dep: Any, manifest: Any) -> bool | None:
    """The ``native:`` key on *manifest*'s own declarations of *dep*, if any.

    Declarations that disagree resolve to ``False`` whatever their order.
    """
    getter = getattr(manifest, "get_all_apm_dependencies", None)
    if dep is None or getter is None:
        return None
    key = dep.get_unique_key()
    values = {
        getattr(declared, "native", None)
        for declared in getter()
        if declared.get_unique_key() == key
    } - {None}
    return None if not values else all(values)


def effective_native_route(dep: Any, manifest: Any) -> bool:
    """Whether dependency *dep* may take the native plugin route.

    The root *manifest*'s own declaration of *dep* decides through its
    ``native:`` key; otherwise the manifest's top-level ``native_plugins:``
    default; absent both, the route stays automatic (``True``). A key binds
    only its own declaration, so a transitive dependency follows the project
    default and a parent's key never reaches the packages it depends on.
    """
    declared = _declared_native(dep, manifest)
    if declared is not None:
        return declared
    return getattr(manifest, "native_plugins", None) is not False


def frozen_route_change_reasons(lockfile: Any, manifest: Any) -> list[str]:
    """Name each locked native plugin that *manifest* now keeps off the native route.

    ``--frozen`` cannot move a plugin between routes: that retires its native
    registration and rewrites the lock, so the frozen install refuses first.
    """
    reasons = []
    for key, locked in sorted(getattr(lockfile, "dependencies", {}).items()):
        if getattr(locked, "native_plugin", None) is None:
            continue
        if not effective_native_route(locked.to_dependency_ref(), manifest):
            reasons.append(
                f"  - {key} is locked on the native plugin route, but apm.yml now sets "
                "it loose (native: false)"
            )
    return reasons


def _misspelled(text: str, node: Any) -> str | None:
    """The raw spelling of a YAML 1.1 boolean that is not plain ``true``/``false``."""
    if getattr(node, "tag", None) != _BOOL_TAG or node.value in ("true", "false"):
        return None
    # An explicit ``!!bool`` tag is a boolean whatever its spelling.
    return None if text[node.start_mark.index :].startswith("!") else node.value


_MERGE_TAG = "tag:yaml.org,2002:merge"


def _mapping_items(node: Any, seen: set[int] | None = None) -> list:
    """A mapping's items, including those a ``<<`` merge key brings in.

    Each merged mapping is visited once, so a shared or cyclic merge graph
    costs no more than its size.
    """
    import yaml

    seen = set() if seen is None else seen
    if not isinstance(node, yaml.MappingNode) or id(node) in seen:
        return []
    seen.add(id(node))
    items = []
    for key, value in node.value:
        if key.tag != _MERGE_TAG:
            items.append((key, value))
            continue
        merged = value.value if isinstance(value, yaml.SequenceNode) else [value]
        for source in merged:
            items += _mapping_items(source, seen)
    return items


def route_boolean_spelling_errors(text: str) -> list[str]:
    """Name each ``native_plugins`` / dependency ``native`` value spelled ``no``, ``Off``...

    YAML 1.1 loads ``yes``/``no``/``on``/``off`` (any case) and ``True``/``FALSE``
    as booleans; for these fields only plain ``true``/``false`` or an explicit
    ``!!bool`` count (req-mf-030).
    """
    import yaml

    from apm_cli.utils.yaml_io import compose_yaml

    errors: list[str] = []
    for key, value in _mapping_items(compose_yaml(text)):
        if key.value == "native_plugins" and (raw := _misspelled(text, value)):
            errors.append(f"'native_plugins' must be true or false, got {raw!r}")
        if key.value not in ("dependencies", "devDependencies"):
            continue
        for block, entries in _mapping_items(value):
            if block.value != "apm" or not isinstance(entries, yaml.SequenceNode):
                continue
            for entry in entries.value:
                errors += [
                    f"'native' field must be true or false, got {raw!r}"
                    for field, flag in _mapping_items(entry)
                    if field.value == "native" and (raw := _misspelled(text, flag))
                ]
    return errors


def refuse_route_boolean_spellings(manifest_path: Path) -> None:
    """Raise ``ValueError`` naming the field when a route value is not plain true/false."""
    try:
        text = Path(manifest_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    if errors := route_boolean_spelling_errors(text):
        raise ValueError(f"Invalid {Path(manifest_path).name}: " + "; ".join(errors))


class _RouteView:
    """Only the route choices of a manifest: its default and its ``native`` declarations."""

    def __init__(self, native_plugins: bool | None, declarations: list[Any]) -> None:
        self.native_plugins = native_plugins
        self._declarations = declarations

    def get_all_apm_dependencies(self) -> list[Any]:
        return list(self._declarations)


def _route_entries(data: dict) -> list[dict]:
    """Object-form ``dependencies.apm`` / ``devDependencies.apm`` entries with a ``native`` key."""
    entries = []
    for block in ("dependencies", "devDependencies"):
        section = data.get(block)
        apm = section.get("apm") if isinstance(section, dict) else None
        entries += [e for e in apm or () if isinstance(e, dict) and "native" in e]
    return entries


def load_route_manifest(manifest_path: Path) -> Any:
    """Read only the native route choices of the manifest at *manifest_path* (#4995).

    A replay needs nothing else, so required-field checks such as ``version``
    do not apply here. Everything a route depends on fails closed and raises:
    an unreadable or invalid manifest (duplicate keys, anchors), an invalid
    ``native`` or ``native_plugins`` value, or an object entry carrying
    ``native`` that does not parse. Only a missing file or a root that is not
    a mapping yields ``None`` (the route stays automatic).
    """
    from apm_cli.models.dependency.manifest_host import manifest_host_scope_for
    from apm_cli.models.dependency.object_fields import parse_native_route
    from apm_cli.models.dependency.reference import DependencyReference
    from apm_cli.models.manifest_io import load_manifest

    path = Path(manifest_path)
    if not path.is_file():
        return None
    # The loader's duplicate-key and anchor checks run before the raw scan.
    data = load_manifest(path)
    refuse_route_boolean_spellings(path)
    if not isinstance(data, dict):
        return None
    scope = manifest_host_scope_for(data)
    native_plugins = data.get("native_plugins")
    if "native_plugins" in data and not isinstance(native_plugins, bool):
        raise ValueError(f"Invalid {path.name}: 'native_plugins' must be true or false")
    declarations = []
    with scope:
        for entry in _route_entries(data):
            native = parse_native_route(entry)
            declared = DependencyReference.parse_from_dict(entry)
            declared.native = native
            declarations.append(declared)
    return _RouteView(native_plugins, declarations)
