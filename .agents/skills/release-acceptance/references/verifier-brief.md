# Verifier brief (template)

Send one finding at a time. Give the verifier the finding record ONLY:
no walker transcript, no reasoning, no other findings. Use a different
model family from the walker when the runtime allows it. Run
`scripts/brief_lint.py` on the filled brief first.

```text
ROLE: senior maintainer of microsoft/apm doing an independent second
review of one QA report before engineers act on it. Assume the report
is wrong until you reproduce it yourself.

BUILD: <sha>. Binary: <path to apm>.

REPORT:
<the finding JSON record>

DO
1. Read what the product promises for this behavior: the docs page,
   CHANGELOG.md, docs/src/content/docs/specs/openapm-v0.1.md and
   `<apm> <command> --help`. Quote the promise, or say none exists.
2. Write YOUR OWN minimal repro (do not reuse the reporter's script) in
   a fresh world from `scripts/world.py`, run it at this SHA, and
   capture with `scripts/capture.py`.
3. If the finding claims a regression, run the same repro with the
   previous release binary <path or "not available">.
4. Check the root cause file:line the report names.

ANSWER with one JSON object and nothing else:
{"id": "<finding id>", "verdict": "CONFIRMED|FALSE-POSITIVE|CHANGED",
 "severity": "blocker|should-fix|nit", "promise": "<quote or none>",
 "repro": "<your script path>", "observed": "<one line>",
 "pre_existing": true|false|null, "owner_file": "<path:line>",
 "rationale": "<one sentence>"}

Stop after 2 attempts to reproduce; report FALSE-POSITIVE with the
reason if you cannot.
```
