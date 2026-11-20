#!/usr/bin/env python3
"""Load the program's journey catalogue through one schema-checked loader.

Sources, in order:
  1. --catalog PATH supplied by the operator (JSON or YAML);
  2. tests/lifecycle/journeys/index.yaml in the repository root.
There is no embedded copy and no fallback: if neither exists the loader
refuses with "catalogue unavailable" (exit 3).

Accepted shapes, normalized to one record per journey:
  - index.yaml (schema_version 1, Stream T): steps carry "key_lines",
    "forbidden_lines", "kind" (apm|shell|edit|manual) and "argv";
  - devx-journeys.json (the program's source deliverable): steps carry
    "must_print" and "must_not_print".
Each step must carry "cmd"; every other step field is optional. Both
shapes normalize to the same record, so one code path reads either.

stdout: JSON {"source": path, "journeys": [...], "counts": {...}}.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

INDEX_PATH = Path("tests/lifecycle/journeys/index.yaml")
PERSONAS = {"consumer", "producer", "enterprise"}


class CatalogueError(ValueError):
    """Raised when the catalogue is missing or malformed."""


def _read(path: Path) -> object:
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise CatalogueError(f"PyYAML is required to read {path}") from exc
        return yaml.safe_load(text)
    return json.loads(text)


def normalize(data: object, source: str) -> list[dict]:
    """Validate and normalize a catalogue document into journey records."""
    if not isinstance(data, dict) or not isinstance(data.get("journeys"), list):
        raise CatalogueError(f"{source}: expected a mapping with a 'journeys' list")
    journeys: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(data["journeys"]):
        where = f"{source}: journeys[{index}]"
        if not isinstance(raw, dict):
            raise CatalogueError(f"{where}: expected a mapping")
        jid = raw.get("id")
        if not isinstance(jid, str) or not jid:
            raise CatalogueError(f"{where}: missing 'id'")
        if jid in seen:
            raise CatalogueError(f"{where}: duplicate id {jid}")
        seen.add(jid)
        persona = raw.get("persona")
        if persona not in PERSONAS:
            raise CatalogueError(f"{where}: persona must be one of {sorted(PERSONAS)}")
        steps_raw = raw.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise CatalogueError(f"{where}: 'steps' must be a non-empty list")
        steps = []
        for sindex, step in enumerate(steps_raw, start=1):
            if not isinstance(step, dict) or not isinstance(step.get("cmd"), str):
                raise CatalogueError(f"{where}.steps[{sindex - 1}]: each step needs 'cmd'")
            steps.append(
                {
                    "id": f"{jid}.{step.get('n', sindex)}",
                    "cmd": step["cmd"],
                    "kind": step.get("kind", "apm" if step["cmd"].startswith("apm ") else "manual"),
                    "argv": list(step.get("argv") or []),
                    "exit": step.get("exit"),
                    "must_print": list(step.get("key_lines", step.get("must_print")) or []),
                    "must_not_print": list(
                        step.get("forbidden_lines", step.get("must_not_print")) or []
                    ),
                    "files": list(step.get("files") or []),
                    "oracles": list(step.get("oracles") or []),
                }
            )
        journeys.append(
            {
                "id": jid,
                "persona": persona,
                "title": raw.get("title", ""),
                "golden": bool(raw.get("golden", False)),
                "families": list(raw.get("families") or []),
                "docs": list(raw.get("docs") or []),
                "steps": steps,
            }
        )
    return journeys


def load(repo_root: Path, catalog: str | None) -> dict:
    if catalog:
        path = Path(catalog)
        if not path.is_file():
            raise CatalogueError(f"catalogue unavailable: {path} does not exist")
    else:
        path = repo_root / INDEX_PATH
        if not path.is_file():
            raise CatalogueError(
                f"catalogue unavailable: {INDEX_PATH} is not in this checkout; "
                "pass --catalog PATH to use another copy of the program catalogue"
            )
    journeys = normalize(_read(path), str(path))
    counts: dict[str, int] = {}
    for journey in journeys:
        counts[journey["persona"]] = counts.get(journey["persona"], 0) + 1
    return {
        "source": str(path),
        "embedded": False,
        "journeys": journeys,
        "counts": {
            "journeys": len(journeys),
            "steps": sum(len(j["steps"]) for j in journeys),
            **counts,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load the program journey catalogue.")
    parser.add_argument("--repo-root", default=".", help="repository root (default: .)")
    parser.add_argument("--catalog", help="operator-supplied catalogue path")
    parser.add_argument("--ids", help="comma-separated journey ids to keep")
    args = parser.parse_args(argv)
    try:
        result = load(Path(args.repo_root), args.catalog)
    except CatalogueError as exc:
        print(f"journeys: {exc}", file=sys.stderr)
        return 3
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        missing = sorted(wanted - {j["id"] for j in result["journeys"]})
        if missing:
            print(f"journeys: unknown ids {missing}", file=sys.stderr)
            return 3
        result["journeys"] = [j for j in result["journeys"] if j["id"] in wanted]
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
