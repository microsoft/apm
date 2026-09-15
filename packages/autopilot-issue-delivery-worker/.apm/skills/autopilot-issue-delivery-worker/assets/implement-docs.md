# Implement lens: type/docs

Coverage gate: DOCS BUILD + LINK CHECK.

1. Make the documentation change to the brief's `deliverable`. Docs in
   this repo are Starlight content under `docs/src/content/docs/`; use
   relative cross-page links and correct frontmatter (title, sidebar
   order). When the change reflects CLI commands, flags, dependency
   formats, auth flow, policy schema, or primitive formats, ALSO update
   the matching apm-usage resource files (see repo doc-sync rules).
2. Build the docs site and confirm it succeeds with no broken internal
   links (run the docs build the repo defines under `docs/`). A build
   or link failure is a red gate -- fix before pushing. Record the
   build/link result as `coverage_gate`.
3. Keep prose pragmatic and succinct per the repo documentation rules;
   do not bloat pages.

Scope fence: edit ONLY the pages the brief names. Sweeping rewrites of
adjacent pages are `non_goals`. The main README is special -- if the
brief implies a README change, surface it for human approval rather
than editing it unattended (return `status: escalate` with the drift
described).
