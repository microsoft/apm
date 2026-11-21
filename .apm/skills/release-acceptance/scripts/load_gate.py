#!/usr/bin/env python3
"""Check the host load before spawning acceptance workers.

Exit codes: 0 ready, 75 busy (load above the threshold), 2 usage error.
stdout: one JSON object {"ready": bool, "load1": float, "threshold": float}.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report whether the 1-minute load average allows another worker."
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=30.0,
        help="maximum 1-minute load average (default: 30)",
    )
    args = parser.parse_args(argv)
    try:
        load1 = os.getloadavg()[0]
    except OSError as exc:
        print(f"load_gate: cannot read load average: {exc}", file=sys.stderr)
        return 2
    ready = load1 <= args.threshold
    print(json.dumps({"ready": ready, "load1": round(load1, 2), "threshold": args.threshold}))
    return 0 if ready else 75


if __name__ == "__main__":
    sys.exit(main())
