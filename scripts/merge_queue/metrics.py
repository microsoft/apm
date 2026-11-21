"""Shadow metrics from the runner's JSONL event log."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path


def _minutes(start: str, end: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds() / 60


def summarize(events: list[dict]) -> dict:
    kinds = Counter(event["event"] for event in events)
    verdicts = Counter(event["verdict"] for event in events if event["event"] == "verdict")
    published = {e["train"]: e["at"] for e in events if e["event"] == "published"}
    durations = [
        _minutes(published[e["train"]], e["at"])
        for e in events
        if e["event"] == "verdict" and e["train"] in published
    ]
    decided = verdicts["green"] + verdicts["red"]
    return {
        "trains_published": kinds["published"],
        "verdicts": dict(verdicts),
        "green_rate": round(verdicts["green"] / decided, 3) if decided else None,
        "would_land": kinds["would-land"],
        "deferred": kinds["deferred"],
        "writes_to_main": kinds["write-to-main"],
        "mean_train_minutes": round(sum(durations) / len(durations), 1) if durations else None,
    }


def read_events(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
