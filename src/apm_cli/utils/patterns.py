"""Shared helpers for working with primitive ``applyTo`` patterns.

The ``applyTo`` frontmatter on instruction primitives is documented as a
glob OR a comma-separated list of globs.  This module owns the canonical
parse so converters and the placement optimizer behave consistently.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

import yaml

from apm_cli.utils.yaml_io import load_yaml_str

_APPLY_TO_ESCAPE = "\\"
_APPLY_TO_SEPARATOR = ","
_ESCAPABLE_APPLY_TO_CHARS = frozenset({_APPLY_TO_SEPARATOR, _APPLY_TO_ESCAPE})
_GLOB_META_CHARACTERS = frozenset({"*", "?", "[", "{"})


class _ApplyToPattern(str):
    """A parsed pattern that remembers an escaped top-level comma."""

    _escaped_top_level_comma: bool

    def __new__(cls, value: str, escaped_top_level_comma: bool):
        instance = super().__new__(cls, value)
        instance._escaped_top_level_comma = escaped_top_level_comma
        return instance

    def strip(self, chars: str | None = None) -> _ApplyToPattern:
        """Preserve comma-boundary metadata while trimming a pattern."""
        return type(self)(super().strip(chars), self._escaped_top_level_comma)


def has_top_level_comma(pattern: str) -> bool:
    """Return True if ``pattern`` contains a comma outside any ``{...}`` group.

    Commas inside brace alternation (e.g. ``**/*.{css,scss}``) are part
    of glob brace expansion and must not be treated as list separators.
    Single source of truth for the comma-vs-brace discrimination; the
    placement optimizer and the integrators both consume this so the
    semantics of ``parse_apply_to`` and its callers stay in lock-step.
    """
    if getattr(pattern, "_escaped_top_level_comma", False):
        return False

    depth = 0
    in_character_class = False
    index = 0
    while index < len(pattern):
        ch = pattern[index]
        if (
            ch == _APPLY_TO_ESCAPE
            and index + 1 < len(pattern)
            and pattern[index + 1] in _ESCAPABLE_APPLY_TO_CHARS
        ):
            index += 2
            continue
        if in_character_class:
            if ch == "]":
                in_character_class = False
        elif ch == "[":
            in_character_class = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
        elif ch == _APPLY_TO_SEPARATOR and depth == 0:
            return True
        index += 1
    return False


def yaml_double_quote(value: str) -> str:
    """Escape ``value`` for embedding inside a YAML double-quoted scalar.

    Defence-in-depth for the instruction integrators that emit YAML
    frontmatter via f-strings (``f'  - "{g}"'``). A glob containing a
    literal backslash, double-quote, or control character would break
    the surrounding YAML if inlined verbatim. JSON string serialization
    is a strict subset of YAML double-quoted scalar syntax and covers every
    control character. Returns the value wrapped in double quotes.

    Note: ``parse_apply_to`` already strips leading/trailing whitespace
    per segment, and the Windsurf integrator strips newlines from the
    raw frontmatter value before splitting, so the practical risk today
    is near-zero -- this exists so emitted YAML stays well-formed even
    on adversarial or copy-paste-mangled inputs.
    """
    return json.dumps(value, ensure_ascii=True)


def yaml_plain_scalar(value: str) -> str:
    """Render ``value`` in the least-quoted valid YAML scalar form.

    Cursor's own ``.mdc`` frontmatter examples
    (cursor.com/docs/context/rules) never show quoted ``globs`` or
    ``description`` values -- every documented example is a bare plain
    scalar. This prefers that form when it round-trips losslessly
    through YAML's own resolver, falls back to a single-quoted scalar
    (still no backslash escaping) when the value merely needs
    delimiting, and only resorts to a double-quoted scalar for values
    neither can represent, such as one containing a newline. Unlike
    :func:`yaml_double_quote`, this never forces ``\\uXXXX`` escaping of
    printable non-ASCII text -- the escaping there is ASCII-only
    defence-in-depth for targets that always quote; here, the bare and
    single-quoted forms already avoid backslash escapes entirely, so
    the double-quoted fallback keeps UTF-8 literal for the same reason.

    Introduced for the Cursor target (issue #3002); other converters
    keep their existing always-double-quoted behavior via
    :func:`yaml_double_quote`.
    """
    if not _has_yaml_line_break_like_char(value):
        if _yaml_scalar_round_trips(value, value):
            return value
        single_quoted = "'" + value.replace("'", "''") + "'"
        if _yaml_scalar_round_trips(single_quoted, value):
            return single_quoted
    return _yaml_double_quote_utf8_safe(value)


def _yaml_scalar_round_trips(candidate: str, expected: str) -> bool:
    """Return True if parsing ``candidate`` as a bare YAML scalar yields ``expected``.

    ``description``/``globs`` values originate in an installed package's
    frontmatter -- untrusted content. Routed through the same
    ``_BoundedSafeLoader`` every other untrusted-YAML entry point in this
    codebase uses (:func:`apm_cli.utils.yaml_io.load_yaml_str`), not stock
    ``yaml.safe_load``: a description whose literal text happens to be a
    YAML alias/merge-key expansion bomb would otherwise bypass that guard
    on this second, in-memory parse even though the original frontmatter
    parse was already bounded (issue #2389's attack class).
    """
    try:
        return load_yaml_str(candidate) == expected
    except yaml.YAMLError:
        return False


# YAML 1.1 treats these as line breaks (b-char) or excludes them from the
# printable set (c-printable) entirely, so a double-quoted scalar produced
# by ``json.dumps(..., ensure_ascii=False)`` -- which only escapes
# U+0000-U+001F per the JSON spec -- can silently change on a YAML
# round-trip (NEL/LS/PS folded as a line break) or fail to parse at all
# (DEL, U+007F, isn't in YAML 1.1's printable character set).
_YAML_UNSAFE_UTF8_CODEPOINTS = ("\x7f", "\u0085", "\u2028", "\u2029")

# Superset of the above plus \n/\r: any of these must skip straight to the
# double-quoted fallback rather than attempt the bare/single-quoted round
# trip. PyYAML folds *any* line-break-like character (not just \n/\r) when
# scanning a multi-line plain or single-quoted scalar, so a value containing
# e.g. U+2028 immediately followed by \n can spuriously round-trip as
# "equal to itself" -- the folding of the two adjacent breaks reproduces the
# original text by coincidence, even though the emitted scalar is not a
# faithful, parser-independent representation of ``value``.
_YAML_LINE_BREAK_LIKE_CHARS = ("\n", "\r", *_YAML_UNSAFE_UTF8_CODEPOINTS)

# A lone (unpaired) UTF-16 surrogate is not "printable non-ASCII text" -- it
# cannot be UTF-8 encoded at all, so leaving one literal in the emitted
# scalar doesn't just violate YAML's c-printable set, it crashes the file
# write outright the moment the target's frontmatter integration touches
# disk. This is also the project's existing hidden-unicode security-gate
# attack shape (a description escaped as e.g. ``\uDB40\uDC01``), so it must
# fall back to the JSON-escaped double-quoted form even under ``--force``.
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def _has_lone_surrogate(value: str) -> bool:
    return bool(_LONE_SURROGATE_RE.search(value))


def _has_yaml_line_break_like_char(value: str) -> bool:
    return any(ch in value for ch in _YAML_LINE_BREAK_LIKE_CHARS) or _has_lone_surrogate(value)


def _yaml_double_quote_utf8_safe(value: str) -> str:
    """Double-quote ``value``, keeping printable non-ASCII literal.

    Like :func:`yaml_double_quote` but with ``ensure_ascii=False`` --
    except for the handful of codepoints YAML 1.1 treats as a line break
    or excludes from double-quoted scalars outright, and any lone UTF-16
    surrogate, all of which still need a JSON-style ``\\uXXXX`` escape
    despite being outside JSON's own required control-character range.
    """
    encoded = json.dumps(value, ensure_ascii=False)
    for codepoint in _YAML_UNSAFE_UTF8_CODEPOINTS:
        if codepoint in encoded:
            encoded = encoded.replace(codepoint, f"\\u{ord(codepoint):04x}")
    encoded = _LONE_SURROGATE_RE.sub(lambda m: f"\\u{ord(m.group()):04x}", encoded)
    return encoded


def yaml_globs_scalar(value: str) -> str:
    """Render a Cursor ``globs`` value exactly as Cursor's docs show it: bare.

    Every ``globs`` example in Cursor's docs (cursor.com/docs/context/rules)
    is unquoted, including patterns starting with ``**`` -- a bare leading
    ``*`` is technically a YAML alias indicator under a strict parser (which
    is why :func:`yaml_plain_scalar`'s round-trip check falls back to
    quoting it), but Cursor's own frontmatter reader tolerates it, and glob
    syntax has no legitimate use for the ``: ``/reserved-word ambiguities
    that check also guards against. So globs get a simpler, more permissive
    rule than free-form text: always bare, unless the value contains a
    literal newline (or another character YAML treats as a line break,
    e.g. U+2028) which would inject extra lines into the single-line
    frontmatter block no matter how lenient the reader is.
    """
    if _has_yaml_line_break_like_char(value):
        return _yaml_double_quote_utf8_safe(value)
    return value


def normalize_apply_to(value: object, default: str = "") -> str:
    """Normalize scalar or YAML-list ``applyTo`` values into one OR expression.

    Compilation stores ``applyTo`` as a string. YAML sequences therefore use
    the documented top-level-comma representation consumed by
    :func:`parse_apply_to`, preserving every non-null list entry.
    """
    if isinstance(value, list):
        patterns = []
        for pattern in value:
            if pattern is None:
                continue
            normalized = str(pattern).strip()
            if normalized:
                patterns.append(escape_apply_to_segment(normalized))
        return ",".join(patterns) if patterns else default
    if value is None:
        return default
    return str(value)


def escape_apply_to_segment(pattern: str) -> str:
    """Encode one already-resolved pattern so top-level commas/backslashes
    round-trip losslessly back through :func:`parse_apply_to`.

    Used both to re-encode a YAML-list ``applyTo`` into the single
    comma-separated OR expression (:func:`normalize_apply_to`) and to
    re-join :func:`parse_apply_to`'s output for a target whose native
    format has no list syntax and must comma-join multiple globs into one
    scalar (Cursor's ``globs:``, issue #3002) -- without this, a glob
    that legitimately contains a literal comma (escaped by the author as
    ``\\,``) would become indistinguishable from two separate globs once
    rejoined.
    """
    escaped: list[str] = []
    depth = 0
    in_character_class = False
    for char in pattern:
        if in_character_class:
            escaped.append(char)
            if char == "]":
                in_character_class = False
        elif char == _APPLY_TO_ESCAPE:
            escaped.append(_APPLY_TO_ESCAPE * 2)
        elif char == "[":
            in_character_class = True
            escaped.append(char)
        elif char == "{":
            depth += 1
            escaped.append(char)
        elif char == "}":
            if depth > 0:
                depth -= 1
            escaped.append(char)
        elif char == _APPLY_TO_SEPARATOR and depth == 0:
            escaped.append(_APPLY_TO_ESCAPE + _APPLY_TO_SEPARATOR)
        else:
            escaped.append(char)
    return "".join(escaped)


def parse_apply_to(value: str | None) -> list[str]:
    """Split a primitive ``applyTo`` value into individual glob patterns.

    The input is either a single glob (``"**/*.py"``) or a
    comma-separated list (``"**/src/**,**/api/**"``).  Each segment is
    stripped of surrounding whitespace; empty segments are discarded so
    leading, trailing, doubled-up, and lone commas are tolerated.

    Commas inside brace alternation (``{a,b}``) are NOT separators -- only
    top-level commas split the list.  So ``"**/*.{css,scss},**/*.py"``
    yields ``["**/*.{css,scss}", "**/*.py"]``.

    Returns an empty list for ``None``, empty, or whitespace-only input.
    """
    if not value:
        return []
    segments: list[_ApplyToPattern] = []
    depth = 0
    in_character_class = False
    current: list[str] = []
    escaped_top_level_comma = False

    def append_current() -> None:
        segments.append(_ApplyToPattern("".join(current), escaped_top_level_comma))

    index = 0
    while index < len(value):
        char = value[index]
        if (
            char == _APPLY_TO_ESCAPE
            and index + 1 < len(value)
            and value[index + 1] in _ESCAPABLE_APPLY_TO_CHARS
        ):
            current.append(value[index + 1])
            escaped_top_level_comma = (
                escaped_top_level_comma or value[index + 1] == _APPLY_TO_SEPARATOR
            )
            index += 2
            continue
        if in_character_class:
            current.append(char)
            if char == "]":
                in_character_class = False
        elif char == "[":
            in_character_class = True
            current.append(char)
        elif char == "{":
            depth += 1
            current.append(char)
        elif char == "}":
            if depth > 0:
                depth -= 1
            current.append(char)
        elif char == _APPLY_TO_SEPARATOR and depth == 0:
            append_current()
            current = []
            escaped_top_level_comma = False
        else:
            current.append(char)
        index += 1
    append_current()
    return [segment for segment in (s.strip() for s in segments) if segment]


def literal_apply_to_top_level_roots(
    apply_to_values: Iterable[str | None],
) -> frozenset[str] | None:
    """Return provable literal roots for a batch of ``applyTo`` expressions.

    ``None`` means a root-restricted scan could omit a matching file and callers
    must retain their full traversal. Each expression must contain one or more
    scoped patterns, whose first path component is literal. The returned roots
    are the union across comma-separated expressions.
    """
    roots: set[str] = set()

    for apply_to in apply_to_values:
        if (
            not apply_to
            or not apply_to.strip()
            or _APPLY_TO_ESCAPE in apply_to
            or not _has_balanced_glob_groups(apply_to)
        ):
            return None

        patterns = parse_apply_to(apply_to)
        if not patterns:
            return None

        for pattern in patterns:
            root = _literal_top_level_root(pattern)
            if root is None:
                return None
            roots.add(root)

    return frozenset(roots) if roots else None


def _has_balanced_glob_groups(pattern: str) -> bool:
    """Return whether brace and character-class delimiters are balanced."""
    brace_depth = 0
    in_character_class = False

    for character in pattern:
        if in_character_class:
            if character == "]":
                in_character_class = False
            continue
        if character == "[":
            in_character_class = True
        elif character == "{":
            brace_depth += 1
        elif character == "}":
            if brace_depth == 0:
                return False
            brace_depth -= 1
        elif character == "]":
            return False

    return brace_depth == 0 and not in_character_class


def _literal_top_level_root(pattern: str) -> str | None:
    """Return a pattern's literal first directory component, if provable."""
    normalized = pattern
    while normalized.startswith("./"):
        normalized = normalized[2:]

    first, separator, _ = normalized.partition("/")
    if (
        not separator
        or not first
        or first in {".", ".."}
        or any(character in _GLOB_META_CHARACTERS for character in first)
    ):
        return None
    return first
