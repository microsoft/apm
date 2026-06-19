# Implement lens: type/feature

Coverage gate: FAILING ACCEPTANCE TEST FIRST.

1. Translate the brief's `acceptance_tests` into one or more executable
   tests that assert the NEW behavior. Run them and confirm they FAIL
   (red) against HEAD -- this proves the feature is genuinely absent.
2. Implement the feature to the brief's `deliverable`, no more.
3. Run the acceptance tests to green, plus the full suite
   (`uv run --extra dev pytest -q`) to confirm no regression.
4. Record the acceptance test path(s) as `coverage_gate`.

Docs are part of the deliverable: fold the brief's `docs_required`
(Starlight pages under docs/, and the apm-usage resource files when
the change touches CLI commands, flags, dependency formats, auth flow,
policy schema, or primitive formats) and a CHANGELOG entry into the
same PR.

Scope fence: implement ONLY the accepted feature surface in the brief.
Adjacent enhancements are `non_goals`. If the feature requires a
breaking change, an auth/security surface, or a schema migration that
the brief did not flag, STOP and return `status: escalate` -- this is
beyond unattended scope.
