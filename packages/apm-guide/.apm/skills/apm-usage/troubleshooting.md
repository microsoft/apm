# Troubleshooting

| Problem | Fix |
|---------|-----|
| `apm: command not found` | On macOS with Homebrew, use `brew install apm` and follow Homebrew's `brew shellenv` guidance if needed. For Linux/macOS without Homebrew or Windows, see [Installation](./installation.md). On macOS/Linux native desktop installs, open a new shell after install; if setup was skipped, run the shell-specific `PATH` command the installer printed. Pip installs never edit profiles. |
| Disable automatic Unix PATH setup | Run the native installer with `APM_NO_MODIFY_PATH=1`. To re-enable after opting out, rerun a normal desktop native install with `APM_NO_MODIFY_PATH=0`. |
| Unix install/self-update ownership, destination, or pip fallback refusal | Use the owning Python's `-m pip` or original owner. Inspect unknown data without deleting it. For a fresh install choose another empty dedicated bundle or one `--prefix PATH`; do not combine `--prefix` with contradictory `APM_INSTALL_DIR` / `APM_LIB_DIR`. Otherwise use the owner's uninstall before migration. See [ownership rules](./installation.md#unix-ownership-and-migration). |
| Authentication errors (401/403) | Set the correct token. Run `apm install --verbose` to see which token source is used. See [Authentication](./authentication.md). |
| File collision on install | A local file conflicts with a dependency file. Use `--force` to overwrite, or rename the local file. |
| Stale dependencies | Run `apm install --update` to refresh to latest refs. |
| MCP path contains `.apm-resolution-staging` | Upgrade APM and retry the same install once. If it repeats, stop and report the redacted error and named MCP entry. Do not edit package files, delete the lockfile, or use `--refresh`/`--force` solely for this repair. |
| TLS verification failed | Install your corporate CA into the OS trust store. For a per-shell override, set `REQUESTS_CA_BUNDLE=/path/to/ca-bundle.pem`; `SSL_CERT_FILE` alone is not a reliable requests override. |
| Orphaned packages in lockfile | Run `apm prune` to remove packages no longer in apm.yml. |
| Security findings block install | Run `apm audit` to review findings, then `apm install --force` if acceptable. |
| Compilation not picking up changes | Run `apm compile --clean` to remove orphaned output, or `apm compile --watch` for auto-regeneration. |
| Windows encoding / charmap errors | Prefer printable ASCII (U+0020-U+007E) for CLI output and scripts: whether a non-ASCII character survives depends on the active code page, so it may fail. cp1252 encodes U+00E9 and U+2014 but raises UnicodeEncodeError on U+65E5 or an emoji. Package content may be UTF-8 -- `apm audit` flags hidden characters (zero-width, bidi), not ordinary non-ASCII text. |
| Fine-grained PAT cannot access org | The PAT resource owner must be the org, not your user account. Recreate with org as owner. |
| SSO-protected repo access denied | Authorize the token: Settings > Tokens > Configure SSO for the org. |
