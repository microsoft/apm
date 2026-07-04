"""Cross-platform YAML I/O with guaranteed UTF-8 encoding.

All YAML file operations in apm_cli should use these helpers to ensure
consistent encoding (UTF-8) and formatting (unicode, block style, key
order preserved).  This prevents silent mojibake on Windows where the
default file encoding is cp1252, not UTF-8.

Public API::

    load_yaml(path)                 -- read a .yml/.yaml file -> dict | None
    dump_yaml(data, path)           -- write dict -> .yml/.yaml file
    load_yaml_roundtrip(path)       -- read YAML while preserving comments
    dump_yaml_roundtrip(data, path) -- write round-trip YAML data
    yaml_to_str(data)               -- serialize dict -> YAML string
"""

import os
import secrets
from contextlib import suppress
from io import StringIO
from pathlib import Path
from typing import Any, NoReturn

import yaml
from frontmatter.default_handlers import YAMLHandler as _FrontmatterYAMLHandler

# Shared defaults matching existing codebase convention.
_DUMP_DEFAULTS: dict[str, Any] = dict(
    default_flow_style=False,
    sort_keys=False,
    allow_unicode=True,
)


class _BoundedSafeLoader(yaml.SafeLoader):
    """SafeLoader that bounds YAML merge-key (``<<``) expansion.

    PyYAML resolves merge keys EAGERLY in ``flatten_mapping``. A linear-size
    document that chains aliased merges (``<<: [*a, *a]`` once per level)
    doubles the merged value-list at each level, driving that resolution to
    O(2^N) work, so a sub-kilobyte ``apm.yml`` can hang the parser for minutes
    -- a CPU DoS reachable at PARSE time, before any post-parse structural
    guard (``_is_fingerprint_safe``) can run, and before the trust gate, so an
    untrusted clone could wedge ``apm install``.

    The stock ``flatten_mapping`` calls itself only O(N) times (it mutates
    each node in place, so a re-referenced alias is cheap on the second
    visit); the cost lives in the ``merge.extend`` copies whose CUMULATIVE
    volume grows like 2^N. So we reimplement ``flatten_mapping`` -- mirroring
    PyYAML 6.x exactly -- and bound (a) the cumulative count of merged entries
    and (b) the merge-recursion depth. A hostile manifest then raises a
    ``yaml.YAMLError`` (which every ``load_yaml`` caller already treats as
    fail-closed) within a small fixed budget instead of hanging. Both budgets
    are orders of magnitude above any legitimate hand-written config, so real
    ``<<`` merges still resolve correctly.

    A sibling vector needs no ``<<`` at all: a PURE-ALIAS billion-laughs graph
    (``lN: &lN [*l(N-1), *l(N-1)]``) is only O(N) shared-reference objects, so
    ``flatten_mapping`` and the merge budget never see it, yet it expands to
    O(2^N) when any consumer materializes the value (``str()``, deepcopy,
    re-serialize). ``construct_document`` therefore first walks the composed
    node graph with a memoized expansion-weight budget (``_guard_expansion``)
    and fails the document closed before construction -- protecting every
    ``load_yaml`` consumer uniformly, including the non-trust
    ``apm lifecycle validate`` / ``test`` paths that never run
    ``_is_fingerprint_safe``.
    """

    _MAX_MERGE_ENTRIES = 100_000
    _MAX_FLATTEN_DEPTH = 200
    _MAX_EXPANSION_WEIGHT = 5_000_000
    _EXPANSION_INPROGRESS = -1

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._merge_entries = 0
        self._flatten_depth = 0

    def _raise_expansion(self, node: Any) -> None:
        raise yaml.constructor.ConstructorError(
            "while constructing a node",
            getattr(node, "start_mark", None),
            "YAML alias/anchor expansion exceeded the safe budget "
            "(possible billion-laughs expansion bomb)",
            getattr(node, "start_mark", None),
        )

    def _guard_expansion(self, root: Any) -> None:
        # Bound the LOGICAL (alias-expanded) size of the composed node graph
        # BEFORE construction. PyYAML shares one node object across every
        # ``*alias`` reference, so a pure-alias billion-laughs graph
        # (``lN: &lN [*l(N-1), *l(N-1)]``) is only O(N) objects yet expands
        # to O(2^N) the moment any consumer materializes it (``str()``,
        # deepcopy, re-serialize). It carries no ``<<`` so the merge-entry
        # budget never engages, and non-trust consumers
        # (``apm lifecycle validate`` / ``test``) never run the post-parse
        # ``_is_fingerprint_safe`` guard -- so without this the bomb wedges
        # them. We compute a memoized per-node expansion weight (shared nodes
        # are walked once but summed per occurrence by each parent) and fail
        # closed as a ``yaml.YAMLError`` the instant the running total crosses
        # the budget. A self-referential anchor (``a: &a [*a]``) is a cycle in
        # the node graph; the in-progress sentinel detects it and fails closed
        # rather than recursing forever. The budget is orders of magnitude
        # above any legitimate config, so real anchors/aliases still resolve.
        #
        # Leaf weight is BYTE-AWARE, not a flat 1: PyYAML's representer reports
        # ``ignore_aliases() == True`` for ``str`` / ``int`` / ``float`` /
        # ``bytes`` / ``bool``, so on the dump side (``dump_yaml`` /
        # ``yaml_to_str``) a shared scalar is NOT re-anchored -- its full text
        # is re-emitted once PER alias occurrence. A single ~50KB anchored
        # scalar aliased tens of thousands of times therefore composes as only
        # O(N) nodes (passing a node-count guard) yet re-serializes to ~GBs and
        # hangs/OOMs the emitter -- reachable pre-trust on the
        # ``apm install`` / ``apm uninstall`` apm.yml round-trip. Charging each
        # scalar occurrence its emitted byte length makes the budget model the
        # real dump-amplification cost, so the bomb fails closed at parse while
        # a single large scalar (referenced a handful of times) still resolves.
        weights: dict[int, int] = {}

        def weight(node: Any) -> int:
            nid = id(node)
            cached = weights.get(nid)
            if cached is not None:
                if cached == self._EXPANSION_INPROGRESS:
                    self._raise_expansion(node)
                return cached
            weights[nid] = self._EXPANSION_INPROGRESS
            if isinstance(node, yaml.nodes.MappingNode):
                total = 1
                for key_node, value_node in node.value:
                    total += weight(key_node) + weight(value_node)
                    if total > self._MAX_EXPANSION_WEIGHT:
                        self._raise_expansion(node)
            elif isinstance(node, yaml.nodes.SequenceNode):
                total = 1
                for child in node.value:
                    total += weight(child)
                    if total > self._MAX_EXPANSION_WEIGHT:
                        self._raise_expansion(node)
            else:
                total = self._leaf_byte_cost(node)
                if total > self._MAX_EXPANSION_WEIGHT:
                    self._raise_expansion(node)
            weights[nid] = total
            return total

        weight(root)

    @staticmethod
    def _leaf_byte_cost(node: Any) -> int:
        # Emitted-size proxy for a scalar leaf: the length of its source text.
        # ScalarNode.value is the raw scalar string PyYAML will re-emit, so a
        # huge int written as a long decimal / hex literal, or a multi-KB
        # block scalar, is charged its true re-serialization cost. Floor at 1
        # so an empty scalar still counts as one node.
        value = getattr(node, "value", "")
        try:
            return max(1, len(value))
        except TypeError:
            return 1

    def construct_document(self, node: Any) -> Any:
        # Fail closed on an alias/anchor expansion bomb before the stock
        # constructor materializes the shared-reference graph.
        self._guard_expansion(node)
        return super().construct_document(node)

    def _merge_budget_guard(self, node: Any) -> None:
        if (
            self._merge_entries > self._MAX_MERGE_ENTRIES
            or self._flatten_depth > self._MAX_FLATTEN_DEPTH
        ):
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                getattr(node, "start_mark", None),
                "YAML merge-key expansion exceeded the safe budget "
                "(possible merge-key expansion bomb)",
                getattr(node, "start_mark", None),
            )

    def flatten_mapping(self, node: Any) -> Any:
        # Faithful reimplementation of PyYAML 6.x
        # ``SafeConstructor.flatten_mapping`` with a cumulative merged-entry
        # budget + depth guard woven into the recursion. Keep the control flow
        # identical to upstream so legitimate merges resolve identically.
        self._flatten_depth += 1
        try:
            self._merge_budget_guard(node)
            merge = []
            index = 0
            while index < len(node.value):
                key_node, value_node = node.value[index]
                if key_node.tag == "tag:yaml.org,2002:merge":
                    del node.value[index]
                    if isinstance(value_node, yaml.nodes.MappingNode):
                        self.flatten_mapping(value_node)
                        merge.extend(value_node.value)
                    elif isinstance(value_node, yaml.nodes.SequenceNode):
                        submerge = []
                        for subnode in value_node.value:
                            if not isinstance(subnode, yaml.nodes.MappingNode):
                                raise yaml.constructor.ConstructorError(
                                    "while constructing a mapping",
                                    node.start_mark,
                                    f"expected a mapping for merging, but found {subnode.id}",
                                    subnode.start_mark,
                                )
                            self.flatten_mapping(subnode)
                            submerge.append(subnode.value)
                        submerge.reverse()
                        for value in submerge:
                            merge.extend(value)
                    else:
                        raise yaml.constructor.ConstructorError(
                            "while constructing a mapping",
                            node.start_mark,
                            "expected a mapping or list of mappings for "
                            f"merging, but found {value_node.id}",
                            value_node.start_mark,
                        )
                    self._merge_entries += len(merge)
                    self._merge_budget_guard(node)
                elif key_node.tag == "tag:yaml.org,2002:value":
                    key_node.tag = "tag:yaml.org,2002:str"
                    index += 1
                else:
                    index += 1
            if merge:
                node.value = merge + node.value
        finally:
            self._flatten_depth -= 1


def _bounded_load(stream: Any) -> Any:
    """Parse *stream* with the merge-bounded SafeLoader, failing closed.

    Centralizes the round-16 bounded-loader entrypoint AND normalizes the
    non-``YAMLError`` failure modes the bounded loader can still surface into a
    ``yaml.YAMLError`` so every caller's fail-closed ``except yaml.YAMLError``
    catches them as one class:

    * a huge decimal-int scalar (``bignum: <6000 digits>``) reaches CPython's
      ``int()`` and raises ``ValueError`` past ``sys.int_max_str_digits``;
    * a deeply-nested document raises ``RecursionError`` during construction.

    Without this, those escape the integrators' ``except yaml.YAMLError``
    wrappers and abort a whole ``apm audit`` drift replay (one hostile
    ``.prompt.md`` -> whole-run DoS) instead of the intended per-file skip.
    """
    try:
        return yaml.load(stream, Loader=_BoundedSafeLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError:
        raise
    except (ValueError, RecursionError) as exc:
        raise yaml.YAMLError(f"bounded YAML parse failed: {type(exc).__name__}: {exc}") from exc


def load_yaml(path: str | Path) -> dict[str, Any] | None:
    """Load a YAML file with explicit UTF-8 encoding.

    Returns parsed data or ``None`` for empty files.
    Raises ``FileNotFoundError`` or ``yaml.YAMLError`` on failure.

    Uses a merge-bounded SafeLoader so a hostile manifest cannot wedge the
    parser via an eager ``<<`` merge-key expansion (see ``_BoundedSafeLoader``);
    the bomb -- and a huge-int / deep-nest scalar -- fails closed as a
    ``yaml.YAMLError`` (see ``_bounded_load``) instead of hanging or escaping.
    """
    with open(path, encoding="utf-8") as fh:
        return _bounded_load(fh)


def load_yaml_str(text: str) -> dict[str, Any] | None:
    """Load YAML from an in-memory string with the bounded SafeLoader.

    The string-input twin of :func:`load_yaml`, for callers that already hold
    the YAML text rather than a path: a lockfile body (``LockFile.from_yaml``),
    a local bundle's ``apm.lock.yaml``, packed bundle metadata, or an installed
    package's ``.md`` frontmatter. Routing those through ``_BoundedSafeLoader``
    (instead of stock ``yaml.safe_load``) means a hostile string cannot wedge
    the parser via an eager ``<<`` merge-key expansion; the bomb fails closed as
    a ``yaml.YAMLError`` instead of hanging the caller in an uncatchable
    ``O(2^N)`` construction loop.

    Returns parsed data or ``None`` for empty input. Raises ``yaml.YAMLError``
    on malformed, over-budget, huge-int, or deeply-nested input.
    """
    return _bounded_load(text)


def _roundtrip_yaml() -> Any:
    """Return a configured ruamel.yaml round-trip parser."""
    from ruamel.yaml import YAML
    from ruamel.yaml.constructor import ConstructorError

    def reject_python_tag(_constructor: Any, tag_suffix: str, node: Any) -> NoReturn:
        raise ConstructorError(
            None,
            None,
            f"forbidden Python YAML tag: {tag_suffix}",
            node.start_mark,
        )

    rt = YAML(typ="rt")
    # Class-level constructor registration is intentional: every round-trip
    # YAML instance in this process should reject unsafe Python tags.
    rt.Constructor.add_multi_constructor("tag:yaml.org,2002:python/", reject_python_tag)
    rt.preserve_quotes = True
    rt.indent(mapping=2, sequence=4, offset=2)
    return rt


def _raise_as_pyyaml_error(exc: Exception) -> NoReturn:
    """Normalize ruamel parser failures to the yaml.YAMLError family."""
    from ruamel.yaml import YAMLError as RuamelYAMLError

    if isinstance(exc, RuamelYAMLError):
        raise yaml.YAMLError(f"YAML parse failed: {exc}") from exc
    raise exc


def load_yaml_roundtrip(path: str | Path) -> Any:
    """Load YAML while preserving comments and formatting metadata.

    The document is first parsed by the bounded PyYAML loader so the manifest
    update paths keep the same alias and merge-key safety budget as
    :func:`load_yaml`. The original text is then parsed with ruamel.yaml
    round-trip mode so callers can mutate the returned object and write it back
    without stripping comments.
    """
    text = Path(path).read_text(encoding="utf-8")
    _bounded_load(text)
    try:
        return _roundtrip_yaml().load(text)
    except Exception as exc:
        _raise_as_pyyaml_error(exc)


def dump_yaml_roundtrip(data: Any, path: str | Path) -> None:
    """Write ruamel round-trip YAML data with explicit UTF-8 encoding."""
    stream = StringIO()
    try:
        _roundtrip_yaml().dump(data, stream)
    except Exception as exc:
        _raise_as_pyyaml_error(exc)
    text = stream.getvalue()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class _BoundedYAMLHandler(_FrontmatterYAMLHandler):
    """python-frontmatter YAML handler bound to ``_BoundedSafeLoader``.

    The stock ``YAMLHandler.load`` defaults to ``yaml.SafeLoader``, which has
    no merge / alias / expansion budget. Every ``frontmatter.load`` call in
    apm_cli (the primitive parsers, the install + audit-replay integrators,
    skill validation, the packer) reads an UNTRUSTED installed package's
    ``.md`` frontmatter, so a sub-kilobyte merge-key or pure-alias
    billion-laughs bomb in that frontmatter wedges the parser in an
    uncatchable ``O(2^N)`` construction loop -- a CPU DoS the integrators'
    ``except`` clauses cannot preempt because the loop holds the GIL and never
    yields. ``load_yaml`` / ``load_yaml_str`` already route their callers
    through ``_BoundedSafeLoader``; this handler closes the SAME class for the
    ``python-frontmatter`` entrypoint so the bomb fails closed as a
    ``yaml.YAMLError`` within the fixed budget instead of hanging
    ``apm install`` / ``apm audit``.
    """

    def load(self, fm: str, **kwargs: Any) -> Any:
        kwargs["Loader"] = _BoundedSafeLoader
        try:
            return yaml.load(fm, **kwargs)  # noqa: S506 - SafeLoader subclass
        except yaml.YAMLError:
            raise
        except (ValueError, RecursionError) as exc:
            raise yaml.YAMLError(
                f"bounded frontmatter parse failed: {type(exc).__name__}: {exc}"
            ) from exc


_BOUNDED_FRONTMATTER_HANDLER = _BoundedYAMLHandler()


def load_frontmatter(fd: Any, encoding: str = "utf-8") -> Any:
    """Parse Markdown front matter with the bounded YAML loader.

    Drop-in for ``frontmatter.load(fd)``: accepts a path string or an open
    file object and splits the ``---``-fenced YAML front matter, but parses
    that block with ``_BoundedSafeLoader`` (see ``_BoundedYAMLHandler``) so an
    untrusted installed package's hostile ``.md`` front matter cannot hang the
    parser via an eager ``<<`` merge or a pure-alias expansion bomb. Returns
    the same ``frontmatter.Post`` (``.metadata`` / ``.content``) as the stock
    call; raises ``yaml.YAMLError`` on malformed or over-budget front matter,
    which every existing caller already treats as fail-closed.
    """
    import frontmatter

    return frontmatter.load(fd, encoding=encoding, handler=_BOUNDED_FRONTMATTER_HANDLER)


def dump_yaml(
    data: Any,
    path: str | Path,
    *,
    sort_keys: bool = False,
) -> None:
    """Write data to a YAML file with UTF-8 encoding and unicode support.

    Serializes to a string FIRST, then opens the destination file. A
    representer or value error (for example an integer whose decimal form
    exceeds CPython's ``int_max_str_digits`` limit -- reachable via a hex or
    octal literal that ``safe_load`` materialised without a digit cap) is
    therefore raised BEFORE the file is opened, so an unserialisable payload
    can never truncate the existing file to zero bytes.
    """
    text = yaml.safe_dump(data, **{**_DUMP_DEFAULTS, "sort_keys": sort_keys})
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def yaml_to_str(data: Any, *, sort_keys: bool = False) -> str:
    """Serialize data to a YAML string with unicode support.

    Use instead of bare ``yaml.dump()`` when building YAML content
    for later file writes or string returns.
    """
    return yaml.safe_dump(data, **{**_DUMP_DEFAULTS, "sort_keys": sort_keys})


def write_yaml_text_atomic(
    path: str | Path,
    content: str,
    *,
    tmp_suffix: str = ".tmp",
) -> None:
    """Atomically replace a YAML file with already-rendered text.

    The replacement is written to a sibling file first and then moved into
    place with ``os.replace``. If the write or replace fails, the original
    file remains untouched.
    """
    target = Path(path)
    tmp_path: Path | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        for _attempt in range(10):
            candidate = target.with_name(f".{target.name}.{secrets.token_hex(8)}{tmp_suffix}")
            try:
                fd = os.open(candidate, flags, 0o600)
            except FileExistsError:
                continue
            tmp_path = candidate
            break
        else:
            raise FileExistsError(f"Could not create a unique temp file for {target}")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, target)
        tmp_path = None
    except Exception:
        if tmp_path is not None:
            with suppress(OSError):
                tmp_path.unlink()
        raise
