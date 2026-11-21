# Atlas protocol

Atlas is the project's decision and lesson store. It is an external
companion: probe for it, never assume it.

## Probe (P1)

1. Look for the atlas skill script (`atlas.py`) and a mounted or cloned
   store (for this repository, `.atlas/<host>/<org>/<repo>` or a path
   the operator gives).
2. If either is missing, continue without Atlas, write
   `atlas: unavailable` in the run state and the report, and never
   invent history.

## Read (P1)

Search the store (`atlas.py search --root <store> "<query>"`) for:

- lessons: red-team method, split authority, test-corpus completeness,
  evidence and convergence;
- decisions that touch the owner families changed since the last tag;
- prior release-acceptance experience pages for this release line;
- the release-acceptance break-pattern document, if it exists.

Condense the results into a WISDOM BLOCK of at most one page:
recurring break classes with the journeys and owners where they hit,
lenses that found the most, known false-positive traps, and open known
issues. Paste the wisdom block into every walker brief. Atlas is
history, not truth about current behavior: every claim still needs a
repro at this SHA.

## Write (P6)

Write DRAFTS only, into the run directory, and hand them to the
coordinator who is the designated single Atlas writer. Never commit to
the store directly.

1. One `experience` page per pass: version, SHA, mode, journeys and
   lenses run, coverage ratio per journey, findings per owner family
   and severity, which lenses and journeys found bugs, false-positive
   rate after verification, model or host incidents.
2. An update to the break-pattern `document`: increment the count for
   each defect class seen, with journey, owner family and lens.
3. A new `lesson` ONLY when a class recurs across at least 2 passes.

Security details are summarized with `sensitivity: internal` and no
reproduction steps.
