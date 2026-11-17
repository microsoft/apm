# 0.36.1 harness-track upgrade fixtures

Native MCP configs, manifests and lockfiles written by tagged APM releases.
`tests/integration/test_mcp_retired_shape_heal_lifecycle.py` replays them
against the binary under test. Nothing here is hand-written.

| Directory | Produced by | Notes |
|---|---|---|
| `v0.35.1/` | tag `v0.35.1` (787a97cc52) | Lockfile deployments carry `content_hash: null` (pre-fingerprint). |
| `v0.36.0/` | release commit `2769573f40` (v0.36.0) | Fingerprinted deployments. |

Each version has `project-mcp/` (project scope; targets vscode, copilot,
claude, gemini, antigravity, hermes), `user-mcp/` (`--global`; copilot, claude,
gemini, antigravity, hermes), `project-codex/` (a registry SSE remote plus an
HTTP server installed for codex only) and `user-agents/` (root-local `~/.apm/agents`
agent installed with `-g` for codex, kiro, copilot, claude). The loopback
registry URL is replaced with `__REGISTRY_URL__`.

Regenerate (from the repository root):

```bash
git fetch --no-tags https://github.com/microsoft/apm.git refs/tags/v0.35.1:refs/tags/v0.35.1
git worktree add --detach ./.rt-v0351 v0.35.1
.venv/bin/python tests/fixtures/upgrade_0361/t3/generate_fixtures.py \
    --label v0.35.1 --source ./.rt-v0351/src --version 0.35.1
git worktree add --detach ./.rt-v0360 2769573f40
.venv/bin/python tests/fixtures/upgrade_0361/t3/generate_fixtures.py \
    --label v0.36.0 --source ./.rt-v0360/src --version 0.36.0
git worktree remove ./.rt-v0351 ./.rt-v0360
```

`--version` pins the reported `apm_version` (an editable install otherwise
reports the virtualenv's package metadata).
