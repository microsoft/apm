"""The lifecycle obligation set: t=2 pairs, five named triples, rules applied (#4099).

``build_space()`` enumerates every pair of factors (plus the exception
factors crossed with the factors they refine) and the five named triples,
then applies ``rules.classify``. The result separates obligations (each with
its required strength), cells mapped onto another obligation (equivalence or
refusal, with the rule chain) and removed cells (definition rules).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from itertools import combinations, product

from tests.lifecycle.generator.factors import (
    EXCEPTION_FACTORS,
    FACTORS,
    TRIPLES,
    cell_id,
    parse_cell,
)
from tests.lifecycle.generator.rules import (
    EXCEPTION_PARTNERS,
    RULE_CELL_REFUSALS,
    RULE_CELLS,
    STATE_FACT_RULE_CELLS,
    classify,
    refusal_cell,
)

# Must-cover rules whose evidence is a state fact (a file kept, nothing written).
_STATE_FACT_RULES = frozenset({"U2", "U7"})


@dataclass(frozen=True)
class Entry:
    id: str
    family: str
    cell: Mapping[str, str]
    strength: str
    rules: tuple[str, ...] = ()
    must_cover: bool = False
    exit: int | None = None


@dataclass
class Space:
    obligations: dict[str, Entry] = field(default_factory=dict)
    # id -> (final obligation id, rule chain)
    mapped: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)
    removed: dict[str, tuple[str, ...]] = field(default_factory=dict)
    raw: dict[str, int] = field(default_factory=dict)


def raw_cells() -> Iterator[tuple[str, dict[str, str]]]:
    """Every candidate cell before the rules: pairs, exception pairs, named triples."""
    for first, second in combinations(FACTORS, 2):
        for a, b in product(FACTORS[first], FACTORS[second]):
            yield "pair", {first: a, second: b}
    for factor, partners in EXCEPTION_PARTNERS.items():
        for partner, domain in partners.items():
            for a, b in product(EXCEPTION_FACTORS[factor], domain or FACTORS[partner]):
                yield "pair", {factor: a, partner: b}
    for family, names in TRIPLES.items():
        for values in product(*(FACTORS[name] for name in names)):
            yield family, dict(zip(names, values, strict=True))


def _strength(family: str, key: str, rules: tuple[str, ...]) -> str:
    if family == "refusal" or key in STATE_FACT_RULE_CELLS:
        return "e2e-evidence"
    if set(rules) & _STATE_FACT_RULES:
        return "e2e-evidence"
    return "e2e"


def _add(space: Space, family: str, cell: Mapping[str, str], **fields: object) -> str:
    key = cell_id(family, cell)
    if key not in space.obligations:
        rules = tuple(fields.pop("rules", ()))
        space.obligations[key] = Entry(
            key, family, dict(cell), _strength(family, key, rules), rules, **fields
        )
    return key


def _resolve(space: Space, family: str, cell: Mapping[str, str]) -> str | None:
    """Add *cell* to *space*; return the obligation id that discharges it (None if removed)."""
    key = cell_id(family, cell)
    verdict = classify(family, cell)
    if verdict.status == "removed":
        space.removed[key] = verdict.rules
        return None
    if verdict.status == "obligation":
        return _add(space, family, cell, rules=verdict.rules, must_cover=verdict.must_cover)
    target = verdict.maps_to
    assert target is not None, key
    if target.startswith("refusal|"):
        refusal = refusal_cell(cell)
        assert refusal is not None and refusal[0] == target, (key, target)
        final = _add(space, "refusal", parse_cell(target)[1], rules=verdict.rules, exit=refusal[1])
    elif target.startswith("rule|"):
        assert target in space.obligations, target
        final = target
    else:
        final = _resolve(space, *parse_cell(target))
    if final is None:
        space.removed[key] = verdict.rules
        return None
    chain = verdict.rules + space.mapped.get(target, (None, ()))[1]
    space.mapped[key] = (final, chain)
    return final


def build_space() -> Space:
    space = Space()
    for key, rule in RULE_CELLS.items():
        exit_code = RULE_CELL_REFUSALS[key][0] if key in RULE_CELL_REFUSALS else None
        space.obligations[key] = Entry(
            key,
            "rule",
            {},
            _strength("rule", key, (rule,)),
            (rule,),
            must_cover=True,
            exit=exit_code,
        )
    for family, cell in raw_cells():
        space.raw[family] = space.raw.get(family, 0) + 1
        _resolve(space, family, cell)
    return space
