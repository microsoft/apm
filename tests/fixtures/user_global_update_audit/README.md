# #3781 user-scope fixtures

`~/.apm` state written by tagged APM releases for a user manifest that declares
no APM packages: one self-defined stdio MCP server (targets copilot, claude)
and a root-local `~/.apm/agents/kit-agent.agent.md`.
`tests/integration/test_global_update_audit_materialization_lifecycle.py` replays
`apm update -g` then `apm audit --ci` against them. Nothing here is hand-written.

| Directory | Produced by | Notes |
|---|---|---|
| `v0.35.1/` | tag `v0.35.1` (787a97cc52) | Copilot deployment carries `content_hash: null`. |
| `v0.36.0/` | tag `v0.36.0` (1759e1e5d6) | Fingerprinted deployments. |

`~/.apm/apm_modules` is deliberately absent: it is disposable install state that
dotfile sync and fresh machines do not carry.

Regenerate (from the repository root):

```bash
git fetch --no-tags --depth=1 https://github.com/microsoft/apm.git refs/tags/v0.35.1:refs/tags/v0.35.1
mkdir -p /tmp/apm-v0351 && git archive v0.35.1 src | tar -x -C /tmp/apm-v0351
.venv/bin/python tests/fixtures/user_global_update_audit/generate_fixtures.py \
    --label v0.35.1 --source /tmp/apm-v0351/src --version 0.35.1
```

Repeat with `v0.36.0` / `--version 0.36.0`.
