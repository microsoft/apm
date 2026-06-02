# OpenAPM Spec Linter Checklist (Wave 5)

Mechanical post-fold checks for any PR that touches the OpenAPM
specification artifact (`docs/src/content/docs/specs/openapm-*.md`),
its sidecar JSON Schemas, or the conformance fixture seed. Each check
is a one-liner that produces exit code 0 (or empty output where
noted). Run them in order. Record pass / fail per check.

Linter outcomes are ADVISORY -- a failed check does NOT change the
synthesizer's `ship_decision`; it surfaces in the comment as a
"Linter notes" section so the maintainer decides whether to fold the
fix into the same PR.

Conventions:
- `SPEC` = path of the in-scope spec markdown (e.g.
  `docs/src/content/docs/specs/openapm-v0.1.md`).
- `FIXTURES` = `tests/fixtures/spec-conformance/`.
- `SCHEMAS` = the set of JSON Schemas referenced from Appendix A
  (whether inline-fenced or sidecar under
  `docs/src/content/docs/specs/schemas/`).

## 1. ASCII-only artifact

```bash
python3 -c "
import sys
data = open('SPEC').read()
bad = [(i, repr(c)) for i, c in enumerate(data) if ord(c) > 0x7E]
print('OK' if not bad else bad[:20])
sys.exit(0 if not bad else 1)
"
```

MUST print `OK`. Every byte in the spec MUST be within U+0020 - U+007E.

## 2. Forbidden tokens (no-vendor-foundation language)

```bash
grep -niE 'CNCF|Linux Foundation|Sandbox|Incubation|W3C Process|IETF RFC stream' SPEC
```

MUST return empty (i.e. grep exits 1). Pedigree references in the
panelist persona prompts are fine; binding affiliations in the
artifact text are not.

## 3. JSON Schemas parse + check_schema

For each schema (inline-fenced under Appendix A or sidecar file under
`docs/src/content/docs/specs/schemas/*.schema.json`):

```bash
python3 -c "
import json, jsonschema
jsonschema.Draft202012Validator.check_schema(json.load(open('SCHEMA_PATH')))
"
```

All schemas MUST pass `check_schema`. If a schema is inline-fenced,
extract it first with a one-off helper that parses Markdown
fences with `language=json`.

## 4. Fixture parse

For each `*.yml` and `*.json` under `FIXTURES`:

```bash
python3 -c "
import yaml, sys
yaml.safe_load(open('FIXTURE_PATH'))
"
```

(Use `json.load` for `.json` fixtures.) All MUST parse.

## 5. req-XXX anchor uniqueness

```bash
grep -oE 'id="req-[a-z0-9-]+"' SPEC | sort | uniq -d
```

MUST return empty. Every `<a id="req-...">` anchor in the spec is
unique.

## 6. Count consistency across the three sites

Extract the normative-statement count from three places in the spec:
(a) the sentence in sec. 1.3, (b) the Appendix C trailer
("Total: N statements"), and (c) the Appendix D revision-history
("N -> M"). All three numbers MUST agree AND MUST equal:

```bash
grep -c 'id="req-' SPEC
```

minus any prose references (one-off textual mention of `req-XXX`
outside an anchor declaration; these must be hand-counted and
subtracted).

## 7. Markdown link resolution (relative anchors)

```bash
python3 - <<'PY'
import re, sys
src = open('SPEC').read()
anchors = set(re.findall(r'<a id="([^"]+)"', src))
anchors |= {re.sub(r'[^a-z0-9]+', '-', h.lower()).strip('-')
            for h in re.findall(r'^#+\s+(.+)$', src, re.M)}
missing = [a for a in re.findall(r'\]\(#([^)]+)\)', src)
           if a not in anchors]
print('OK' if not missing else missing[:20])
sys.exit(0 if not missing else 1)
PY
```

MUST print `OK`. Every `](#anchor)` in the spec MUST resolve to
either an `<a id="anchor">` declaration or a heading whose slug
equals `anchor`.

## 8. Mermaid blocks (if present)

```bash
grep -c '^```mermaid' SPEC
```

If 0, skip. If > 0, copy each block into a scratch file and validate
with `npx -y @mermaid-js/mermaid-cli -i SCRATCH -o /dev/null`. If
`mmdc` is unavailable, mark the check SKIPPED and surface in
linter notes so the maintainer can confirm at PR review.

## 9. Fixture cross-citation

Every fixture file under `FIXTURES` MUST contain at least one
`req-XXX` reference (in YAML comments or JSON `_comment`):

```bash
python3 - <<'PY'
import os, sys
root = 'tests/fixtures/spec-conformance/'
bad = []
for dirpath, _, files in os.walk(root):
    for f in files:
        if not (f.endswith('.yml') or f.endswith('.json')):
            continue
        p = os.path.join(dirpath, f)
        if 'req-' not in open(p).read():
            bad.append(p)
print('OK' if not bad else bad)
sys.exit(0 if not bad else 1)
PY
```

MUST print `OK`. Cross-citation makes every fixture trace back to a
normative statement it exercises.

## 10. CHANGELOG.md update

When the PR adds a fold of any size to the spec artifact, the
`CHANGELOG.md` SHOULD mention the spec file path under the next
unreleased section (per `.github/instructions/changelog.instructions.md`).

```bash
grep -q 'openapm-' CHANGELOG.md && echo OK || echo MISSING
```

SHOULD print `OK` for substantive folds; MAY be SKIPPED for pure nit
folds (typo / heading-label fixes). Linter surfaces "MISSING" as an
advisory note, never as a hard fail.

## 11. APM lint contract (Python lint chain N/A here)

The spec is `.md` not `.py`, so `ruff` / `pylint` do not apply.
Confirm no `.py` files were modified in this PR:

```bash
git diff --stat origin/main...HEAD | grep -E '\.py(\s|$)' || echo NONE
```

MUST print `NONE`. If a `.py` file was modified, the PR is outside
this skill's scope (it should also be triggering `apm-review-panel`);
surface "Non-spec files modified -- consider running apm-review-panel
in parallel" as a linter note.

---

**Failure handling.** A failing check does NOT change the
synthesizer's `ship_decision`. It is reported in the comment as a
"Linter notes" entry: the check id, the one-line failure summary, and
(if applicable) the synthesizer's `linter_handoff_notes` that
specifically called this check out.
