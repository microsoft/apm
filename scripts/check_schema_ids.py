#!/usr/bin/env python3
"""Verify every published JSON Schema's $id resolves against the docs build.

Run after `npm run build` (or in CI after the docs build step). For each
schema under docs/public/specs/schemas/, this loads the schema, reads its
`$id`, derives the on-site path under docs/dist/, and asserts:

  1. the path exists in the build output;
  2. the bytes are identical to the source schema (sha256);
  3. the schema parses as JSON.

Meta-schema validation (Draft 2020-12) is NOT performed here; it is
already covered by the spec-conformance pytest suite via
`_manifest.check_schema()` on the requirements schema and implicitly
through fixture validation in `_helpers.validate_against()`. This
script uses only the Python standard library so it runs on the docs
build job without an extra Python setup step.

The check catches three real failure modes in one pass: schema moved
but $id forgotten, $id typo, and Starlight accidentally swallowing a
public/ asset. Failing exits 1 with a per-schema diagnostic.

The `$id` URL convention: each schema declares
`https://microsoft.github.io/apm/specs/schemas/<name>.schema.json`.
The repo's GitHub Pages base path is `/apm/`, so the local path under
`docs/dist/` is everything after `/apm/`.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
SCHEMA_SRC = DOCS_DIR / "public" / "specs" / "schemas"
DIST_DIR = DOCS_DIR / "dist"
SITE_BASE_PREFIX = "/apm/"


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    if not DIST_DIR.is_dir():
        print(
            f"[x] {DIST_DIR.relative_to(REPO_ROOT)} not found -- "
            "run `npm run build` from docs/ first.",
            file=sys.stderr,
        )
        return 2

    schemas = sorted(SCHEMA_SRC.glob("*.schema.json"))
    if not schemas:
        print(
            f"[x] no schemas found under {SCHEMA_SRC.relative_to(REPO_ROOT)}",
            file=sys.stderr,
        )
        return 2

    failures: list[str] = []
    for src in schemas:
        try:
            schema = json.loads(src.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(f"{src.name}: invalid JSON -- {exc}")
            continue

        sid = schema.get("$id")
        if not sid:
            failures.append(f"{src.name}: missing required `$id`")
            continue

        url_path = urlparse(sid).path
        if not url_path.startswith(SITE_BASE_PREFIX):
            failures.append(
                f"{src.name}: $id {sid!r} does not start with "
                f"{SITE_BASE_PREFIX!r} -- toolchains pinning by $id would "
                "land off-site"
            )
            continue

        rel = url_path[len(SITE_BASE_PREFIX) :]
        dist_path = DIST_DIR / rel
        if not dist_path.is_file():
            failures.append(
                f"{src.name}: $id resolves to {url_path} but "
                f"{dist_path.relative_to(REPO_ROOT)} is missing from the "
                "build output (schema moved without $id update, or "
                "Starlight ate the asset)"
            )
            continue

        if sha256(src) != sha256(dist_path):
            failures.append(
                f"{src.name}: bytes at $id URL differ from source -- the "
                "published schema is NOT byte-identical to the in-tree "
                "schema. This breaks every toolchain pinned to the $id."
            )
            continue

        print(f"[+] {src.name}: $id {sid} -> {dist_path.relative_to(REPO_ROOT)} (sha256 match)")

    if failures:
        print("", file=sys.stderr)
        print(
            f"[x] {len(failures)} schema $id reachability check(s) failed:",
            file=sys.stderr,
        )
        for f in failures:
            print(f"    - {f}", file=sys.stderr)
        return 1

    print(f"[+] all {len(schemas)} schema $id URLs resolve to byte-identical assets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
