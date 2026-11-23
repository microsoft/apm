"""Top-level ``apm.lock.yaml`` container validation (split from ``deps/lockfile.py``).

Checks the version and the shape of every top-level list and mapping before
``LockFile`` construction; the read path in ``deps/lockfile.py`` calls it.
"""

from __future__ import annotations

import copy
import threading
from collections import OrderedDict
from typing import Any


def records_mcp_target_servers(lock: Any) -> bool:
    """Whether *lock* writes ``mcp_target_servers``.

    A non-empty map is always written. An explicit empty map is written only
    beside MCP servers, where it differs from a legacy-absent one; with no
    server left it is omitted, as a fresh deploy omits it (#4852).
    """
    return bool(lock.mcp_target_servers) or (
        lock._mcp_target_servers_present and bool(lock.mcp_servers)
    )


# One install reads the same lockfile text many times (about 19 call sites).
# The bounded YAML parse is a pure function of that text, so its result is
# memoized by exact content; every caller still gets a private copy and still
# runs container validation and construction.
_PARSED_TEXT_LIMIT = 4
_parsed_texts: OrderedDict[tuple[object, str], object] = OrderedDict()
_parsed_texts_lock = threading.Lock()


def parse_key(yaml_str: str) -> tuple[object, str]:
    """The memo key of *yaml_str*: its loader and exact text.

    The one place the key is built, so anything that seeds the memo reads
    back with the loader ``parse_lockfile_text`` uses (a substituted loader
    never reads another loader's parse).
    """
    from ..utils import yaml_io

    return (yaml_io.load_machine_yaml_str, yaml_str)


def parse_lockfile_text(yaml_str: str) -> object:
    """Return a private copy of the bounded YAML parse of *yaml_str*.

    Raises whatever ``load_machine_yaml_str`` raises; failed parses are not memoized.
    """
    from ..utils import yaml_io

    key = parse_key(yaml_str)
    with _parsed_texts_lock:
        parsed = _parsed_texts.get(key, _parsed_texts)
        if parsed is not _parsed_texts:
            _parsed_texts.move_to_end(key)
    if parsed is _parsed_texts:
        parsed = yaml_io.load_machine_yaml_str(yaml_str)
        with _parsed_texts_lock:
            _parsed_texts[key] = parsed
            while len(_parsed_texts) > _PARSED_TEXT_LIMIT:
                _parsed_texts.popitem(last=False)
    if "&" not in yaml_str and "*" not in yaml_str:
        # No anchor or alias: the tree shares no node, so a plain copy equals deepcopy.
        return yaml_io.copy_plain_tree(parsed)
    try:
        return copy.deepcopy(parsed)
    except RecursionError:
        # A merge-reordered alias chain can nest deeper than deepcopy recurses
        # though the bounded loader accepted it; such text is parsed per read.
        with _parsed_texts_lock:
            _parsed_texts.pop(key, None)
        return yaml_io.load_machine_yaml_str(yaml_str)


_NOT_PLAIN = object()
_YAML_LINE_BREAKS = ("\x85", "\u2028", "\u2029")
_PLAIN_SCALARS = (str, int, bool, type(None))


def _plain_copy(value: object) -> object:
    """A private copy of *value* when it holds only what a safe dump round-trips.

    Exact ``dict`` (``str`` keys), ``list``, ``str``, ``int``, ``bool`` and
    ``None``; anything else (floats, subclasses, tuples) is ``_NOT_PLAIN``.
    """
    kind = type(value)
    if kind in _PLAIN_SCALARS:
        return value
    if kind is list:
        items = [_plain_copy(item) for item in value]
        return _NOT_PLAIN if any(item is _NOT_PLAIN for item in items) else items
    if kind is dict:
        copied = {}
        for key, item in value.items():
            if type(key) is not str:
                return _NOT_PLAIN
            item = _plain_copy(item)
            if item is _NOT_PLAIN:
                return _NOT_PLAIN
            copied[key] = item
        return copied
    return _NOT_PLAIN


def remember_lockfile_text(yaml_str: str, document: object) -> None:
    """Record *document* as the parse of *yaml_str*, the text dumped from it.

    ``LockFile.write`` serializes plain built-ins with the safe dumper, which
    loads back to an equal document in the same key order, so a later read of
    the text it staged or wrote needs no YAML parse. A document holding any
    other type is not recorded and its text is parsed on first read.
    """
    from ..utils import yaml_io

    if not yaml_io.machine_yaml_admits(yaml_str):
        return  # a read must refuse it exactly as a fresh parse would
    if any(brk in yaml_str for brk in _YAML_LINE_BREAKS):
        # allow_unicode emits NEL, LS and PS raw inside scalars and a load
        # normalizes them, so such text would not read back as the document.
        return
    plain = _plain_copy(document)
    if plain is _NOT_PLAIN:
        return
    key = parse_key(yaml_str)
    with _parsed_texts_lock:
        _parsed_texts[key] = plain
        _parsed_texts.move_to_end(key)
        while len(_parsed_texts) > _PARSED_TEXT_LIMIT:
            _parsed_texts.popitem(last=False)


def validate_lockfile_container(data: object) -> dict[str, Any]:
    """Validate version and top-level container shapes before construction."""
    from .lockfile import LockfileFormatError, require_supported_lockfile_version

    if not isinstance(data, dict):
        raise LockfileFormatError("Lockfile root must be a mapping")
    data = dict(data)
    # Pre-versioned lockfiles are a supported legacy v1 migration input.
    # Explicit unknown/newer versions still fail closed here.
    data["lockfile_version"] = require_supported_lockfile_version(data)
    list_fields = (
        "dependencies",
        "deployments",
        "mcp_servers",
        "lsp_servers",
        "local_deployed_files",
        "local_staged_files",
    )
    mapping_fields = (
        "mcp_configs",
        "mcp_target_servers",
        "mcp_config_provenance",
        "mcp_omissions",
        "lsp_configs",
        "lsp_target_servers",
        "lsp_config_provenance",
        "local_deployed_file_hashes",
        "local_staged_file_hashes",
    )
    for field_name in list_fields:
        if field_name in data and data[field_name] is None:
            data[field_name] = []
        elif field_name in data and not isinstance(data[field_name], list):
            raise LockfileFormatError(f"Lockfile field {field_name!r} must be a list")
    for field_name in mapping_fields:
        if field_name in data and data[field_name] is None:
            data[field_name] = {}
        elif field_name in data and not isinstance(data[field_name], dict):
            raise LockfileFormatError(f"Lockfile field {field_name!r} must be a mapping")
    for index, dependency in enumerate(data.get("dependencies", [])):
        if not isinstance(dependency, dict):
            raise LockfileFormatError(f"Lockfile dependency at index {index} must be a mapping")
    if any(not isinstance(name, str) or not name for name in (data.get("mcp_omissions") or {})):
        raise LockfileFormatError(
            "MCP omission evidence has invalid server names; selection is unknown. "
            "Restore a known-good lockfile and repeat the intended install selection"
        )
    for field_name in ("mcp_target_servers", "lsp_target_servers"):
        for target, servers in (data.get(field_name) or {}).items():
            if not isinstance(target, str) or not target or not isinstance(servers, list):
                raise LockfileFormatError(
                    f"Lockfile {field_name} values must be string-to-list mappings"
                )
            if not all(isinstance(server, str) and bool(server) for server in servers):
                raise LockfileFormatError(f"Lockfile {field_name} entries must be strings")
    for server, provenance in (data.get("mcp_config_provenance") or {}).items():
        if not isinstance(server, str) or not (
            (isinstance(provenance, str) and bool(provenance))
            or (
                isinstance(provenance, list)
                and bool(provenance)
                and all(isinstance(owner, str) and bool(owner) for owner in provenance)
            )
        ):
            raise LockfileFormatError(
                "Lockfile mcp_config_provenance values must be strings or string lists"
            )
    if "deployments" in data:
        from ..core.deployment_ledger import DeploymentLedgerCodec

        try:
            DeploymentLedgerCodec.validate_rows(data["deployments"])
        except ValueError as exc:
            raise LockfileFormatError(str(exc)) from exc
    return data
