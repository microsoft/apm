---
name: devx-ux
description: >-
  Activate when designing or modifying CLI command surfaces, command help
  text, install/init/run flows, error wording, or first-run experience
  in the APM CLI -- even when the user does not say "UX" explicitly.
---

# Developer Tooling UX Skill

[Developer Tooling UX expert persona](../../agents/devx-ux-expert.agent.md)

## When to activate

- Changes to `src/apm_cli/cli.py` or any Click command definition
- New / renamed commands, subcommands, flags, or positional args
- Help strings (`help=`) and command docstrings
- Error messages that the user reads (not internal exceptions)
- `apm init`, `apm install`, `apm run`, `apm compile`, `apm preview`,
  `apm list`, `apm deps` flow changes
- README quickstart edits that change the first-run path

## Key rules

- Compare every flow against `npm` / `pip` / `cargo` / `gh` mental
  models -- justify any deviation.
- Default output is for humans; `--verbose` is for agents.
- Every error names the failure, the cause, and one next action.
- Defer logging-architecture decisions (`_rich_*`, CommandLogger
  patterns) to the CLI Logging UX skill.
