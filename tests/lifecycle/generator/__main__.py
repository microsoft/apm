"""``python -m tests.lifecycle.generator [--check]`` (#4099).

Without arguments, regenerate ``tests/quality/lifecycle_obligations.json``
and ``tests/lifecycle/replay/rows/generated_*.yaml``. With ``--check``, write
nothing and exit 1 when a checked-in output differs from a fresh generation.
"""

from __future__ import annotations

import argparse
import sys

from tests.lifecycle.generator import emit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.lifecycle.generator")
    parser.add_argument("--check", action="store_true", help="fail on drift, write nothing")
    args = parser.parse_args(argv)
    if args.check:
        stale = emit.drift()
        for path in stale:
            print(f"[x] stale generated output: {path}")
        if stale:
            print("[i] Run 'python -m tests.lifecycle.generator' and commit the result.")
            return 1
        print("[+] generated lifecycle obligations are up to date")
        return 0
    emit.write()
    print("[+] wrote tests/quality/lifecycle_obligations.json and the generated replay rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
