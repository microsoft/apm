# T2 upgrade fixtures (0.36.1 red team, #3732)

Consumer projects with one local `path: ../pkg` dependency and one self-defined
HTTP MCP server, one directory per manifest target spelling:

| case | declaration |
|---|---|
| singular | `target: claude` |
| singular-csv | `target: claude,copilot` |
| singular-alias | `target: vscode` |
| singular-all | `target: all` |
| plural | `targets: [claude, copilot]` |
| plural-alias | `targets: [vscode]` |
| plural-all | `targets: [all]` (plus a `.claude/` detection signal) |

Each directory holds `apm.yml`, `apm.lock.yaml` and every deployed file, exactly
as written by `apm install --no-policy` in a temp HOME with no network.

- `v0.35.1/`: produced by tag `v0.35.1` (787a97cc52):
  `git worktree add --detach ./.rt-v0351 v0.35.1` then
  `.venv/bin/python tests/fixtures/upgrade_0361/t2/generate.py --apm-src .rt-v0351/src --version 0.35.1 --out tests/fixtures/upgrade_0361/t2/v0.35.1`
- `v0.36.0/`: produced by the v0.36.0 release commit (2769573f40):
  `.venv/bin/python tests/fixtures/upgrade_0361/t2/generate.py --apm-src src --version 0.36.0 --out tests/fixtures/upgrade_0361/t2/v0.36.0`

Consumed by `tests/integration/test_target_manifest_upgrade_lifecycle.py`.
