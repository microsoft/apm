# Troubleshooting

| Problem | Fix |
|---------|-----|
| `apm: command not found` | Install APM: `curl -sSL https://aka.ms/apm-unix \| sh` (macOS/Linux) or `irm https://aka.ms/apm-windows \| iex` (Windows). Ensure `/usr/local/bin` is in `$PATH`. |
| Authentication errors (401/403) | Set the correct token. Run `apm install --verbose` to see which token source is used. See [Authentication](./authentication.md). |
| File collision on install | A local file conflicts with a dependency file. Use `--force` to overwrite, or rename the local file. |
| Stale dependencies | Run `apm install --update` to refresh to latest refs. |
| Orphaned packages in lockfile | Run `apm prune` to remove packages no longer in apm.yml. |
| Security findings block install | Run `apm audit` to review findings, then `apm install --force` if acceptable. |
| Compilation not picking up changes | Run `apm compile --clean` to remove orphaned output, or `apm compile --watch` for auto-regeneration. |
| Windows encoding / charmap errors | Ensure all source files and CLI output use printable ASCII only (U+0020-U+007E). No emojis or unicode symbols. |
| Fine-grained PAT cannot access org | The PAT resource owner must be the org, not your user account. Recreate with org as owner. |
| SSO-protected repo access denied | Authorize the token: Settings > Tokens > Configure SSO for the org. |
