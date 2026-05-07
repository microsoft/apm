---
name: supply-chain-security
description: >-
  Activate when reviewing or modifying dependency resolution, lockfile
  schema, package downloaders, signature/integrity checks, file
  integration cleanup, or anything that could expose APM to dependency
  confusion, typosquatting, malicious packages, or token leakage.
---

# Supply Chain Security Skill

[Supply chain security expert persona](../../agents/supply-chain-security-expert.agent.md)

## When to activate

- Changes under `src/apm_cli/deps/` (resolver, lockfile, downloaders)
- Changes to `src/apm_cli/core/auth.py` or `token_manager.py`
- Changes to `src/apm_cli/integration/cleanup.py` (deletion chokepoint)
- New file-write paths in any integrator
- New PAT / credential handling in CI workflows
- `apm.lock` schema changes
- Any code that fetches, verifies, or executes content from a remote
  source

## Key rules

- All path construction routes through
  `src/apm_cli/utils/path_security.py` (no ad-hoc `".." in x`).
- All deletions of deployed files route through
  `integration/cleanup.py:remove_stale_deployed_files()` (3 safety
  gates).
- All credential reads route through `AuthResolver` -- never raw
  `os.getenv` for token vars.
- Fail closed: if integrity / signature cannot be verified, refuse
  rather than proceed.
- Token values must never appear in user-facing strings.
