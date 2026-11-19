#!/usr/bin/env python3
"""Check that required Copilot agent files exist in a consumer repository."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_AGENT_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def main() -> int:
    """Report available and missing required agent roles as JSON."""
    parser = argparse.ArgumentParser(
        description="Check required .github/agents/<slug>.agent.md files."
    )
    parser.add_argument(
        "--agents-dir",
        type=Path,
        default=Path(".github/agents"),
        help="consumer agent directory (default: .github/agents)",
    )
    parser.add_argument("roles", nargs="+", help="required agent role slugs")
    args = parser.parse_args()

    invalid = [role for role in args.roles if not _AGENT_SLUG.fullmatch(role)]
    if invalid:
        parser.error(f"invalid role slug(s): {', '.join(invalid)}")

    roles = sorted(set(args.roles))
    available = [role for role in roles if (args.agents_dir / f"{role}.agent.md").is_file()]
    missing = [role for role in roles if role not in available]
    print(
        json.dumps(
            {
                "agents_dir": str(args.agents_dir),
                "available_roles": available,
                "missing_roles": missing,
                "ready": not missing,
            },
            sort_keys=True,
        )
    )
    if missing:
        print(
            f"Missing required agent roles in {args.agents_dir}: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
