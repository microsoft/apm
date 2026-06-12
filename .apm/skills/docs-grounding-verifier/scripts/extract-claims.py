#!/usr/bin/env python3
"""
Extract atomic factual claims from a documentation page.

Stage 1 of the grounding-verification pipeline. Emits a prompt on stdout
that the caller pipes to an LLM to get back JSON-structured claims.

USAGE:
    python3 extract-claims.py <page_path>           # emit prompt
    python3 extract-claims.py <page_path> --schema  # show expected JSON schema

A factual claim is a statement that COULD be verified or falsified against
the codebase. Examples:
  - "The `apm install` command writes to apm.lock.yaml"
  - "Hook paths are rewritten by BaseIntegrator"
  - "Registry selectors must be semver ranges"
NOT factual claims:
  - "APM makes dependency management simple"  (opinion)
  - "See the guide for more"  (meta)
"""

import sys
from pathlib import Path

SCHEMA = """{
  "page": "<page_path>",
  "claims": [
    {
      "id": "c1",
      "text": "<atomic claim, single sentence>",
      "section": "<heading the claim appears under>",
      "keywords": ["<3-6 grep-able keywords for evidence retrieval>"],
      "expected_source_areas": ["<file or dir hints, e.g. src/apm_cli/integration/>"]
    }
  ]
}"""

PROMPT_TEMPLATE = """You are extracting ATOMIC FACTUAL CLAIMS from a documentation page.

A factual claim is a statement that could be verified or falsified against
source code. Each claim must be a single sentence describing ONE fact.

EXCLUDE: opinions, marketing language, navigation/meta text ("see also",
"in the next section"), section headings, code-block contents (those are
already source-of-truth).

INCLUDE: command behavior, file paths written/read, schema fields, default
values, lifecycle/ordering, integration points, error conditions.

For each claim provide:
  - text: one sentence stating the fact
  - section: the heading the claim appears under
  - keywords: 3-6 specific words a grep could use (function names, file
    paths, flag names, schema keys) - NOT generic English
  - expected_source_areas: file paths or directories where evidence would
    live (e.g. "src/apm_cli/commands/install.py")

Cap at 15 claims per page. Pick the 15 most LOAD-BEARING claims.

OUTPUT VALID JSON ONLY, conforming to this schema:
"""


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: extract-claims.py <page_path> [--schema]\n")
        sys.exit(2)
    if "--schema" in sys.argv:
        print(SCHEMA)
        return
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return

    page_path = Path(sys.argv[1])
    if not page_path.exists():
        sys.stderr.write(f"page not found: {page_path}\n")
        sys.exit(1)

    content = page_path.read_text()

    print(PROMPT_TEMPLATE)
    print(SCHEMA)
    print("\n---\nPAGE: " + str(page_path))
    print("---\n")
    print(content)


if __name__ == "__main__":
    main()
