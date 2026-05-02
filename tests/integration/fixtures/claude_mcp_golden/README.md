# Claude Code MCP golden fixtures

Captured live from the `claude` CLI shipped with Claude Code 2.1.126
(probed locally on macOS, 2025) by running `claude mcp add` against a
throwaway project + a backed-up `~/.claude.json`. These are the
canonical on-disk shapes the Claude Code runtime accepts and emits;
the APM `ClaudeClientAdapter` is asserted byte-equivalent against
them in `tests/integration/test_claude_mcp_schema_fidelity.py`.

We deliberately do NOT take a runtime dependency on the `claude` CLI
in CI — these fixtures freeze the contract at probe time and CI runs
the equivalence checks offline. When Claude Code ships a new schema
version, re-run the probe locally, refresh the fixtures, and
re-assert.

## Probe commands (Claude Code 2.1.126)

PROJECT scope, HTTP:

```
claude mcp add --scope project --transport http p-http https://example.invalid/mcp
```

PROJECT scope, SSE:

```
claude mcp add --scope project --transport sse p-sse https://example.invalid/sse
```

PROJECT scope, stdio (note: env uses `-e KEY=VAL`, NOT `--env KEY=VAL`):

```
claude mcp add --scope project --transport stdio p-stdio \
    -e FOO=bar -e BAZ=qux -- npx -y some-pkg --flag arg2
```

PROJECT scope, HTTP with auth header:

```
claude mcp add --scope project --transport http p-http-auth \
    https://api.example.invalid/mcp \
    --header "Authorization: Bearer XYZ"
```

USER scope, HTTP:

```
claude mcp add --scope user --transport http u-http https://example.invalid/mcp
```

USER scope, stdio:

```
claude mcp add --scope user --transport stdio u-stdio \
    -e BAZ=qux -- npx -y stdio-pkg --foo
```

LOCAL scope (default), HTTP — NOT implemented by APM (intentional, see
adapter docstring), captured here for completeness:

```
claude mcp add --transport http l-http https://example.invalid/mcp
```

Local scope writes under `projects.<absolute_project_path>.mcpServers`
in `~/.claude.json`.
