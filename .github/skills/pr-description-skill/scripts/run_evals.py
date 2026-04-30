#!/usr/bin/env python3
# ASCII-only. Runs the pr-description-skill evals suite.
"""Run pr-description-skill evals.

Two eval families are exercised:

  * TRIGGER EVALS scoring the SKILL.md `description:` against
    should-fire and should-not-fire queries via a deterministic
    keyword/bigram matcher. The matcher is documented in the
    README; it approximates dispatcher behavior without requiring
    a live LLM.

  * CONTENT EVALS scoring pre-recorded `with_skill` and
    `without_skill` fixtures against per-scenario regex rubrics.
    The delta between the two scores is reported per scenario.

The runner is non-interactive, stdlib-only, and emits structured
JSON on stdout (machine-readable summary) plus diagnostics on
stderr. Exit codes:

  0 = all gates met
  1 = one or more gates failed
  2 = runner error (manifest or fixture missing, parse error)

Run from the worktree root:

    python .apm/skills/pr-description-skill/scripts/run_evals.py

Use --help for full options.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SKILL_DIR = Path(__file__).resolve().parent.parent
EVALS_DIR = SKILL_DIR / "evals"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _log(f"[x] missing file: {path}")
        sys.exit(2)
    except json.JSONDecodeError as exc:
        _log(f"[x] invalid JSON in {path}: {exc}")
        sys.exit(2)


# ---------------------------------------------------------------------------
# trigger evals
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def score_trigger(query: str, manifest: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Return (predicted_fire, diagnostic) for one query.

    Rule (deterministic dispatcher approximation):
      1. Lowercase + collapse whitespace.
      2. If any phrase from `stop_list` appears verbatim, predict
         no_fire (negative override beats everything else).
      3. If any phrase from `trigger_keywords_primary` appears
         verbatim, predict fire.
      4. Otherwise count distinct `trigger_keywords_secondary`
         tokens present; predict fire iff the count is >= 3 AND
         at least one of {pr, pull} is present.
    """
    q = _normalize(query)

    for stop in manifest["stop_list"]:
        if stop in q:
            return False, {"reason": "stop_list_hit", "match": stop}

    for kw in manifest["trigger_keywords_primary"]:
        if kw in q:
            return True, {"reason": "primary_match", "match": kw}

    sec = manifest["trigger_keywords_secondary"]
    hits = sorted({k for k in sec if re.search(rf"\b{re.escape(k)}\b", q)})
    has_pr = any(t in hits for t in ("pr", "pull"))
    if has_pr and len(hits) >= 3:
        return True, {"reason": "secondary_threshold", "hits": hits}

    return False, {"reason": "no_match", "hits": hits}


def run_trigger_evals(
    manifest: dict[str, Any], split: str
) -> dict[str, Any]:
    triggers_path = EVALS_DIR / manifest["triggers_manifest"]
    triggers = _load_json(triggers_path)["items"]

    if split != "all":
        triggers = [t for t in triggers if t["split"] == split]

    rows: list[dict[str, Any]] = []
    fire_total = fire_correct = 0
    no_fire_total = no_fire_correct = 0

    for item in triggers:
        predicted_fire, diag = score_trigger(item["query"], manifest)
        expected_fire = item["expected"] == "fire"
        passed = predicted_fire == expected_fire

        rows.append({
            "id": item["id"],
            "split": item["split"],
            "query": item["query"],
            "expected": item["expected"],
            "predicted": "fire" if predicted_fire else "no_fire",
            "passed": passed,
            "diagnostic": diag,
        })

        if expected_fire:
            fire_total += 1
            if passed:
                fire_correct += 1
        else:
            no_fire_total += 1
            if passed:
                no_fire_correct += 1

    fire_rate = (fire_correct / fire_total) if fire_total else 0.0
    no_fire_rate = (no_fire_correct / no_fire_total) if no_fire_total else 0.0

    gates = manifest["gates"]["triggers"]
    fire_gate_met = fire_rate >= gates["should_fire_rate_min"]
    no_fire_gate_met = (
        (no_fire_rate) >= (1.0 - gates["should_not_fire_rate_max"])
    )

    return {
        "split": split,
        "should_fire_correct_rate": fire_rate,
        "should_not_fire_correct_rate": no_fire_rate,
        "should_fire_gate": {
            "min": gates["should_fire_rate_min"],
            "met": fire_gate_met,
        },
        "should_not_fire_gate": {
            "max_miss_rate": gates["should_not_fire_rate_max"],
            "met": no_fire_gate_met,
        },
        "rows": rows,
        "passed": fire_gate_met and no_fire_gate_met,
    }


# ---------------------------------------------------------------------------
# content evals
# ---------------------------------------------------------------------------

def score_fixture(text: str, rubric: list[dict[str, Any]]) -> dict[str, Any]:
    score = 0
    anchors_hit: list[str] = []
    anchors_missed: list[str] = []
    for check in rubric:
        pattern = re.compile(check["pattern"])
        match = bool(pattern.search(text))
        weight = int(check["weight"])
        if match:
            score += weight
            anchors_hit.append(check["id"])
        else:
            if weight < 0:
                # penalty pattern: not matching is GOOD, no score change
                pass
            else:
                anchors_missed.append(check["id"])
    return {"score": score, "anchors_hit": anchors_hit, "anchors_missed": anchors_missed}


def run_content_eval(scenario_path: Path, manifest_root: Path) -> dict[str, Any]:
    scenario = _load_json(scenario_path)

    fixtures = scenario["fixtures"]
    with_path = (scenario_path.parent / fixtures["with_skill"]).resolve()
    without_path = (scenario_path.parent / fixtures["without_skill"]).resolve()

    if not with_path.exists():
        _log(f"[x] missing fixture: {with_path}")
        sys.exit(2)
    if not without_path.exists():
        _log(f"[x] missing fixture: {without_path}")
        sys.exit(2)

    with_text = with_path.read_text(encoding="utf-8")
    without_text = without_path.read_text(encoding="utf-8")

    with_score = score_fixture(with_text, scenario["rubric"])
    without_score = score_fixture(without_text, scenario["rubric"])

    delta_score = with_score["score"] - without_score["score"]
    delta_anchors = len(
        set(with_score["anchors_hit"]) - set(without_score["anchors_hit"])
    )

    return {
        "id": scenario["id"],
        "summary": scenario["summary"],
        "with_skill": with_score,
        "without_skill": without_score,
        "delta_score": delta_score,
        "delta_anchors": delta_anchors,
    }


def run_content_evals(manifest: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    delta_min = int(manifest["gates"]["content"]["delta_min_anchors"])
    all_passed = True

    for rel in manifest["content_manifests"]:
        scenario_path = EVALS_DIR / rel
        result = run_content_eval(scenario_path, EVALS_DIR)
        result["passed"] = result["delta_anchors"] >= delta_min
        if not result["passed"]:
            all_passed = False
        results.append(result)

    return {
        "delta_min_anchors": delta_min,
        "scenarios": results,
        "passed": all_passed,
    }


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_evals.py",
        description="Run pr-description-skill evals (triggers + content).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--filter",
        choices=("all", "triggers", "content"),
        default="all",
        help="Eval family to run (default: all).",
    )
    p.add_argument(
        "--split",
        choices=("all", "train", "val"),
        default="val",
        help="Trigger-eval split to score for the gate (default: val, the ship gate).",
    )
    p.add_argument(
        "--manifest",
        default=str(EVALS_DIR / "evals.json"),
        help="Path to evals manifest (default: <skill>/evals/evals.json).",
    )
    p.add_argument(
        "--results-dir",
        default=str(EVALS_DIR / "results"),
        help="Directory to write timestamped result JSON.",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stderr diagnostics.",
    )
    p.add_argument(
        "--no-write",
        action="store_true",
        help="Do not write a result file to --results-dir.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.quiet:
        global _log
        _log = lambda msg: None  # noqa: E731

    manifest_path = Path(args.manifest)
    manifest = _load_json(manifest_path)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        _log(
            f"[!] schema_version mismatch: manifest={manifest.get('schema_version')} "
            f"runner={SCHEMA_VERSION}"
        )

    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "skill": manifest.get("skill"),
        "timestamp_utc": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
        "filter": args.filter,
        "split": args.split,
    }

    overall_passed = True

    if args.filter in ("all", "triggers"):
        _log("[>] running trigger evals")
        trig = run_trigger_evals(manifest, args.split)
        summary["triggers"] = trig
        if not trig["passed"]:
            overall_passed = False

    if args.filter in ("all", "content"):
        _log("[>] running content evals")
        cont = run_content_evals(manifest)
        summary["content"] = cont
        if not cont["passed"]:
            overall_passed = False

    summary["passed"] = overall_passed

    if not args.no_write:
        results_dir = Path(args.results_dir)
        results_dir.mkdir(parents=True, exist_ok=True)
        ts = summary["timestamp_utc"].replace(":", "-").replace("+00-00", "Z")
        out_path = results_dir / f"{ts}.json"
        out_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        summary["result_file"] = str(out_path.relative_to(SKILL_DIR.parent.parent.parent))
        _log(f"[+] wrote {out_path}")

    print(json.dumps(summary, indent=2))
    return 0 if overall_passed else 1


if __name__ == "__main__":
    sys.exit(main())
