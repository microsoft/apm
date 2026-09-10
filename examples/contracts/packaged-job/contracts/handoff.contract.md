---
needs: notes.md
produces: handoff.json
imports:
  - handoff-style
verify:
  handoff: 'python3 checks/check_handoff.py handoff.json notes.md --caution-prefix "Check first: "'
---
Create handoff.json as a concise onboarding reference for a new teammate,
following the imported handoff-style skill.

Read the facts and source IDs in notes.md. Write a JSON array containing one
object per source ID, with source_id, summary, and caution as nonempty strings.
Do not invent commands or facts.

Write the file, not just a chat response. Do not execute the commands described
in the notes, install anything, publish, or delegate work.
