"""Offline SPDX declared-license classifier.

Decides how a *declared* license string is represented in an SBOM. This module
NEVER validates against a network, never reads LICENSE file text, and never
gates: an unrecognized string is rendered as a named license, not rejected.

Classification feeds the SBOM ``licenses`` representation only:

* a single recognized SPDX identifier  -> ``KIND_ID``      (CycloneDX ``license.id``)
* a structurally valid SPDX expression -> ``KIND_EXPRESSION`` (CycloneDX ``expression``)
* anything else (``UNLICENSED``, ``SEE LICENSE IN ...``, unknown strings)
                                        -> ``KIND_NAMED``    (CycloneDX ``license.name``)

The distinction between ``KIND_NAMED`` (a recorded assertion) and an *absent*
declaration (NOASSERTION, handled by the caller) is supply-chain critical and
must never be collapsed: ``UNLICENSED`` asserts "no rights granted"; it is not
the same as "unknown".
"""

from __future__ import annotations

from dataclasses import dataclass

from .spdx_data import SPDX_EXCEPTION_IDS, SPDX_LICENSE_IDS

KIND_ID = "id"
KIND_EXPRESSION = "expression"
KIND_NAMED = "named"

_OPERATORS = {"AND", "OR"}


@dataclass(frozen=True)
class LicenseClass:
    """Result of classifying a declared license string.

    ``value`` is the verbatim (whitespace-stripped) declared string -- APM
    records exactly what was declared, never a normalized or concluded form.
    """

    kind: str
    value: str


def _is_license_ref(token: str) -> bool:
    """Whether *token* is an SPDX document-local license reference."""
    return token.startswith("LicenseRef-") or token.startswith("DocumentRef-")


def _is_valid_license_id(token: str) -> bool:
    """Whether *token* is a recognized SPDX id (allowing a trailing ``+``).

    Lookup is case-SENSITIVE by design: SPDX ids have canonical casing (``MIT``,
    ``Apache-2.0``), and the SBOM id field must carry a canonical identifier.
    A non-canonical casing (``mit``) therefore falls through to ``KIND_NAMED``
    and is recorded verbatim -- honest about what was declared rather than
    asserting a canonical id we cannot vouch for.
    """
    bare = token[:-1] if token.endswith("+") else token
    return bool(bare) and (bare in SPDX_LICENSE_IDS or _is_license_ref(token))


def _tokenize(text: str) -> list[str]:
    """Split an SPDX expression into tokens, treating parentheses as tokens."""
    return text.replace("(", " ( ").replace(")", " ) ").split()


class _ExpressionParser:
    """Recursive-descent validator for the SPDX license-expression grammar.

    Validates structure only -- AND/OR precedence is irrelevant to validity,
    so both bind identically here. Returns whether the full token stream forms
    a syntactically valid expression with every identifier recognized.
    """

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self._pos = 0

    def _peek(self) -> str | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _advance(self) -> str | None:
        tok = self._peek()
        if tok is not None:
            self._pos += 1
        return tok

    def parse(self) -> bool:
        """Return True when the token stream is a complete valid expression."""
        if not self._parse_or():
            return False
        return self._pos == len(self._tokens)

    def _parse_or(self) -> bool:
        if not self._parse_unit():
            return False
        nxt = self._peek()
        while nxt is not None and nxt.upper() in _OPERATORS:
            self._advance()
            if not self._parse_unit():
                return False
            nxt = self._peek()
        return True

    def _parse_unit(self) -> bool:
        tok = self._peek()
        if tok is None:
            return False
        if tok == "(":
            self._advance()
            if not self._parse_or():
                return False
            return self._advance() == ")"
        if tok in {")", *_OPERATORS} or tok.upper() == "WITH":
            return False
        self._advance()
        if not _is_valid_license_id(tok):
            return False
        nxt = self._peek()
        if nxt is not None and nxt.upper() == "WITH":
            self._advance()
            exc = self._advance()
            return exc is not None and exc in SPDX_EXCEPTION_IDS
        return True


def _has_expression_syntax(tokens: list[str]) -> bool:
    """Whether *tokens* contain any operator or grouping (vs a bare id)."""
    for tok in tokens:
        if tok in ("(", ")") or tok.upper() in _OPERATORS or tok.upper() == "WITH":
            return True
    return False


def classify_declared_license(declared: str) -> LicenseClass:
    """Classify a non-empty declared license string for SBOM rendering.

    Absent declarations (``None`` / empty) are the caller's responsibility
    (NOASSERTION) and must not be passed here.
    """
    value = declared.strip()
    if not value:
        return LicenseClass(KIND_NAMED, value)

    # Special declared tokens are assertions, not SPDX ids: render as named.
    if value.upper() == "UNLICENSED" or value.upper().startswith("SEE LICENSE IN "):
        return LicenseClass(KIND_NAMED, value)

    tokens = _tokenize(value)
    if not _has_expression_syntax(tokens):
        # A single token: a recognized id renders as license.id, else named.
        if len(tokens) == 1 and _is_valid_license_id(tokens[0]):
            return LicenseClass(KIND_ID, value)
        return LicenseClass(KIND_NAMED, value)

    if _ExpressionParser(tokens).parse():
        return LicenseClass(KIND_EXPRESSION, value)
    return LicenseClass(KIND_NAMED, value)


__all__ = [
    "KIND_EXPRESSION",
    "KIND_ID",
    "KIND_NAMED",
    "LicenseClass",
    "classify_declared_license",
]
