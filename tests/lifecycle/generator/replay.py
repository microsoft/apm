"""Replay one generated covering row and credit its obligations (#4099).

``CoveringReplay`` extends ``tests/lifecycle/rows.py`` ``RowReplay`` with what
the generated rows need, without changing the row schema it reads:

- ``world.sources`` maps a package to a world variant (``git``,
  ``marketplace``, ``bundle``); command lines keep the bare package name and
  the replay substitutes the variant's spec (or, for ``approve`` and
  ``deny``, its identity);
- ``{marketplace_url}`` in a command line is the world's marketplace;
- a step ``{run: <line>, cells: [<obligation id>, ...]}`` credits each id
  through ``oracles.check_obligation`` after the step kept every
  cross-cutting law. The required strength of an id comes from
  ``tests/quality/lifecycle_obligations.json``; ``e2e-evidence`` ids need a
  state fact, which the step names with ``unchanged`` (project path prefixes
  that must keep their bytes).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from pathlib import Path

import yaml

from tests.lifecycle import driver, oracles
from tests.lifecycle.rows import Row, RowReplay, load_row

OBLIGATIONS_PATH = Path(__file__).parents[2] / "quality" / "lifecycle_obligations.json"
# Commands that name a package by its identity, not by its install spec.
_IDENTITY_COMMANDS = frozenset({"approve", "deny"})


@cache
def required_strengths(path: Path = OBLIGATIONS_PATH) -> Mapping[str, str]:
    """Obligation id -> required strength, for every id of the generated list."""
    data = json.loads(path.read_text(encoding="ascii"))
    return {key: entry["strength"] for key, entry in data["obligations"].items()}


def load_covering_row(path: Path) -> tuple[Row, Mapping[str, object]]:
    """The row as ``rows.py`` reads it, plus the raw document for the extensions."""
    return load_row(path), yaml.safe_load(path.read_text(encoding="utf-8"))


class CoveringReplay(RowReplay):
    """``RowReplay`` with package sources and obligation crediting."""

    def __init__(self, world, row: Row, document: Mapping[str, object], **kwargs) -> None:
        super().__init__(world, row, **kwargs)
        self.sources: Mapping[str, str] = dict(document["world"].get("sources", {}))
        self.credited: list[str] = []

    def step(self, step: object) -> None:
        cells = step.get("cells", ()) if isinstance(step, Mapping) else ()
        if not cells:
            super().step(step)
            return
        before = self.observe()
        super().step(step)
        after = self.observe()
        unchanged = tuple(step.get("unchanged", ()))
        for cell in cells:
            evidence = oracles.Evidence(
                exit=self.last.returncode,
                ascii_output=True,
                unchanged={"project": unchanged} if unchanged else {},
            )
            oracles.check_obligation(
                cell,
                strength=required_strengths()[cell],
                before=before,
                after=after,
                result=self.last,
                evidence=evidence,
                scope=self.row.scope,
            )
            self.credited.append(cell)

    def _args(self, line: str) -> tuple[str, ...]:
        args = driver.command_args(line.format(marketplace_url=self.world.services.marketplace_url))
        command = args[0]
        return tuple(
            (self._identity(arg) if command in _IDENTITY_COMMANDS else self._spec(arg))
            if arg in self.kinds
            else arg
            for arg in args
        )

    def _ref(self, name: str):
        variant = self.sources.get(name)
        return self.world.packages[f"{name}:{variant}" if variant else name]

    def _spec(self, name: str) -> str:
        return self._ref(name).spec

    def _identity(self, name: str) -> str:
        """``apm approve`` takes the local path, or ``<org>/<name>`` for a remote source."""
        variant = self.sources.get(name)
        if variant in ("git", "marketplace"):
            return self.world.packages[f"{name}:git"].spec.split("#", 1)[0]
        return self._spec(name)
