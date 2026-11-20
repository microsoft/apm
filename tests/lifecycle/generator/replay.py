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
import re
from collections.abc import Mapping
from dataclasses import replace
from functools import cache
from pathlib import Path

import yaml

from tests.lifecycle import driver, oracles
from tests.lifecycle.rows import Row, RowReplay, load_row

OBLIGATIONS_PATH = Path(__file__).parents[2] / "quality" / "lifecycle_obligations.json"
# Commands that name a package by its identity, not by its install spec
# (`apm update lifecycle/p-skill#v1.0.0` answers "not found in apm.yml").
_IDENTITY_COMMANDS = frozenset({"approve", "deny", "update"})


@cache
def required_strengths(path: Path = OBLIGATIONS_PATH) -> Mapping[str, str]:
    """Obligation id -> required strength, for every id of the generated list."""
    data = json.loads(path.read_text(encoding="ascii"))
    return {key: entry["strength"] for key, entry in data["obligations"].items()}


def credit(
    cell: str,
    before: oracles.Observation,
    after: oracles.Observation,
    result,
    unchanged: tuple[str, ...] = (),
    *,
    scope: str = "project",
) -> None:
    """Credit one generated obligation through the oracle entry point."""
    oracles.check_obligation(
        cell,
        strength=required_strengths()[cell],
        before=before,
        after=after,
        result=result,
        evidence=oracles.Evidence(
            exit=result.returncode,
            ascii_output=True,
            unchanged={"project": unchanged} if unchanged else {},
        ),
        scope=scope,
    )


def load_covering_row(path: Path) -> tuple[Row, Mapping[str, object]]:
    """The row as ``rows.py`` reads it, plus the raw document for the extensions."""
    return load_row(path), yaml.safe_load(path.read_text(encoding="utf-8"))


class CoveringReplay(RowReplay):
    """``RowReplay`` with package sources and obligation crediting."""

    def __init__(self, world, row: Row, document: Mapping[str, object], **kwargs) -> None:
        super().__init__(world, row, **kwargs)
        self.sources: Mapping[str, str] = dict(document["world"].get("sources", {}))
        self.credited: list[str] = []
        self._refusal = False

    def step(self, step: object) -> None:
        # A pinned refusal is a new request, never a repeat of the converged
        # command, so I2 does not apply to it; I7 and the step's own
        # ``unchanged`` evidence still prove it wrote nothing.
        self._refusal = isinstance(step, Mapping) and "exit" in step
        cells = step.get("cells", ()) if isinstance(step, Mapping) else ()
        if not cells:
            super().step(step)
            return
        before = self.observe()
        super().step(step)
        after = self.observe()
        if "message" in step:
            output = f"{self.last.stdout}\n{self.last.stderr}"
            assert re.search(step["message"], output), (
                f"I7: {step['run']!r} refused without the pinned message "
                f"{step['message']!r}:\n{output}"
            )
        unchanged = tuple(step.get("unchanged", ()))
        for cell in cells:
            credit(cell, before, after, self.last, unchanged, scope=self.row.scope)
            self.credited.append(cell)

    def _requested(self, args: tuple[str, ...]):
        """``install --mcp`` declares a server and a refusal is a new request: neither repeats."""
        requested = super()._requested(args)
        if "--mcp" in args or self._refusal:
            return replace(requested, converged=False)
        return requested

    def _advance(self, op, requested, result) -> None:
        """A dry run changes nothing, so the model stays where it was."""
        if "DRYRUN" not in op.modifiers:
            super()._advance(op, requested, result)

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
