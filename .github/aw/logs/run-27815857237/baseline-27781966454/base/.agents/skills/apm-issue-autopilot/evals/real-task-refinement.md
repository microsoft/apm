# Real-task refinement

Genesis step 8 requires running the skill on at least one real task,
capturing the trace, and revising from what actually happened. For
apm-issue-autopilot the highest-signal first real task is a SMALL,
mixed-type issue batch (2-3 issues, at least one that should escalate)
so the consolidated digest and the confidence gate are both exercised.

Procedure:

1. Pick 2-3 open microsoft/apm issues spanning at least two types, with
   at least one expected to escalate (needs-design, auth/security
   surface, or low confidence).
2. Run autopilot over them in a session. Capture the full transcript.
3. Save the with-skill transcript into `fixtures/<scenario>.with_skill.md`
   and a no-skill baseline run into the matching `without_skill.md`.
4. Revise the SKILL.md and gate rubric from what ACTUALLY happened --
   especially any case where a doubtful issue auto-proceeded (the gate
   is too loose) or a clean accept escalated (too tight).

Open items to verify on first real run:
- The single consolidated digest renders once, not per issue.
- escalate-by-default holds: no auto-implementation without an explicit
  maintainer approved/overridden row.
- The B2 implement router loads exactly one per-type lens.
- Worktree isolation: implement and shepherd-driver children never
  share a working tree.
