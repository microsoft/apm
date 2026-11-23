"""One canonical caseless key for path spellings (#5158, #5167)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.path_security import _caseless
from apm_cli.utils.paths import spelling_key

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I3", "P-INV-I1")]

# U+0390 and its NFD capital spelling: folding after NFC alone misses this pair.
_REDECOMPOSING = [
    ("\u0390", "\u0399\u0308\u0301"),
    ("\u03b0", "\u03a5\u0308\u0301"),
    ("\u1fd3", "\u0399\u0308\u0301"),
]


@pytest.mark.parametrize(("composed", "decomposed"), _REDECOMPOSING)
def test_a_re_decomposing_spelling_shares_one_key(composed: str, decomposed: str) -> None:
    assert spelling_key(f"rules/{composed}.md") == spelling_key(f"rules/{decomposed}.md")


# Separator-free spellings: str(Path) uses the platform separator, the key uses "/".
@pytest.mark.parametrize(
    "spelling", ["Guide.md", "caf\u00e9", "cafe\u0301", "\u0390", "\u0399\u0308\u0301"]
)
def test_the_spelling_key_is_the_containment_key(spelling: str) -> None:
    assert spelling_key(spelling) == _caseless(Path(spelling))


def test_the_spelling_key_uses_forward_slashes() -> None:
    assert spelling_key("Rules\\Guide.md") == spelling_key("rules/guide.md") == "rules/guide.md"
