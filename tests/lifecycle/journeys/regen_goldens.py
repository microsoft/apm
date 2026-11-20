"""Regenerate the goldens of the golden journeys (#4110, normalizer N7).

Usage (from the repository root)::

    uv run --extra dev python tests/lifecycle/journeys/regen_goldens.py [--check] [J01 ...]

It runs the golden journeys (index ``golden: true``, or the ids given) through
pytest with ``APM_JOURNEY_RECORD_DIR`` set, so every O1 step writes its
normalized transcript instead of comparing it. Every other oracle still runs;
a failing journey writes nothing. A transcript with a non-ASCII byte or a line
repeated within one command is refused (O3, O4). The script prints the review
diff, then writes ``goldens/<J>/<nn>.txt``; ``--check`` only compares and
exits 1 on any difference. ``--record-dir`` keeps the raw transcripts in a
directory of your choice (default: a temporary directory).
"""

from __future__ import annotations

import argparse
import difflib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))

from tests.lifecycle.journeys import normalize as norm  # noqa: E402


def golden_ids() -> list[str]:
    index = yaml.safe_load((HERE / "index.yaml").read_text(encoding="ascii"))
    return [journey["id"] for journey in index["journeys"] if journey.get("golden")]


def record(ids: list[str], directory: Path) -> int:
    env = dict(os.environ, **{"APM_JOURNEY_RECORD_DIR": str(directory)})
    command = [
        sys.executable,
        "-m",
        "pytest",
        str(HERE / "test_journeys.py"),
        "-n",
        "0",
        "-p",
        "no:cacheprovider",
        "-q",
        "-k",
        " or ".join(ids),
    ]
    return subprocess.run(command, cwd=REPO, env=env, check=False).returncode


def review(directory: Path, ids: list[str], *, write: bool) -> tuple[int, int]:
    """Return (refused, changed) counts; write accepted goldens when *write*."""
    refused = changed = 0
    for jid in ids:
        recorded = sorted((directory / jid).glob("*.txt"))
        known = sorted((norm.GOLDENS / jid).glob("*.txt"))
        stale = {p.name for p in known} - {p.name for p in recorded}
        for name in sorted(stale):
            print(f"[!] {jid}/{name}: golden has no recorded step; remove it")
            changed += 1
        for path in recorded:
            text = path.read_text(encoding="utf-8")
            problems = norm.golden_problems(text)
            if problems:
                refused += 1
                print(f"[x] {jid}/{path.name} refused: " + "; ".join(problems))
                continue
            target = norm.golden_path(jid, int(path.stem))
            old = target.read_text(encoding="ascii") if target.is_file() else ""
            if norm.canonical(old.splitlines()) == norm.canonical(text.splitlines()):
                continue
            changed += 1
            sys.stdout.writelines(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    f"golden/{jid}/{path.name}",
                    f"recorded/{jid}/{path.name}",
                )
            )
            if write:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="ascii")
    return refused, changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ids", nargs="*", help="journey ids (default: every golden journey)")
    parser.add_argument("--check", action="store_true", help="compare only; never write")
    parser.add_argument("--record-dir", type=Path, help="keep raw transcripts here")
    args = parser.parse_args(argv)
    ids = args.ids or golden_ids()
    unknown = set(ids) - set(golden_ids())
    if unknown:
        print(f"[x] not golden journeys: {sorted(unknown)}")
        return 2
    directory = args.record_dir or Path(tempfile.mkdtemp(prefix="apm-journey-goldens-"))
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    try:
        if record(ids, directory) != 0:
            print("[x] the journeys failed; no golden written")
            return 1
        refused, changed = review(directory, ids, write=not args.check)
    finally:
        if args.record_dir is None:
            shutil.rmtree(directory, ignore_errors=True)
    if refused:
        print(f"[x] {refused} transcript(s) refused (O3 ASCII or O4 duplicate line)")
        return 1
    if args.check and changed:
        print(f"[x] {changed} golden(s) differ; run without --check to update them")
        return 1
    print(f"[+] {changed} golden(s) {'differ' if args.check else 'written'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
