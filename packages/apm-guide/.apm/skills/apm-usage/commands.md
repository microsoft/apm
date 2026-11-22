# CLI Command Reference

`apm --version` shows an optional commit SHA only when embedded at build time
or verified against the source checkout containing the loaded APM package.
Installed wheels do not borrow an enclosing repository's SHA. Source/editable
checkouts, including forks and worktrees, show their revision when Git can
verify it; otherwise the SHA is omitted.
Standalone `apm --version` does not require Git to be installed.

## User metadata root

`APM_HOME` is the root for APM-owned user metadata: `apm.yml`,
`apm.lock.yaml`, `apm_modules/`, `config.json`, `marketplaces.json`, lifecycle
trust, logs, lock, and deployment receipts. It defaults to `~/.apm` (empty
counts as unset); every `~/.apm` path below describes that default. Global
install, update, uninstall, deps, lock, compile, marketplace, lifecycle, and
config commands honor it. Target deployments do not: target-specific home
variables take precedence for their own destinations, with the operating-system
home as the fallback. Runtimes (`~/.apm/runtimes/`), the `--global`
`.apm-local/` identity in your home, and the download cache stay outside it.
A relative `APM_HOME` resolves against the starting directory (APM errors if that directory is gone). State-changing
commands also hold `~/.apm-local/.apm-lifecycle.lock`, so different `APM_HOME`
values serialize instead of racing on shared projects or target directories.
APM does not migrate an existing `~/.apm`.

## Project setup

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm init [NAME]` | Initialize a new APM project; names must be non-empty and must not contain path separators or equal `..`. A derived filesystem-root name falls back to `my-project`. | `-y` skip prompts, `--name`/`--description`/`--version`/`--author` set manifest metadata with `-y` and seed the prompt defaults without it (`--name` is for in-place init and is ignored with a warning when `NAME` is given; empty `--version` exits 1), `--target` comma-separated targets (CLI aliases such as `agents` and `vscode` are persisted as canonical `copilot`, so the generated manifest is immediately installable; `intellij` is an IDE, not a target, and exits 1 before writing). `--plugin` and `--marketplace` were removed in 0.37: they exit 2 without writing and name `apm plugin init` / `apm marketplace init`. On re-init over an existing `apm.yml`, its targets carry over as an equivalent `targets:` list (for example `agents` -> `copilot`, `agy` -> `antigravity`); a legacy target with no `targets:` equivalent (`intellij`) exits 1 under `-y` or non-interactive stdin before writing (pass `--target`), and the rewritten manifest is parsed back before it is written. After init, Next Steps suggests `agentrc init` only when agentrc is in PATH and no agent instruction files exist. |
| `apm init --discover` | Preview existing agent content without writing or executing it. Apply adds missing local package references to `apm.yml`, leaving source files unchanged; reruns do not duplicate declarations. Unsupported content is reported, not converted. Run `apm install` separately. | `--apply`/`--write` request a manifest merge and imply `--discover`; `--yes` supplies noninteractive consent (unlike plain `init --yes`, this never overwrites the manifest). `--format text\|json\|yaml`; `-g`/`--global` for user scope with absolute/home-directory references. Select `--target` on install, not discovery. |
| `apm plugin init [NAME]` | Scaffold a plugin project (`plugin.json` + `apm.yml`). No-flag default scaffolds the legacy Claude-compatible layout. Pass `--format agent-plugin` to explicitly scaffold a portable Agent Plugins v1 project instead; `--claude-plugin` (or `--format plugin\|claude\|claude-plugin`) is the explicit form of the default. `--format` and `--claude-plugin` are mutually exclusive. | `-y` skip prompts, `--target` comma-separated targets, `--format [agent-plugin\|plugin\|claude\|claude-plugin]`, `--claude-plugin`, `--version`/`--author`/`--description` set metadata, `-v`/`--verbose` |

## Dependency management

Repeating an existing positional package without a requested change reports
`already in apm.yml` and preserves its declaration. An otherwise unchanged
manifest is not rewritten; changed refs and explicit skill additions or resets
still update it.
Explicit HTTPS upgrades and Git-qualified marketplace requests are not no-ops
when they change the stored transport or default-registry routing.

During install, uninstall or prune, `Cleaned N stale deployed files for
DEPENDENCY:` names the affected dependency and lists each removed path below it
(a skill directory once, with a trailing `/`). A local source path in the
header identifies the package; cleanup removes deployed files, not source files.

`apm install` rejects incompatible immutable requirements for one package
identity and reports both dependency paths and refs. Equivalent tag/SHA
spellings are accepted. `--frozen` verifies transitive immutable requirements
as parent manifests become available, including after cold-cache downloads.
Short pins must match the locked full commit's prefix. Unchanged locked refs
replay without ref discovery; new names may require a lookup.
Align manifest refs and regenerate the lockfile with a normal install to repair
a collapsed graph; `--force` does not bypass this check.

`apm deps list` recognizes declared or locked aliases at their canonical
install paths and displays logical dependency identities, including in user
scope. It reads only the selected store; it does not substitute another store.

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm install [PKGS...]` | Install APM, MCP, and LSP dependencies (supports APM packages, Claude skills (SKILL.md), and plugin collections (plugin.json)); one effective target decision drives package, MCP, and LSP phases; dependency aliases materialize at the canonical install path, not duplicate noncanonical paths; plain/frozen installs replay matching dependency and provider lock entries, and a mutable ref without a matching lock entry resolves upstream instead of stale bare-cache refs, including with unrelated or sibling lock entries; a successful non-dry-run install also reconciles deployed artifacts, lockfile ownership, and merge-hook config/sidecar entries for any target dropped from `targets:`; project-local `.apm/` cleanup requires recorded concrete target ownership (including after whole-tree deletion), preserves ambiguous legacy output, user edits, and surviving package/target claims, including declared targets skipped by `--target`; `apm install PKG` (project or `-g`) also installs declared local-path dependencies the lockfile does not record yet, keeps unrelated packages' lock entries but removes dependencies PKG no longer declares and nothing else retained needs from the lockfile, `apm_modules/` and deployed files | `--update` (deprecated; prefer `apm update`) refresh refs without accepting stale bare-cache answers, `--refresh` re-fetch all deps from upstream and re-resolve all ref pins, `--force` overwrite collisions and permit deployment after critical built-in scan findings (does NOT refresh refs by itself; `apm update --force` still requires upstream truth), `--frozen` requires the lockfile (unless `apm.yml` declares no APM, MCP or LSP dependency, a converged no-op that `apm audit --ci` also accepts) and checks manifest refs, pins, providers, transport, local-source content and available MCP declarations before the pipeline; unavailable package MCP evidence is verified after hydration, before deployment (see [MCP-only lock and frozen behavior](#mcp-only-lock-and-frozen-behavior); not network freshness; mutually exclusive with `--update`, package additions, and `--mcp`; before deployment it also fails when any deployed file (instruction, agent, command or skill) differs from its locked hash -- restore by deleting the listed files, rerunning `apm install`, and committing (`--force` also restores but overwrites every edit and skips the critical-findings block); normal install creates or repairs lock state; packages the lock lists but `apm.yml` no longer declares keep their deployed files, `apm_modules/` checkout and lock rows, with an info line naming them -- run `apm install` to remove them), `--dry-run` (runs the install pipeline up to its first write: contacts sources and applies the same policy and MCP checks; no package/deployment writes; positional packages and ref changes are previewed without changing an existing `apm.yml`; project bootstrap keeps its new manifest and explicit targets, while absent global `-g` bootstrap uses temporary preview state and does not create `~/.apm`; stale-file cleanup is not previewed), `--verbose`, `--only [apm\|mcp]` (APM packages or MCP/LSP service dependencies), `--target` (comma-separated, e.g. `--target claude,cursor`; resolution chain `--target` > apm.yml `targets:` > `apm config set target ...` > auto-detect; this decision is reused by package, MCP, and LSP phases; unresolved required service work fails non-zero before manifest or package writes, and native MCP/LSP write failures also fail non-zero; `intellij` is MCP-only and writes JetBrains Copilot's user-scope config; explicit lists are exact, so `intellij,claude` writes those two MCP configs and `all,intellij` adds JetBrains to `all`; on auto-bootstrap when no `apm.yml` exists, recognized manifest target(s) are persisted to the new manifest's `targets:` field so a later bare `apm update` reuses them (`intellij` is an IDE, not a target: every bootstrap, project or global `--mcp`, refuses it before any write; use `copilot`, or create `apm.yml` first to deploy JetBrains MCP config), except absent global dry-runs only preview targets in temporary state; the bootstrapped manifest omits `author` instead of recording the local Git identity; `--target all` deprecated, see `apm compile --all`; use `kiro` for Kiro IDE; use `grok-build` for stable Grok Build rules, agents, commands, skills, and `AGENTS.md`; use `copilot-cowork` with `--global` after `apm experimental enable copilot-cowork`; use `grok-cloud` after `apm experimental enable grok-cloud` to deploy skills only to `.grok/skills/`; use explicit target `hermes` to deploy skills and home-scoped MCP servers to `$HERMES_HOME/config.yaml` (or `~/.hermes/config.yaml` when unset or blank); run `apm compile` separately for `AGENTS.md`), use explicit target `grok-bot` to deploy skills only to `agent-data/workflows/` (project) or `~/agent-data/workflows/` (`--global`), `--dev` (moves a package already in `dependencies`; without `--dev`, a package already in `devDependencies` stays there), `-g` global (MCP deploys only to user-scope runtimes: Copilot CLI, Claude Code, Cursor, OpenCode, Codex CLI, Gemini CLI, Antigravity CLI, Kiro, Windsurf, JetBrains Copilot, and Hermes when selected explicitly), `--trust-transitive-mcp` (unapproved transitive MCP for this run only; never saved; keep one with `apm approve <pkg>`), `--parallel-downloads N`, `--trust-bin` / `--no-trust-bin` (per-invocation consent for marketplace-plugin bin/ deployment: `--trust-bin` suppresses the trust-posture warning, `--no-trust-bin` skips bin/ even if policy allows; default deploys with a warning), `--allow-insecure`, `--allow-insecure-host HOSTNAME`, `--skill NAME` install named skills from a dependency that exposes selectable skills (repeatable; plugin manifests accept a leaf name or source-relative path under skills/; a CLI name or path that matches no declared skill fails before manifest or lockfile commit with available names; a non-empty persisted `skills:` selection matching no available skill fails nonzero with the dependency, requested names, and available names (or none) before any of that dependency's primitives are written, preserving its previously deployed bytes and ownership/hash records across repeated failures; correct `skills:` in apm.yml to restore normal reconciliation, including valid narrowing and dependency removal; persisted in apm.yml only on a successful CLI match; additive across separate installs -- a later `--skill X` adds to the existing pin (union) rather than replacing it, so previously deployed skills are never silently removed; `'*'` resets to the full bundle; drop a single skill by editing the `skills:` list in apm.yml then re-running install), `--legacy-skill-paths` restore per-client skill dirs, `--mcp NAME` add MCP entry using that same effective target decision (the shared decision applies, so `apm install --mcp NAME --target intellij` writes only JetBrains Copilot's MCP config; compilation target policy applies to every explicitly selected target; `apm install -g --mcp NAME` writes user-scope and bypasses the project-scope gate by design), `--transport`, `--url`, `--env KEY=VAL`, `--header KEY=VAL`, `--mcp-version`, `--registry URL` custom MCP registry, `--policy SOURCE` also enforce a policy (local path, `owner/repo`, or `https://` URL) for plain-git, other-host, or air-gapped setups -- combined tighten-only with any discovered org policy, never loosening it; an unloadable source fails the command before any write, whatever `policy.fetch_failure_default` says; effective sources are always printed; not valid with `-g` or `--no-policy`, `--root DIR` redirect writes (`apm_modules/`, lockfile, `.gitignore`, integrated harness files) under DIR while `apm.yml`/`.apm/`/local deps resolve from `$PWD` (mirrors `pip install --target`; created if missing; not valid with `-g`/`--global`, which exits 2). Explicit plugin component paths must resolve inside the plugin root; missing declarations fail before deployment and lockfile commit. |
| `apm install [PKGS...]` | Install APM, MCP, and LSP dependencies (supports APM packages, Claude skills (SKILL.md), and plugin collections (plugin.json)); one effective target decision drives package, MCP, and LSP phases; dependency aliases materialize at the canonical install path, not duplicate noncanonical paths; plain/frozen installs replay matching dependency and provider lock entries, and a mutable ref without a matching lock entry resolves upstream instead of stale bare-cache refs, including with unrelated or sibling lock entries; a successful non-dry-run install also reconciles deployed artifacts, lockfile ownership, and merge-hook config/sidecar entries for any target dropped from `targets:`; project-local `.apm/` cleanup requires recorded concrete target ownership (including after whole-tree deletion), preserves ambiguous legacy output, user edits, and surviving package/target claims, including declared targets skipped by `--target`; `apm install PKG` (project or `-g`) also installs declared local-path dependencies the lockfile does not record yet, keeps unrelated packages' lock entries but removes dependencies PKG no longer declares and nothing else retained needs from the lockfile, `apm_modules/` and deployed files | `--update` (deprecated; prefer `apm update`) refresh refs without accepting stale bare-cache answers, `--refresh` re-fetch all deps from upstream and re-resolve all ref pins, `--force` overwrite collisions and permit deployment after critical built-in scan findings (does NOT refresh refs by itself; `apm update --force` still requires upstream truth), `--frozen` (default when `CI` is set and not empty, `0` or `false` and `apm.lock.yaml` exists; without a lockfile install resolves and writes one; `--no-frozen`, `--update`, `--refresh`, positional packages and `--mcp` opt out) requires the lockfile and checks manifest refs, pins, providers, transport, local-source content and available MCP declarations before the pipeline; unavailable package MCP evidence is verified after hydration, before deployment (see [MCP-only lock and frozen behavior](#mcp-only-lock-and-frozen-behavior); not network freshness; mutually exclusive with `--update`, package additions, and `--mcp`; before deployment it also fails when any deployed file (instruction, agent, command or skill) differs from its locked hash -- restore by deleting the listed files, rerunning `apm install`, and committing (`--force` also restores but overwrites every edit and skips the critical-findings block); normal install creates or repairs lock state; packages the lock lists but `apm.yml` no longer declares keep their deployed files, `apm_modules/` checkout and lock rows, with an info line naming them -- run `apm install` to remove them), `--dry-run` (no package/deployment writes; positional packages and ref changes are previewed without changing an existing `apm.yml`; project bootstrap keeps its new manifest and explicit targets, while absent global `-g` bootstrap uses temporary preview state and does not create `~/.apm`; stale-file cleanup is not previewed), `--verbose`, `--only [apm\|mcp]` (APM packages or MCP/LSP service dependencies), `--target` (comma-separated, e.g. `--target claude,cursor`; resolution chain `--target` > apm.yml `targets:` > `apm config set target ...` > auto-detect; this decision is reused by package, MCP, and LSP phases; unresolved required service work fails non-zero before manifest or package writes, and native MCP/LSP write failures also fail non-zero; `intellij` is MCP-only and writes JetBrains Copilot's user-scope config; explicit lists are exact, so `intellij,claude` writes those two MCP configs and `all,intellij` adds JetBrains to `all`; on auto-bootstrap when no `apm.yml` exists, recognized manifest target(s) are persisted to the new manifest's `targets:` field so a later bare `apm update` reuses them (`intellij` is an IDE, not a target: every bootstrap, project or global `--mcp`, refuses it before any write; use `copilot`, or create `apm.yml` first to deploy JetBrains MCP config), except absent global dry-runs only preview targets in temporary state; the bootstrapped manifest omits `author` instead of recording the local Git identity; `--target all` deprecated, see `apm compile --all`; use `kiro` for Kiro IDE; use `grok-build` for stable Grok Build rules, agents, commands, skills, and `AGENTS.md`; use `copilot-cowork` with `--global` after `apm experimental enable copilot-cowork`; use `grok-cloud` after `apm experimental enable grok-cloud` to deploy skills only to `.grok/skills/`; use explicit target `hermes` to deploy skills and home-scoped MCP servers to `$HERMES_HOME/config.yaml` (or `~/.hermes/config.yaml` when unset or blank); run `apm compile` separately for `AGENTS.md`), use explicit target `grok-bot` to deploy skills only to `agent-data/workflows/` (project) or `~/agent-data/workflows/` (`--global`), `--dev` (moves a package already in `dependencies`; without `--dev`, a package already in `devDependencies` stays there), `-g` global (MCP deploys only to user-scope runtimes: Copilot CLI, Claude Code, Cursor, OpenCode, Codex CLI, Gemini CLI, Antigravity CLI, Kiro, Windsurf, JetBrains Copilot, and Hermes when selected explicitly), `--trust-transitive-mcp` (unapproved transitive MCP for this run only; never saved; keep one with `apm approve <pkg>`), `--parallel-downloads N`, `--trust-bin` / `--no-trust-bin` (per-invocation consent for marketplace-plugin bin/ deployment: `--trust-bin` suppresses the trust-posture warning, `--no-trust-bin` skips bin/ even if policy allows; default deploys with a warning), `--allow-insecure`, `--allow-insecure-host HOSTNAME`, `--skill NAME` install named skills from a dependency that exposes selectable skills (repeatable; plugin manifests accept a leaf name or source-relative path under skills/; a CLI name or path that matches no declared skill fails before manifest or lockfile commit with available names; a non-empty persisted `skills:` selection matching no available skill fails nonzero with the dependency, requested names, and available names (or none) before any of that dependency's primitives are written, preserving its previously deployed bytes and ownership/hash records across repeated failures; correct `skills:` in apm.yml to restore normal reconciliation, including valid narrowing and dependency removal; persisted in apm.yml only on a successful CLI match; additive across separate installs -- a later `--skill X` adds to the existing pin (union) rather than replacing it, so previously deployed skills are never silently removed; `'*'` resets to the full bundle; drop a single skill by editing the `skills:` list in apm.yml then re-running install), `--legacy-skill-paths` restore per-client skill dirs, `--mcp NAME` add MCP entry using that same effective target decision (the shared decision applies, so `apm install --mcp NAME --target intellij` writes only JetBrains Copilot's MCP config; compilation target policy applies to every explicitly selected target; `apm install -g --mcp NAME` writes user-scope and bypasses the project-scope gate by design), `--transport`, `--url`, `--env KEY=VAL`, `--header KEY=VAL`, `--mcp-version`, `--registry URL` custom MCP registry, `--policy SOURCE` also enforce a policy (local path, `owner/repo`, or `https://` URL) for plain-git, other-host, or air-gapped setups -- combined tighten-only with any discovered org policy, never loosening it; an unloadable source fails the command before any write, whatever `policy.fetch_failure_default` says; effective sources are always printed; not valid with `-g` or `--no-policy`, `--root DIR` redirect writes (`apm_modules/`, lockfile, `.gitignore`, integrated harness files) under DIR while `apm.yml`/`.apm/`/local deps resolve from `$PWD` (mirrors `pip install --target`; created if missing; not valid with `-g`/`--global`, which exits 2). Explicit plugin component paths must resolve inside the plugin root; missing declarations fail before deployment and lockfile commit. |
| `apm install [PKGS...]` | Install APM, MCP, and LSP dependencies (supports APM packages, Claude skills (SKILL.md), and plugin collections (plugin.json)); one effective target decision drives package, MCP, and LSP phases; dependency aliases materialize at the canonical install path, not duplicate noncanonical paths; plain/frozen installs replay matching dependency and provider lock entries, and a mutable ref without a matching lock entry resolves upstream instead of stale bare-cache refs, including with unrelated or sibling lock entries; a successful non-dry-run install also reconciles deployed artifacts, lockfile ownership, and merge-hook config/sidecar entries for any target dropped from `targets:`; project-local `.apm/` cleanup requires recorded concrete target ownership (including after whole-tree deletion), preserves ambiguous legacy output, user edits, and surviving package/target claims, including declared targets skipped by `--target`; a run narrowed by `--target` keeps a skipped runtime's MCP entry while the server stays declared and admitted there and the manifest still selects that runtime (undeclared or `targets:`-excluded servers are still removed); `apm install PKG` (project or `-g`) also installs declared local-path dependencies the lockfile does not record yet, keeps unrelated packages' lock entries but removes dependencies PKG no longer declares and nothing else retained needs from the lockfile, `apm_modules/` and deployed files | `--update` (deprecated; prefer `apm update`) refresh refs without accepting stale bare-cache answers, `--refresh` re-fetch all deps from upstream and re-resolve all ref pins, `--force` overwrite collisions and permit deployment after critical built-in scan findings (does NOT refresh refs by itself; `apm update --force` still requires upstream truth), `--frozen` requires the lockfile (unless `apm.yml` declares no APM, MCP or LSP dependency, a converged no-op that `apm audit --ci` also accepts) and checks manifest refs, pins, providers, transport, local-source content and available MCP declarations before the pipeline; unavailable package MCP evidence is verified after hydration, before deployment (see [MCP-only lock and frozen behavior](#mcp-only-lock-and-frozen-behavior); not network freshness; mutually exclusive with `--update`, package additions, and `--mcp`; before deployment it also fails when any deployed file (instruction, agent, command or skill) differs from its locked hash -- restore by deleting the listed files, rerunning `apm install`, and committing (`--force` also restores but overwrites every edit and skips the critical-findings block); normal install creates or repairs lock state; packages the lock lists but `apm.yml` no longer declares keep their deployed files, `apm_modules/` checkout and lock rows, with an info line naming them -- run `apm install` to remove them), `--dry-run` (runs the install pipeline up to its first write: contacts sources and applies the same policy and MCP checks; no package/deployment writes; positional packages and ref changes are previewed without changing an existing `apm.yml`; project bootstrap keeps its new manifest and explicit targets, while absent global `-g` bootstrap uses temporary preview state and does not create `~/.apm`; stale-file cleanup is not previewed), `--verbose`, `--only [apm\|mcp]` (APM packages or MCP/LSP service dependencies), `--target` (comma-separated, e.g. `--target claude,cursor`; resolution chain `--target` > apm.yml `targets:` > `apm config set target ...` > auto-detect; this decision is reused by package, MCP, and LSP phases; unresolved required service work fails non-zero before manifest or package writes, and native MCP/LSP write failures also fail non-zero; `intellij` is MCP-only and writes JetBrains Copilot's user-scope config; explicit lists are exact, so `intellij,claude` writes those two MCP configs and `all,intellij` adds JetBrains to `all`; on auto-bootstrap when no `apm.yml` exists, recognized manifest target(s) are persisted to the new manifest's `targets:` field so a later bare `apm update` reuses them (`intellij` is an IDE, not a target: every bootstrap, project or global `--mcp`, refuses it before any write; use `copilot`, or create `apm.yml` first to deploy JetBrains MCP config), except absent global dry-runs only preview targets in temporary state; the bootstrapped manifest omits `author` instead of recording the local Git identity; `--target all` deprecated, see `apm compile --all`; use `kiro` for Kiro IDE; use `grok-build` for stable Grok Build rules, agents, commands, skills, and `AGENTS.md`; use `copilot-cowork` with `--global` after `apm experimental enable copilot-cowork`; use `grok-cloud` after `apm experimental enable grok-cloud` to deploy skills only to `.grok/skills/`; use explicit target `hermes` to deploy skills and home-scoped MCP servers to `$HERMES_HOME/config.yaml` (or `~/.hermes/config.yaml` when unset or blank); run `apm compile` separately for `AGENTS.md`), use explicit target `grok-bot` to deploy skills only to `agent-data/workflows/` (project) or `~/agent-data/workflows/` (`--global`), `--dev` (moves a package already in `dependencies`; without `--dev`, a package already in `devDependencies` stays there), `-g` global (MCP deploys only to user-scope runtimes: Copilot CLI, Claude Code, Cursor, OpenCode, Codex CLI, Gemini CLI, Antigravity CLI, Kiro, Windsurf, JetBrains Copilot, and Hermes when selected explicitly), `--trust-transitive-mcp` (unapproved transitive MCP for this run only; never saved; keep one with `apm approve <pkg>`), `--parallel-downloads N`, `--trust-bin` / `--no-trust-bin` (per-invocation consent for marketplace-plugin bin/ deployment: `--trust-bin` suppresses the trust-posture warning, `--no-trust-bin` skips bin/ even if policy allows; default deploys with a warning), `--allow-insecure`, `--allow-insecure-host HOSTNAME`, `--skill NAME` install named skills from a dependency that exposes selectable skills (repeatable; plugin manifests accept a leaf name or source-relative path under skills/; a CLI name or path that matches no declared skill fails before manifest or lockfile commit with available names; a non-empty persisted `skills:` selection matching no available skill fails nonzero with the dependency, requested names, and available names (or none) before any of that dependency's primitives are written, preserving its previously deployed bytes and ownership/hash records across repeated failures; correct `skills:` in apm.yml to restore normal reconciliation, including valid narrowing and dependency removal; persisted in apm.yml only on a successful CLI match; additive across separate installs -- a later `--skill X` adds to the existing pin (union) rather than replacing it, so previously deployed skills are never silently removed; `'*'` resets to the full bundle; drop a single skill by editing the `skills:` list in apm.yml then re-running install), `--legacy-skill-paths` restore per-client skill dirs, `--mcp NAME` add MCP entry using that same effective target decision (the shared decision applies, so `apm install --mcp NAME --target intellij` writes only JetBrains Copilot's MCP config; compilation target policy applies to every explicitly selected target; `apm install -g --mcp NAME` writes user-scope and bypasses the project-scope gate by design), `--transport`, `--url`, `--env KEY=VAL`, `--header KEY=VAL`, `--mcp-version`, `--registry URL` custom MCP registry, `--root DIR` redirect writes (`apm_modules/`, lockfile, `.gitignore`, integrated harness files) under DIR while `apm.yml`/`.apm/`/local deps resolve from `$PWD` (mirrors `pip install --target`; created if missing; not valid with `-g`/`--global`, which exits 2). Explicit plugin component paths must resolve inside the plugin root; missing declarations fail before deployment and lockfile commit. |
| `apm install [PKGS...]` | Install APM, MCP, and LSP dependencies (supports APM packages, Claude skills (SKILL.md), and plugin collections (plugin.json)); one effective target decision drives package, MCP, and LSP phases; dependency aliases materialize at the canonical install path, not duplicate noncanonical paths; plain/frozen installs replay matching dependency and provider lock entries, and a mutable ref without a matching lock entry resolves upstream instead of stale bare-cache refs, including with unrelated or sibling lock entries; a successful non-dry-run install also reconciles deployed artifacts, lockfile ownership, and merge-hook config/sidecar entries for any target dropped from `targets:`; project-local `.apm/` cleanup requires recorded concrete target ownership (including after whole-tree deletion), preserves ambiguous legacy output, user edits, and surviving package/target claims, including declared targets skipped by `--target`; a run narrowed by `--target` keeps a skipped runtime's MCP entry while the server stays declared and admitted there and the manifest still selects that runtime (undeclared or `targets:`-excluded servers are still removed); `apm install PKG` (project or `-g`) also installs declared local-path dependencies the lockfile does not record yet, keeps unrelated packages' lock entries but removes dependencies PKG no longer declares and nothing else retained needs from the lockfile, `apm_modules/` and deployed files | `--update` (deprecated; prefer `apm update`) refresh refs without accepting stale bare-cache answers, `--refresh` re-fetch all deps from upstream and re-resolve all ref pins, `--force` overwrite collisions and permit deployment after critical built-in scan findings (does NOT refresh refs by itself; `apm update --force` still requires upstream truth), `--frozen` (default when `CI` is set and not empty, `0` or `false` and `apm.lock.yaml` exists; without a lockfile install resolves and writes one; `--no-frozen`, `--update`, `--refresh`, positional packages and `--mcp` opt out) requires the lockfile and checks manifest refs, pins, providers, transport, local-source content and available MCP declarations before the pipeline; unavailable package MCP evidence is verified after hydration, before deployment (see [MCP-only lock and frozen behavior](#mcp-only-lock-and-frozen-behavior); not network freshness; mutually exclusive with `--update`, package additions, and `--mcp`; before deployment it also fails when any deployed file (instruction, agent, command or skill) differs from its locked hash -- restore by deleting the listed files, rerunning `apm install`, and committing (`--force` also restores but overwrites every edit and skips the critical-findings block); normal install creates or repairs lock state; packages the lock lists but `apm.yml` no longer declares keep their deployed files, `apm_modules/` checkout and lock rows, with an info line naming them -- run `apm install` to remove them), `--dry-run` (no package/deployment writes; positional packages and ref changes are previewed without changing an existing `apm.yml`; project bootstrap keeps its new manifest and explicit targets, while absent global `-g` bootstrap uses temporary preview state and does not create `~/.apm`; stale-file cleanup is not previewed), `--verbose`, `--only [apm\|mcp]` (APM packages or MCP/LSP service dependencies), `--target` (comma-separated, e.g. `--target claude,cursor`; resolution chain `--target` > apm.yml `targets:` > `apm config set target ...` > auto-detect; this decision is reused by package, MCP, and LSP phases; unresolved required service work fails non-zero before manifest or package writes, and native MCP/LSP write failures also fail non-zero; `intellij` is MCP-only and writes JetBrains Copilot's user-scope config; explicit lists are exact, so `intellij,claude` writes those two MCP configs and `all,intellij` adds JetBrains to `all`; on auto-bootstrap when no `apm.yml` exists, recognized manifest target(s) are persisted to the new manifest's `targets:` field so a later bare `apm update` reuses them (`intellij` is an IDE, not a target: every bootstrap, project or global `--mcp`, refuses it before any write; use `copilot`, or create `apm.yml` first to deploy JetBrains MCP config), except absent global dry-runs only preview targets in temporary state; the bootstrapped manifest omits `author` instead of recording the local Git identity; `--target all` deprecated, see `apm compile --all`; use `kiro` for Kiro IDE; use `grok-build` for stable Grok Build rules, agents, commands, skills, and `AGENTS.md`; use `copilot-cowork` with `--global` after `apm experimental enable copilot-cowork`; use `grok-cloud` after `apm experimental enable grok-cloud` to deploy skills only to `.grok/skills/`; use explicit target `hermes` to deploy skills and home-scoped MCP servers to `$HERMES_HOME/config.yaml` (or `~/.hermes/config.yaml` when unset or blank); run `apm compile` separately for `AGENTS.md`), use explicit target `grok-bot` to deploy skills only to `agent-data/workflows/` (project) or `~/agent-data/workflows/` (`--global`), `--dev` (moves a package already in `dependencies`; without `--dev`, a package already in `devDependencies` stays there), `-g` global (MCP deploys only to user-scope runtimes: Copilot CLI, Claude Code, Cursor, OpenCode, Codex CLI, Gemini CLI, Antigravity CLI, Kiro, Windsurf, JetBrains Copilot, and Hermes when selected explicitly), `--trust-transitive-mcp` (unapproved transitive MCP for this run only; never saved; keep one with `apm approve <pkg>`), `--parallel-downloads N`, `--trust-bin` / `--no-trust-bin` (per-invocation consent for marketplace-plugin bin/ deployment: `--trust-bin` suppresses the trust-posture warning, `--no-trust-bin` skips bin/ even if policy allows; default deploys with a warning), `--allow-insecure`, `--allow-insecure-host HOSTNAME`, `--skill NAME` install named skills from a dependency that exposes selectable skills (repeatable; plugin manifests accept a leaf name or source-relative path under skills/; a CLI name or path that matches no declared skill fails before manifest or lockfile commit with available names; a non-empty persisted `skills:` selection matching no available skill fails nonzero with the dependency, requested names, and available names (or none) before any of that dependency's primitives are written, preserving its previously deployed bytes and ownership/hash records across repeated failures; correct `skills:` in apm.yml to restore normal reconciliation, including valid narrowing and dependency removal; persisted in apm.yml only on a successful CLI match; additive across separate installs -- a later `--skill X` adds to the existing pin (union) rather than replacing it, so previously deployed skills are never silently removed; `'*'` resets to the full bundle; drop a single skill by editing the `skills:` list in apm.yml then re-running install), `--legacy-skill-paths` restore per-client skill dirs, `--mcp NAME` add MCP entry using that same effective target decision (the shared decision applies, so `apm install --mcp NAME --target intellij` writes only JetBrains Copilot's MCP config; compilation target policy applies to every explicitly selected target; `apm install -g --mcp NAME` writes user-scope and bypasses the project-scope gate by design), `--transport`, `--url`, `--env KEY=VAL`, `--header KEY=VAL`, `--mcp-version`, `--registry URL` custom MCP registry, `--root DIR` redirect writes (`apm_modules/`, lockfile, `.gitignore`, integrated harness files) under DIR while `apm.yml`/`.apm/`/local deps resolve from `$PWD` (mirrors `pip install --target`; created if missing; not valid with `-g`/`--global`, which exits 2). Explicit plugin component paths must resolve inside the plugin root; missing declarations fail before deployment and lockfile commit. |
| `apm targets` | Show resolved deployment targets for the current project (Click group; reads filesystem signals; works with or without `apm.yml`) | `--all` also include the stable explicit-only targets (`agent-skills`, `antigravity`, `grok-bot`, `hermes`; only meaningful with `--json`), `--json` machine-readable output. No provenance line is printed (the table is the provenance). |
| `apm uninstall PKGS...` | Remove packages; reconcile tracked files, hooks, MCP, and LSP. Accepts `owner/repo`, `name@marketplace` (also for object-form `{name, marketplace}` entries, offline), exact declared local paths, or portable `_local/<name>` keys with lock metadata. A local bundle path (`apm install <bundle>`) is refused with a message that bundles cannot be uninstalled; an MCP server name is refused with its removal route (delete it under `dependencies.mcp` and run `apm install`, uninstall the package that brings it in, or, for a local-bundle server, a note that bundles cannot be uninstalled yet, #3887). A package declared more than once (both sections or several spellings) loses every declaration. A requested package that another installed package still needs is removed from `apm.yml` only; it stays installed and its lock row is re-parented, so `apm install --frozen` keeps passing (#4749). Selection is atomic: missing or ambiguous identifiers exit before scripts or APM writes. Direct and orphan materialized directories are deleted before target cleanup or manifest and lockfile writes. Deletion failure exits 1 without `Uninstall complete` and retains declarations, the on-disk lockfile, and deployed ownership; earlier deletions are not rolled back. Fix permission or file-lock errors, or the unsafe path after a containment refusal, then retry the same command. Restore with `apm install` (`apm install --global` for user scope). An edited or unproven tracked file (no receipt or locked render) refuses before any change, naming each file and two remedies: move aside files holding your edits, run `apm install` with the source reachable, then uninstall; or move the files aside. A later undeletable tracked file keeps `apm.yml`, drops already-removed files from `apm.lock.yaml` so it claims only what is still on disk, and may leave removed directories; resolve the listed files and retry. Remaining packages are rebuilt only into targets apm.lock.yaml records; a target apm.yml declares since the last install is left to `apm install` (#4636). MCP cleanup uses recorded target ownership, attempts every owning runtime, and exits nonzero if any target fails. Safe LSP or managed hook cleanup failure (including a hook changed after the initial check or beneath a symlinked parent) preserves the conflicting configuration and exits nonzero after package removal; repair it, then run the scope-appropriate install command. Without a lockfile, no skill directories are removed. | `--dry-run`, `-g` global |
| `apm prune` | Remove unneeded recognized roots under `apm_modules/`, including manifestless `SKILL.md` packages after their lock entries disappear. No receipt or `.apm-pin` is required; personal content inside removable roots is also removed. Preserve declared direct/dev and retained transitive packages, packages a retained package still declares (even after a hand edit removed them from `apm.yml`; kept when a manifest is unreadable), bundles, and entire ancestors containing needed children. Switching `owner/repo` to `path:` dependencies on that repository needs no prune: the next install or update trims the old checkout to the requested subdirectories. GitHub owner/repository casing changes do not orphan packages or rename their directories; aliases, local paths, case-sensitive hosts, and in-repository paths retain their casing rules. Distinct repositories can share case-variant owner prefixes. Multiple case-equivalent paths for the same package exit 1 before cleanup: inspect the conflicting paths under `apm_modules/`, keep the intended package, and rerun `apm install`. Outside deployments retain ownership-based protections; stale records never authorize deleting untrusted or shared bytes. Deletion failures exit 1 with removed/failed counts; resolve the errors and rerun to converge. | `--dry-run` previews removal, retention, and ownership repair without mutation |
| `apm export-patch` | Export local edits to APM-managed files as `git apply`-ready patches against their source packages (one `patches/<pkg>@<ref>.patch` per package, locked base in the header; verbatim-deployed files only -- transformed deployments are listed as skipped with the reason). Read-only: never writes `apm.yml` or the lockfile. `apm audit` drift output and `apm install` point here when managed files carry local edits | `-o/--out DIR` output dir (default `patches`; must not point inside `apm_modules/`), `--dry-run` preview, `-v` verbose |
| `apm deps list` | List manifest- and lockfile-resolved packages; ignore parent-owned embedded manifests. Orphan status uses `apm prune`'s selection: skills bundled inside a declared package are not orphans; a real package above a declared subdirectory is still reported. Direct locked local packages use actionable `_local/<name>` keys without absolute paths; transitives are removed through their parent. Non-default hosts list the host-qualified key `apm uninstall` accepts; Source follows the declared or locked host type. | `-g` global, `--all` both scopes, `--insecure` |
| `apm deps tree` | Show the complete lockfile-resolved tree at any depth; mark repeated ancestors as circular; same-repo sibling packages are disambiguated by their installed `apm.yml` | -- |
| `apm deps why PKG` | Explain why a package is installed (walks lockfile bottom-up to direct deps; analogue of `npm why` / `yarn why`) | `-g` global, `--json` |
| `apm find <PATH>` | Trace a deployed file back to the package(s) that contributed it (inverse of install; reads `apm.lock.yaml` only) | `--source` show OCI/git/local origin, `--path` show full why-chain (same as `apm deps why`) |
| `apm view PKG [FIELD]` | View package details, git refs, or registry versions | `-g` global, `FIELD=versions`, `--registry [NAME]` forces registry path for versions |
| `apm view --file PATH` | Read-only, exact-path owners of one deployed file from deployment records: active writer, surviving owners, source/ref/commit, and current file status (`matches`, `modified`, `missing`, `unverifiable`). Exit `0` recorded, `1` unrecorded or an owner unresolved (not project-owned), `2` usage/path/lockfile error. Check before editing an installed skill; edit the owner's source. Richer than `apm find`. | `-g` user scope (`~/.apm/apm.lock.yaml`) |
| `apm outdated` | Read-only upstream check: does not modify `apm.yml`, the lockfile, `apm_modules/`, or deployed files; legacy `apm.lock` is read in place. Registry `Latest` is highest published parseable semver (existing prerelease/build ordering); `Wanted` is highest within the constraint. `Source` adds `(outside constraint)` when Latest fails it. Unknown remotes never fall back to stale cached refs. Completed checks exit `0`, including outdated/unknown; a marketplace alias rebound to another catalog shows `origin-conflict` and exits `1`. | `-g` global, `-v` verbose (matching registry versions within the constraint; lockfile-only rows list newer published versions), `-j N` parallel checks; no `--json` or package filter |
| `apm deps info PKG` | Alias for `apm view PKG` local metadata | -- |
| `apm deps clean` | Remove the project's entire `apm_modules/` directory; `apm.yml`, the lockfile, and deployed files are untouched (run `apm install` to restore) | `--dry-run`, `-y` skip confirm |
| `apm deps update [PKGS...]` | Deprecated -- use `apm update` instead; it covers every flag here, including `--legacy-skill-paths`. Update specific packages | `--verbose`, `--force`, `--target` (comma-separated), `--parallel-downloads N`, `-g/--global`, `--legacy-skill-paths` |

For conditional columns, lockfile-only rows, unknown selectors, and explicit
outside-constraint upgrades, see the
[outdated reference](https://microsoft.github.io/apm/reference/cli/outdated/#registry-reporting).
`apm update` remains constraint-respecting and writable; it does not take an
outside-constraint `Latest`.

Same-named skills keep other packages' unique assets and personal files;
overlapping managed files retain last-writer behavior in project and global
scope. Use ordinary `apm install` after source or target changes; `--frozen`
keeps recorded ownership. Overwrite warnings name the previous owner when known.
Fix unreadable sources before retrying, not by resetting the lockfile. See
[shared-skill behavior](https://microsoft.github.io/apm/reference/cli/install/#behavior).

Loose-skill updates use local receipts of successful final rendered writes, not
the checked-out lock as proof of the previous local deployment. Keep the ignored
`.apm-local/` identity and `$APM_HOME/deployment-receipts/` local; do not commit or
share them. The identity lives in the project root, or your home for `--global`;
configured external skill roots retain the same ownership and receipt checks.
On Linux and macOS a receipt survives a reboot or remount that changes only the
root's device number; a recreated root is still rejected.
Switching targets that share a skill path refreshes valid same-writer evidence
through a guarded copy, not by adopting existing files.
The same evidence guards instructions, agents, prompts, commands and rules: a
managed file that differs from the render is replaced only with a local receipt or
bytes equal to the locked render, never on a lockfile hash. Otherwise install keeps
it, names it and exits 1; move it aside and rerun `apm install`, or use `--force`.
Uninstall records actual surviving-skill rebuilds and binds those receipts only
after final lock persistence, so the surviving package can update normally.
Branch switches and cold-cache replay can update unchanged files,
but edited files and legacy files without receipts stay protected. Preserve
edits and move conflicting files aside before reinstalling. Audit and dry-run
do not create receipts; frozen install can record local writes without changing
the manifest or lockfile.
Without local receipts (a fresh clone or CI runner), a committed skill whose
whole directory matches what the install would write, and whose files are
recorded in `apm.lock.yaml`, is kept and receipts are recorded; lockfile hashes
are never the proof. When the installed commit differs from the lock (`apm
update`, or `apm install` after an `apm.yml` ref change), such a skill is also
replaced when it matches what the currently locked version renders (fetched
from its locked commit); if any of those files was edited, the command fails
before writing files or the lock.
When a missing local receipt prevents an existing skill from being overwritten,
install exits nonzero before stale cleanup and lock replacement. The refused
files and prior lock contents are preserved, including cold `--frozen` replay
with overlapping dependencies and project/user scope. This is not a full
rollback: files admitted earlier in the run may remain. Move the conflicting
file aside and retry; use `--force` only for an intentional overwrite after
preserving edits. A locally edited file with a valid receipt retains
warning-only preservation.
Creating this state requires filesystem hard-link support; unsupported or
unwritable state paths fail instead of disabling preservation.
Every file primitive APM writes (instructions, prompts, agents, commands, hooks,
skills) records such a receipt. Install, update, uninstall and prune delete a
lockfile-listed file only when a receipt or byte equality with the locked
version's render proves APM deployed its current bytes; a lockfile hash alone
never does. Files without that evidence are kept with a warning naming them,
and uninstall then stops, keeping the declaration.

For `apm install -g --mcp`, mixed target selections warn and skip
workspace-only runtimes. If no selected target supports user scope, the command
exits `2` before creating or changing the user manifest, lockfile, or runtime
configuration.

`apm install` installs native plugins intact: Claude plugins
(`.claude-plugin/plugin.json`) targeting `claude` (or `copilot` at project
scope), and Agent Plugins targeting `copilot`. It copies the approved plugin
root from `apm_modules` to `.agents/plugins/<plugin>/`, generates
`.agents/plugins/.claude-plugin/marketplace.json`, and writes only
`extraKnownMarketplaces.apm-p-<hash>` (path `./.agents/plugins`) and
`enabledPlugins["<plugin>@apm-p-<hash>"]` into `.claude/settings.json` (targets
`claude`) and `.github/copilot/settings.json` (targets `copilot`). Commit
`.agents/plugins/`, both settings files and `apm.lock.yaml`: a fresh clone
loads the plugins without APM after folder trust. With `-g`, APM uses `~/.agents/plugins/`, an absolute path, `apm-u-<hash>`,
`~/.claude/settings.json` (`CLAUDE_CONFIG_DIR`) for Claude plugins and
`~/.copilot/settings.json` (`COPILOT_HOME`) for Agent Plugins. No `claude` or
`copilot` CLI is needed. Copilot also reads `.claude/settings.json` (lower
priority), so for a plugin targeting `claude` but not `copilot` APM writes its
own `false` into `.github/copilot/settings.json`; APM owns that Copilot key for
every plugin it declares to Claude, follows `targets:`, and warns when it
overrides a value it did not write. A `--target` run that excludes a native
plugin keeps its previous lock entry and deployed root, with a note.

At project scope a plugin without `.claude-plugin/plugin.json` or with a
`skills:` selection deploys loose instead. A plugin APM cannot install intact
(unapproved executables, declared plugin `dependencies`, symlinks, `monitors/`
or `workflows/`; with `-g`, also a skill subset or no
`.claude-plugin/plugin.json`) is parked: not deployed or declared, a warning
names the package and the next step, and the install continues (exit 0). To
approve a parked plugin, run `apm approve <package>` (from `~/.apm` for `-g`;
or `--pending`). `bin/` deploys inside the plugin root under the same approval
and org `bin_deploy` policy, and needs `--trust-bin` when not interactive. MCP
and LSP servers the plugin ships are loaded by the harness from the plugin, not
written by APM for Claude or Copilot; the whole-plugin approval covers them,
and a server org MCP policy blocks, or an MCP entry APM cannot read
(`.mcpb`/`.dxt`, URL, unreadable file), parks the plugin
(`enforcement: block`). A transitive plugin with MCP servers needs a pin or
`--trust-transitive-mcp`, even with no `executables` block. Two plugins whose
names match ignoring case fail the install, naming both dependencies.
`apm.lock.yaml` records each as `native_plugin`, the only ownership record: APM removes only entries the
previous lock produced, keeps user entries and never changes an explicit
`false`. Invalid settings JSON fails the install with nothing written. See
[native plugins](https://microsoft.github.io/apm/consumer/install-plugins/#native-plugins).

`apm audit --ci` checks both the source and the deployed copy against that
fingerprint (each publisher file's path, bytes and executable bit, excluding
APM's root `apm.yml` and `.apm/`). Same-revision content
changes are refused, not adopted. Run audit from the user manifest directory with
Claude included in that manifest's targets; see the [user-plugin audit recipe](https://microsoft.github.io/apm/consumer/installing-from-marketplaces/#apm-managed-claude-user-plugins).
`apm audit --ci` checks both the `apm_modules` source and the deployed copy
against that fingerprint (paths and bytes at project scope, plus executable bits
at user scope). Same-revision content changes are refused, not adopted. For
user scope, run audit from the user manifest directory with Claude in its
targets.

A plugin whose hooks were installed loose is refused with `Cannot switch <pkg>
to a native plugin. Hook settings were preserved. Run: apm uninstall <pkg> &&
apm install <pkg>` (with `--global` at user scope). A plugin without loose
hooks switches automatically and its stale loose files are removed.

Uninstall and prune remove the plugin roots and settings keys the previous
lockfile recorded; your explicit `false`, your own entries and roots you edited
stay. If updating Claude settings fails, settings are left unchanged and the
previous lockfile is restored; fix the reported cause and rerun the same
command.
Global uninstall removes the plugin roots and `enabledPlugins` keys the previous
lock recorded, plus APM's `extraKnownMarketplaces` entry once none remain. An
edited root, an explicit `false` and your own entries are kept; APM never takes
over foreign marketplace/plugin ownership. APM's package-replacement rollback
does not cover Claude-owned metadata; failures retain ownership intent. After
repair, reconcile remaining declarations with
`apm install --global --target claude` for Claude-only installs; for mixed
targets, retain the full original `--target` list. Do not re-add removed
package arguments.
stay. If updating settings fails, settings are left unchanged and the previous
lockfile is restored; fix the reported cause and rerun the same command.
The first `apm install` that targets `copilot` and declares every plugin the
earlier `apm` marketplace (pointing at `apm_modules`) enabled retires it and its
ledger in one rollback unit; other commands keep it and warn to run `apm install`. `apm deps list` reports
`N native plugin(s) activated from .agents/plugins`.

Cursor and OpenCode user configs are `~/.cursor/mcp.json` and
`~/.config/opencode/opencode.json`. Global MCP cleanup removes only names
recorded as APM-owned for that target; personal same-name entries stay intact.
Switching a direct `--mcp` target removes the managed server from the old config.
If cleanup fails after the new config was written, retry the same command;
APM keeps managing both copies until the old entry is removed.
Unsafe user configs fail even on reinstall: fix unreadable or non-object JSON,
JSONC, or symlinks below the home directory, then rerun the original command
with `--global` and the same `--target` selection.
Project MCP configs follow the same rule: an unparsable file (JSONC, trailing
comma) or a config that is a symbolic link is left untouched and the target
fails; replace it with a regular JSON file and rerun.
For Cursor credential repair, edit `.cursor/mcp.json` at project scope or
`~/.cursor/mcp.json` with `--global`; replace only the affected values with
native `${env:NAME}` references and preserve other fields and servers.

Same-name MCP conflicts on Claude, Codex and every JSON-config target (VS
Code, Copilot CLI, Cursor, Gemini, Kiro, OpenCode, Windsurf, Antigravity)
preserve differing native entries and exit `1`; successful writes, including earlier registry writes, stay managed.
Registry conflicts do not skip nonconflicting self-defined updates. Full APM
lockfile rebuilds, including `--only apm`, retain prior MCP write receipts.
`--force` cannot take over a foreign connection; `--frozen` cannot certify a
differing one. A user entry equal to a self-defined server (Codex `enabled` aside)
needs no write: install and `--frozen` pass and leave it unowned.
Rename or remove only the conflicting native entry, then retry with the same
scope and targets. Do not delete the lockfile to bypass ownership checks.
See [MCP conflict recovery](https://microsoft.github.io/apm/consumer/install-mcp-servers/#resolve-a-connection-name-conflict)
and [cleanup receipts and partial-failure ownership](https://microsoft.github.io/apm/reference/lockfile-spec/#canonical-deployment-rows).

`apm install --dry-run` includes required indirect APM dependencies in its
counts using local source metadata and unchanged installed remote metadata.
The dependency graph pass is read-only and offline: unavailable metadata or
required fetches/lookups make the preview incomplete and suppress orphan-removal
predictions. Stale-file integration cleanup is not previewed; positional
validation may still access the network.
`--refresh`/`--update` preserve their remote-resolution intent, and `--frozen`
checks locally discoverable transitive pins. An incomplete summary reports only
the locally known count, not a complete install plan.

Project-scope positional dry-runs retain any bootstrapped manifest for inspection.
Global dry-run bootstrap leaves an absent `~/.apm` uncreated.
`apm install -g --mcp ... --dry-run` does not create a user manifest, lockfile,
or runtime configuration.

An APM older than the lockfile's `apm_version` refuses `install` (including
`--frozen`, `--dry-run` and `-g`), `update`, `uninstall`, `prune`, `lock`,
`audit` and `pack` before any change, naming both versions; read-only commands
warn. Upgrade the CLI; there is no override. A local bundle whose embedded
lockfile a newer APM wrote installs with a warning naming both versions.
`apm update`, `apm install --refresh`, and the deprecated `apm install --update` revalidate
each marketplace catalog they resolve once per run and fail without changes if
it cannot be verified; plain `apm install` keeps the cached catalog.

`apm update`, `apm install --refresh`, and the deprecated `apm install --update` stage and
validate replacement packages before publication. A failed download
or validation leaves the previous package active. Activation and its restore
retry transient Windows `WinError 5`/`32` holds with bounded backoff. A
persistent activation failure attempts restoration without changing the
lockfile. If restoration or cleanup
also fails, preserve the reported recovery paths: the prior package may be in
`.apm-resolution-staging/<session>/backups/` rather than active. Close programs
holding the affected paths and inspect/recover prior data before retrying the
same command. Overlapping parent/child recovery is deferred after an obstruction
to retain the needed backups; unrelated packages can still be restored.
Do not delete unknown, legacy, active, or recovery staging roots,
elevate APM, change ACLs, or disable security controls to bypass a Windows denial.
Cleanup of committed staging warns without rolling back the validated package.
Late Codex native reconciliation
runs after APM persistence and is not part of that rollback; see its qualified
contract below.

`apm install` preserves the invoking repository's branch and HEAD when run from
a Git hook. It rejects cross-host network rewrites for every Git host and scopes
credentials by `(host, port, org)` plus repository path for private GitHub
helper fallback; see the
[install behavior reference](https://microsoft.github.io/apm/reference/cli/install/#behavior).

For JavaScript hook bundles, `apm install --target copilot` (and the
deprecated `--target vscode` alias of `copilot`) omit the generated `package.json` sidecar from
project `.github/hooks/` and user `~/.copilot/hooks/` because Copilot
recursively treats JSON there as hook configuration. Use `.mjs` for ES
module hooks and keep runtime configuration in a non-JSON format on these
targets. Other targets keep JSON bundle assets and the Node module-type
sidecar when their runtime requires it; see
[Package authoring](package-authoring.md#hook-files).

`apm publish --package OWNER/REPO` normalizes the owner and repository to
lowercase before constructing the package-registry path.

### Root replacement lifecycle (candidate)

For the unratified development-build syntax and old-reader limits, see
[Root source replacement](dependencies.md#root-source-replacement-candidate).
No new command or flag is added: edit root `replace`, then run `apm install`.
Original identities remain selectors and ownership keys.

`apm install --dry-run` resolves locally available replacement dependencies
without deployment writes. Selected local paths stay consumer-root-relative
at every depth; missing source evidence makes the preview incomplete.

Ordinary install replays matching fork pins; `apm update` refreshes mutable fork
refs and local content but retains explicit replacement SHAs. Partial updates
refuse a changed rule table: run a full install/update to apply it.
`--frozen` compares the entire normalized table, including unused rules, and
refuses missing or changed selected-local content and missing baseline hashes
before deployment. Use normal install after reviewing an intentional change.
`apm outdated` checks selected Git sources and marks local sources not remotely
comparable. Its table shows original and selected sources; `--verbose` adds
per-dependency replacement details. `apm deps list`/`why` show selected-source provenance; tree/why retain
shared original parents. `apm audit --ci` replays selected sources read-only.
Uninstall preserves shared dependencies until their last parent is removed,
repairs incoming requests and retains dormant root rules. Missing source
evidence that prevents safe reachability verification blocks removal and names
the affected dependencies whose manifests must be restored.

### Local single-file dependencies

Local single files use `apm install ./ai/agents/xyz.agent.md` (also
`.instructions.md` and `.prompt.md`), or a matching string/`path:` dependency in
`apm.yml`. They follow the ordinary package lifecycle and are copied snapshots,
not live links. Only the selected file is included; companion assets require a
directory package. Normal reinstall accepts deliberate edits. Missing,
unreadable, unsupported, or invalid sources fail without authorizing removal of
prior owned output. Global installs require absolute or `~/...` references;
file symlinks and arbitrary Markdown are unsupported.

### Install validation chain (virtual subdirectory packages)

Marketplace installation supports versioned directories produced by `apm pack`,
such as `plugins/my-plugin-1.2.3`, including positional `--dry-run` previews.
Directory content and path validation still apply. Git directory probes reject
files, symbolic links, and submodules.

`apm install` validates subdirectory packages (`owner/repo/path#ref`) before writing to `apm.yml` using the same credential chain as the actual install. Git-source semver ranges (for example, `owner/repo/path#^1.2.0`) defer raw-ref validation to semver tag resolution; registry-routed dependencies retain registry version validation. See [Authentication > Install validation chain](../authentication/) for the full probe sequence and troubleshooting.

When a default registry is configured, plain shorthand deps (`owner/repo#<ref>`) bypass the GitHub probe. `apm install` requires a version selector before writing to `apm.yml`; deps with no `#<ref>` at all are rejected. Semver selectors (`1.0.0`, `^1.2.3`) use range matching; non-semver selectors (`stable`, `v1.4.2`, any opaque label) are matched exactly against the registry's published versions.

### MCP-only lock and frozen behavior

A normal project install creates or updates `apm.lock.yaml` when the manifest declares only MCP dependencies. The lock records resolved MCP configs and targets; repeating the same install leaves lock and target config bytes unchanged. A legacy `apm.lock` is migrated before this state is written.

`apm install --frozen` validates package pins and available MCP declarations before the pipeline. A missing or stale MCP-only lock exits nonzero without writing; run normal `apm install` to repair it. `--only=mcp` follows the same guard. Add-style `--mcp NAME` is incompatible with `--frozen` because it mutates `apm.yml`. Frozen install admits dependency MCP exactly as a normal install does: approved and gate-parked servers pass, and a server the lock admits without approval in `apm.yml` fails as `not approved in apm.yml` (or `the executables gate now parks it (denied)` after `apm deny`).

A fresh checkout can restore supported locked remote whole plugins directly with `--frozen`; no ordinary install is required first. Unavailable package MCP evidence is deferred only when its recorded origin is unambiguous, then the actual hydrated declarations are checked before deployment or native MCP writes. Real changed/removed declarations, present/shared-owner mismatches, invalid package shapes, missing local sources and insufficient pins still fail. Deferred MCP failures preserve prior native MCP configuration and user-owned settings, but downloads/cache fills may have occurred; this is not general rollback of lifecycle scripts. Root precedence, transitive trust and native-plugin MCP ownership are unchanged. Frozen replay uses pinned sources, not a refreshed discovery catalog; uncached sources still require access and credentials.

`--only=mcp` cannot defer unavailable package evidence because it skips package hydration. Restore sources with the full `apm install --frozen` command before a services-only replay.

`apm install --frozen` never writes `apm.lock.yaml`. Before lifecycle or deployment it verifies each selected direct or transitive local dependency against its recorded `content_hash`. The hash covers the validated local-copy image, including dereferenced in-package symlinks, before integration transforms it. Changed, added, removed, or retargeted content fails without changing installed files or the lock. Unused local lock entries are ignored by this content check; unavailable local sources can still fail MCP preflight. Missing entries or legacy locks without a hash require a normal `apm install` to establish a baseline. Deployment still runs, and two checks come after it. Frozen mode exits `1` and names the paths when the install deploys files the committed lockfile does not record; it first removes the unrecorded files it wrote, so the project matches the lockfile. It also exits `1` when a deployed file's bytes differ from its locked hash (req-lk-017; the same check as `apm audit --ci`), naming each path with its expected and observed hash. That covers a kept local edit and an APM upgrade that renders a locked file differently. Those drifted files are already on disk -- only the lockfile write is withheld. Rerun the same command without `--frozen` (keeping `--root DIR` or `--global`), then commit the deployed files and the updated lockfile; see the [install reference](https://microsoft.github.io/apm/reference/cli/install/#behavior) for recovery details.

The local-source check is APM-specific frozen-install behavior, not a new
lockfile schema requirement. Legacy bare SHA-256 digests are accepted without
rewriting the lock. Normal non-frozen installs can establish missing baselines.

Narrowing a dependency's `targets:` reconciles its removed targets within the
current install scope, even if the consumer still enables them. Removed files
lose their file/hash/ledger claims; edited files and targets skipped by
`--target` retain protective records.

Normal `apm install` repairs obsolete dependency claims for already-absent files
when complete source discovery and successful skill deployment prove those
files are no longer shipped within a current source-backed skill. It synchronizes
file lists, hashes and deployment records, including historical `legacy` records
and locks without a ledger. No lock reset or uninstall/reinstall is needed.
Other owners, unprocessed targets and incomplete deployments remain protected.
Missing bytes, a saved hash or a directory prefix alone are not proof. Unresolved
claims remain visible in read-only audit; still-present content with unclear
ownership is not deleted or adopted. `--frozen` cannot perform the repair or
accept changed local sources; unchanged frozen replay works after ordinary repair.
Manifest-derived dependency versions come from the acquired package snapshot,
not an older descriptor in `apm_modules/`. The first ordinary install after a
branch switch records the current version. These values are inventory metadata,
not frozen replay pins. Registry and Git-semver
resolver-selected versions and legacy synthesized plugin versions retain their
own meaning; absent optional versions do not become errors.
Frozen native MCP/LSP integration also preserves exact manifest and lockfile
bytes. It refuses a selected target/server without recorded ownership before
writing or removing either service's native configs. For example, adding
`intellij` to a Copilot/Claude baseline requires an ordinary install with the
intended `--target` first; frozen mode does not create an unrecorded personal
deployment. Retain `--only`, `--root DIR` or `--global` during recovery.
Reordered, repeated and narrower selections work. Downloads may precede the
check; package deployment and lifecycle scripts are not generally rolled back.
Late MCP checks use the complete resolved package graph, not just the packages
selected for installation.

`--only mcp` does not hydrate package sources, even without `--frozen`.
Restore the package sources and declarations matching the lock using
`apm install` without `--only mcp` in the same deployment store (retain `--root DIR`
or `--global`, if used) before retrying.

Claude MCP rewrites remove the previous transport's fields while preserving
unmanaged keys such as OAuth blocks. This also repairs older mixed entries
when they are written. An unchanged self-defined declaration with matching
lock state may skip the write, so repeating that install is not a migration.

Codex keeps APM registry identity in TOML comments instead of an unsupported
native `id` setting. After upgrading, `apm install --only mcp --target codex`
repairs entries matching a trustworthy saved configuration, including historical
empty `id = ""` self-defined entries; add `--global` for user scope. A recorded
owner name alone does not verify nonempty IDs, old header spelling, or native
edits. Unverifiable entries stay untouched; unresolved self-defined conflicts
return nonzero. For an entry you have verified, back up the Codex config and remove only its
unsupported `id = ...` setting. Keep comments such as
`# apm-registry-id: "registry-uuid"` for duplicate detection. Inline containers
being written may expand while preserving values and comments.
When replacing a declaration with `--mcp NAME`, `--force` permits changing
`apm.yml`, not taking over a conflicting native connection.

### Registry MCP runtime variables

For registry MCP runtime variables, `apm install` prompts once for a required
non-secret default and accepts an override; secret defaults remain hidden.
Non-secret values resolve every matching `{variable}` launcher reference.
A missing required value declines that target configuration.

### Target resolution chain

`apm install` resolves harness targets in strict priority order:

1. `--target` flag (highest; CSV form: `--target claude,cursor`).
2. `apm.yml` `targets:` list (the singular `target:` key is deprecated: it still works but warns once per run on stderr, naming the exact `targets:` replacement; `vscode` is a deprecated alias of `copilot` and warns too).
3. `apm config set target <value>` default.
4. Auto-detect file-primitive targets from project signals (`.github/copilot-instructions.md` or any of `.github/instructions/`, `.github/agents/`, `.github/prompts/`, `.github/hooks/` -> copilot, `.claude/` or `CLAUDE.md` -> claude, `.grok/` -> grok-build, `.cursor/` or legacy `.cursorrules` -> cursor, `.codex/` -> codex, `.gemini/` or `GEMINI.md` -> gemini, `.opencode/` -> opencode, `.windsurf/` -> windsurf, `.kiro/` -> kiro).

MCP runtime discovery separately recognizes the user-scope JetBrains Copilot
config directory (`github-copilot/intellij/`). That machine-global signal can
select IntelliJ for MCP configuration in every project, but it never
auto-selects a file-primitive target.

For MCP, explicit `--runtime` (legacy) or `--target` selection wins over
canonical manifest targets, then the saved `apm config set target ...`
default. Auto-detection runs only when neither the manifest nor saved config
restricts targets, and `--exclude` narrows the selected set. Declare `targets:`
when committed lockfile ownership must stay deterministic across developer and
CI machines. Omitting targets deliberately allows `mcp_target_servers` and
deployment runtime rows to follow local harness availability.
JetBrains Copilot MCP writes use
`%LOCALAPPDATA%\github-copilot\intellij\mcp.json` on Windows and
`$XDG_CONFIG_HOME/github-copilot/intellij/mcp.json` on macOS/Linux
(`XDG_CONFIG_HOME` defaults to `~/.config`). Reinstalling an older APM-managed
entry migrates only provenance-owned servers from the obsolete data path.

For user-scope Claude MCP installs, an absolute `CLAUDE_CONFIG_DIR` selects the
profile's `.claude.json` for both installation and stale-server cleanup.
Removing managed servers from the selected profile preserves other profiles and
user-owned entries. Cleanup refuses symlinked config files or profile directories;
replace the link with a regular file or directory before retrying.
`apm uninstall -g <package>` removes files and lock rows only under the selected
profile; a package another profile still uses stays in `~/.apm/apm.yml`. Only
the runtime whose profiles share the package loses files; the other runtime's
default root and non-profile targets keep theirs.

Harness home variables (`COPILOT_HOME`, `CODEX_HOME`, `CLAUDE_CONFIG_DIR`,
`HERMES_HOME`) share one rule at user scope: unset or blank uses the default
(`~/.copilot`, `~/.codex`, `~/.claude`, `~/.hermes`), an absolute path selects
that profile, and a relative path fails with an error naming the variable.
With `COPILOT_HOME` set, `apm install -g --target copilot` writes instructions,
agents, hooks, `mcp-config.json`, `lsp-config.json` and plugin settings under
it (skills too with `--legacy-skill-paths`; otherwise `~/.agents/skills/`).
Files already under `~/.copilot` stay as another profile's files. The
`copilot-app` and `copilot-cowork` targets ignore `COPILOT_HOME`.

`apm install` prints a one-line provenance summary before any mutation:

```
[i] Targets: claude, copilot  (source: auto-detect from CLAUDE.md, .github/copilot-instructions.md)
```

Add `--verbose` to also print a `[>] Scanned: ...` line listing every signal probed.

If no `--target`, no `targets:` in `apm.yml`, and no harness signal is present, `apm install` exits 2 with a teaching message instead of silently defaulting to copilot. Run `apm targets` to inspect what APM detects in the current directory; use it for discovery, scripting (`--json`), and debugging unexpected detection.

Package deployment and MCP/LSP reconciliation consume that one decision.
If required MCP/LSP work cannot resolve a target, APM exits non-zero before
writing the direct MCP manifest entry or deploying packages. Native service
config write failures also exit non-zero with an actionable path/permissions
diagnostic.

For project LSP installs, `apm install --target claude` writes the
`lspServers` section in
`.claude/skills/apm-lsp/.claude-plugin/plugin.json`, which Claude Code
discovers as a skills-directory plugin. Global Claude LSP installs use the
same personal-scope plugin shape at
`$CLAUDE_CONFIG_DIR/skills/apm-lsp/.claude-plugin/plugin.json` (default
`~/.claude/...`); global Copilot LSP installs write
`$COPILOT_HOME/lsp-config.json` (default `~/.copilot/lsp-config.json`).
`--target opencode` writes the `lsp` key of `opencode.json` (global:
`~/.config/opencode/opencode.json`); creating that key also enables
OpenCode's built-in language servers. Declared LSP servers with no
LSP-capable target (for example `--target cursor`) fail before any target
write.

For MCP, `--target copilot` writes the workspace-root `.mcp.json`
(`mcpServers`, read by Copilot CLI and VS Code's Copilot) at project scope and
`$COPILOT_HOME/mcp-config.json` with `--global` (or
`~/.copilot/mcp-config.json` when `COPILOT_HOME` is unset or blank; relative
values are rejected). `--target vscode` is a deprecated alias that writes the
same `.mcp.json`; APM no longer writes `.vscode/mcp.json` or `.github/mcp.json`.
`.mcp.json` is shared with Claude Code: both targets write one identical entry
per server, never overwrite user-authored servers, and each record ownership in
`apm.lock.yaml`. The first install of a project locked by APM 0.36 or earlier
(also `--frozen`) moves the entries APM wrote to `.github/mcp.json` or
`.vscode/mcp.json` into `.mcp.json`; user-authored or edited entries stay and
APM warns (`Kept MCP server '<name>' ...`). `.vscode/mcp.json` is deleted only if APM
created it and nothing but empty scaffolding remains; `.github/mcp.json` is only emptied,
never deleted (Copilot CLI reads it, and it is a `copilot` detection signal).

`apm compile` continues to use legacy folder auto-detection (`.github/` alone selects `copilot`; unsignalled projects fall back to `minimal`) -- bringing it onto the strict resolution chain is tracked as a follow-up.

Agent Plugins v1 packages register natively only with the effective `copilot`
target today. If target exclusion leaves a non-dry-run install with no deployed package,
`apm install` exits `1` and prints a recovery command such as
`apm install kunchenguid/lavish-axi/skills/lavish#main --target codex`; mixed
installs that deploy at least one other package still exit `0`. For this target
exclusion, `--dry-run` succeeds as a preview without the per-package recovery
diagnostic; it does not certify real install success.

Compatibility-only `.codex-plugin/plugin.json` bundles use
`apm install -g <package> --target codex` with Codex CLI 0.163.0 or later on
`PATH`; native qualification was measured on macOS arm64 only.
Project scope, older or unrecognized versions, skill subsets, and unsupported native
shapes fail explicitly. Approve them with `apm approve <package>`, which records one `plugin: sha256:<64 hex>` pin over the whole bundle (per-file hook pins never admit a live-loaded plugin);
new native registrations remain disabled. Reinstall/update and
`apm install -g --frozen --target codex` preserve activation and MCP policy,
repair owned missing/corrupt copies, and never run MCP/plugin code. Healthy
reinstalls retain their transport generation. `apm uninstall -g <package>`
refuses foreign or customized native settings before releasing APM ownership;
retire those entries using Codex and retry. No loose registration or
cross-target translation is produced. See the
[qualified contract](https://microsoft.github.io/apm/consumer/installing-from-marketplaces/#codex-compatibility-bundles-through-apm)
for late-failure retry and ownership-journal recovery.
Migration names the dependency and retained paths and refuses native registration
while cleanup retains edited or undeletable loose files. Preserve or relocate your edits, resolve the cleanup
conflict, then retry; APM does not register duplicate native and loose copies.
Same-pin native cache drift fails without replacing the recorded source identity.
From the APM user directory, `apm audit --ci --format json` reports this as a failed
drift check without starting Codex or changing native state.

## Cache management

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm cache info` | Show the cache root and counts of Git repositories, Git checkouts, and HTTP entries, plus total size | -- |
| `apm cache clean` | Remove every entry from the git and HTTP caches. If a deletion fails, it still removes every other entry, reports the affected paths, and exits nonzero; successful removals are not rolled back. Close locking processes or fix permissions, then retry. | `--force`/`-f`, `--yes`/`-y` skip the confirmation prompt only -- they do not suppress deletion failures or make the command succeed |
| `apm cache prune` | Remove Git checkout SHA groups not accessed within `--days`; full and sparse variants share recency and are evicted together, and `0` makes every group eligible. Partial failures exit `1`; fix permissions or release locks and rerun | `--days N` (default `30`; negative values are rejected before the cache is touched) |

## Compilation

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm compile` | Compile agent context; refuses (exit 1, nothing written) while org policy no longer allows an installed package (`enforcement: block`; warns under `warn`); catalog root context files such as `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md` are skipped when they lack an APM generated marker; after a successful write, reconcile deployed artifacts, lockfile ownership, and merge-hook config/sidecar entries when the declared target set contracts; a non-dry-run compile refuses a lockfile written by a newer APM before changing any file; warns when a deployed file links to a missing `apm_modules/` target (run `apm install`) | `-o` output, `-t` target (comma-separated; resolution chain `--target` > apm.yml `targets:` > auto-detect), `--all` compile for every canonical target (preferred over deprecated `--target all`), `-g`/`--global` (read global instructions from `~/.apm/apm_modules/`, write user-scope root files; Codex writes `$CODEX_HOME/AGENTS.md` when `CODEX_HOME` is non-blank (must be absolute), else `~/.codex/AGENTS.md`; OpenCode also retains its `applyTo` sections in `~/.config/opencode/AGENTS.md`; cannot combine with project-output flags such as `--target`, `--all`, `--watch`, `--root`, or `--output`; critical hidden-character findings stop the write and exit 1), `--chatmode`, `--dry-run`, `--no-links`, `--watch`, `--validate`, `--single-agents`, `-v` verbose, `--local-only`, `--clean`, `--with-constitution/--no-constitution`, `--force-instructions` / `--no-dedup` (opt out of Claude/Copilot deduplication), `--root DIR` redirect generated artifacts under DIR while sources resolve from `$PWD` (mirrors `pip install --target`; not valid with `--watch`) |

`apm install` deploys individual primitives but does not generate aggregate
root context files. Run `apm compile` explicitly for `AGENTS.md`, `CLAUDE.md`,
or `GEMINI.md`; `apm run` separately compiles referenced prompt files at
execution time.

For OpenCode user scope, first create or open an OpenCode config directory.
Then run `apm install -g --target opencode` to deploy skills to
`~/.config/opencode/skills/`, followed by `apm compile -g` to update
`~/.config/opencode/AGENTS.md` with scoped instruction sections.

`apm compile -g` rejects `--target`, so `target:` or `targets:` in
`~/.apm/apm.yml` selects
which user-scope root context files it writes. Without that field it writes
every applicable user-scope root context file. The manifest must be a regular,
non-symlink file. Malformed or unreadable YAML, a non-mapping document, or an
invalid target declaration stops compilation before any target output is
written; fix the reported manifest problem and rerun the command.

An explicit `apm install -g --target` does not replace the manifest declaration.
Update `~/.apm/apm.yml` before compiling when the install flag selected a
different target set.

Global Claude compilation omits only instructions whose expected native user
rules match the install renderer's output. Missing, unrelated, changed, or
unsafe rules retain the compiled fallback; project rules and other targets
are unaffected. This also supports external absolute `CLAUDE_CONFIG_DIR`.
An existing redundant generated user `CLAUDE.md` requires explicit cleanup:
preview with `apm compile -g --clean --dry-run`, then run
`apm compile -g --clean`. Cleanup requires unchanged generated content matching
the current instructions and complete native coverage. Hand-authored, edited,
unverifiable, and symlinked roots are retained. Partial coverage preserves
fallback output. Dry-run never mutates files.
Claude's complete instruction selection passes the compiled-output security
check before suppression or cleanup, including dry-run; critical hidden
characters fail compilation without changing its root.

After a project install stages dependency instructions for Claude, Gemini,
Codex, OpenCode, or explicit-only Hermes, `apm install` prints an `[i]` hint
naming `apm compile` and the root context files it will update (for Claude:
`Run 'apm compile' to update AGENTS.md / CLAUDE.md.`). For Claude the hint also
fires when install routed a local `.apm/` instruction to `AGENTS.md` that no
compiled `AGENTS.md` carries yet; a no-op reinstall after compile stays quiet.

`apm compile --watch` live-reloads `apm.yml`: editing `target:` / `targets:` mid-session takes effect on the next file event without restarting the watcher. The CLI `--target` flag, when passed to `apm compile --watch`, still outranks `apm.yml`. Re-resolution is gated on the changed file's basename being `apm.yml`, so `.instructions.md` edits do not pay an extra resolver round-trip and a stray `backup_apm.yml` cannot trigger a reload. When root files are retained, each recompile reports both the retained count and any other outputs generated (or planned in dry-run mode). `--clean` is ignored in watch mode and the watcher prints an explicit `[!]` warning at startup (`--clean is ignored in watch mode; run 'apm compile --clean' separately to remove orphaned outputs.`); run `apm compile --clean` separately between watch sessions to remove orphans.

`--single-agents` keeps target-specific root content separate when several targets are requested; for example, Claude's root summary is never appended to `AGENTS.md`.

**Claude routing.** At project scope, `apm install` writes only path-scoped instructions (file-type globs such as `**/*.py`, wildcard directories such as `src/**/*.py`, comma lists) to `.claude/rules/<name>.md`. Always-on (`applyTo: "**"` or none) and single literal directory instructions (`src/test/**`, `docs/**/*`) go to `AGENTS.md`: `apm compile --target claude` writes always-on ones to the root `AGENTS.md` and each directory one to exactly `<dir>/AGENTS.md` (never hoisted; multi-target compiles keep the shared `AGENTS.md` placement), plus a `CLAUDE.md` beside each that imports `@AGENTS.md`. A directory instruction stays in `.claude/rules/` when its directory does not exist or `compilation.strategy` is `single-file`; `--single-agents` leaves directory instructions out of the root file with a warning (Claude Code reads `AGENTS.md` only through that import while a `CLAUDE.md` exists). `CLAUDE.md` holds no instructions; the root one keeps dependency `@import` paths, and embeds the constitution only when APM did not write the root `AGENTS.md`. A Claude-only compile omits instructions already in `.claude/rules/` from `AGENTS.md`; multi-target compiles keep every instruction in the shared `AGENTS.md`. A hand-authored root `CLAUDE.md` without `@AGENTS.md`, or a hand-authored root `AGENTS.md` without `compilation.agents_md.mode: managed_section`, keeps every instruction in `.claude/rules/` and `apm install` warns once with the fix (add `@AGENTS.md` to `CLAUDE.md`, or set `managed_section` for `AGENTS.md`), then rerun `apm install` and `apm compile`. A hand-authored `CLAUDE.md` that already imports `@AGENTS.md` is left untouched. User scope (`-g`) keeps every instruction in `~/.claude/rules/`. A nested `AGENTS.md` loads only when Claude opens a file in that folder.

**Deduplication override.** Pass `--force-instructions` (alias: `--no-dedup`) to put every instruction in `AGENTS.md`, regardless of `.claude/rules/` contents.

**Dependency memory.** Claude imports package-root `CLAUDE.md` files using
manifest and lockfile materialization paths at any depth, including ADO,
nested GitLab repositories, virtual subdirectories, and transitive packages.
It does not scan nested documentation. Without metadata, only legacy
`owner/package` roots are discovered; run `apm install` for deeper layouts.
With `--root`, source `apm_modules/` takes precedence; if absent, the output
root's modules and lockfile are used. Imports resolve from the generated file;
source and output must be on the same filesystem drive.
For scoped frozen replay and recovery, see the
[install reference](https://microsoft.github.io/apm/reference/cli/install/#deploy-location).

**Stale `CLAUDE.md` removal (`--clean`).** When `.claude/rules/` holds every instruction (so compile writes no `AGENTS.md`) and an APM-generated `CLAUDE.md` (identified by the `<!-- Generated by APM CLI -->` marker) is present, `apm compile --target claude --clean` removes that stale file if no constitution or dependency import keeps `CLAUDE.md` active; otherwise it regenerates the file. Hand-authored `CLAUDE.md` files (no marker) are never deleted; a warning is emitted instead so you know the duplicate context remains.

**Dry-run preview.** Use `--dry-run` with `--clean` to preview what would be removed without touching any files. If a hand-authored `CLAUDE.md` would block deletion, the preview surfaces that outcome as well.

**Context file protection.** Project compiles never overwrite an `AGENTS.md`
or `CLAUDE.md` that lacks the APM-generated marker, at the root or in a
distributed subdirectory, including destinations selected with `--root`.
Compile skips the hand-authored file and prints a
warning. Delete or rename it before compiling if generated output should
replace it. In `managed_section` mode (AGENTS.md only), compile updates only
the explicit APM-owned block. See the
[manifest schema](https://microsoft.github.io/apm/reference/manifest-schema/#62-compilationagents_md).

**Nested Git boundaries.** Compile does not discover, write, or clean content
inside nested Git repositories or linked worktrees below its root. This also
applies to `includes: auto`. Run `apm compile` from the nested checkout when you
want to compile it.

When `apm install --target copilot` has already deployed no-`applyTo` instructions under `.github/instructions/`, `apm compile --target copilot` omits matching global instruction bodies from `.github/copilot-instructions.md` so Copilot does not load them twice. It also omits `AGENTS.md` when its only content would be the duplicated instructions section; `AGENTS.md` is still generated when it carries non-instruction content such as a constitution. Pass `--force-instructions` (alias: `--no-dedup`) to intentionally include the duplicated content.

When no global instruction remains, Copilot compile removes a stale root file
only if its APM header marker and Build ID verify unchanged content. Hand-authored,
edited, legacy files without a valid Build ID, and symbolic links are retained.
Preview cleanup with `apm compile --target copilot --dry-run`.

**Distributed managed sections.** With `compilation.agents_md.mode:
managed_section`, existing distributed `AGENTS.md` files retain content
outside their APM markers. New placements are created with a managed block. Run
`apm compile --dry-run` to inspect the same eligible placement set a real
compile uses; nested Git repositories and linked worktrees, including their
descendants, are skipped.

## Scripts

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm run SCRIPT` | Execute a named script (experimental; the interface may change, so do not depend on it in automation) | `-p name=value` (repeatable) |
| `apm preview SCRIPT` | Preview script without running | `-p name=value` |
| `apm list` | List available scripts | -- |

## Security and audit

Audit replay and coverage use current manifest targets, otherwise concrete
recorded deployment targets, before directory discovery. Unrelated workflow
files cannot add Copilot output to a recorded Grok Cloud installation.
Legacy lockfiles without recorded `deployments` retain directory discovery,
even when compatibility paths imply a target.
The `apm config set target` default applies to install/update, not retroactive
audit intent. A disabled selected experimental target exits `3` with an enable hint
on stderr, not a JSON audit report. Missing or modified managed files retain
the existing drift and CI-check rules; bare audit drift is advisory unless
configured otherwise. Retained historical paths remain eligible for CI
presence and recorded-hash checks. Audit does not repair them.
Run `apm audit --ci` from the project root: APM does not search parent
directories, and without an `apm.yml` in the current directory the gate fails
`manifest-missing` (exit 1) instead of passing an empty audit.

For shared hook targets, drift compares only APM-owned hook entries. Ownership
sidecars are compared as JSON: object-key order and formatting do not cause
drift, but changed fields, ownership markers, or list order do. Malformed JSON,
duplicate keys, and non-JSON constants (`NaN`, `Infinity`, `-Infinity`) report
`modified`. User-owned hooks do not cause drift.

Automatic `apm audit` discovers registry-recognized primitive files, including
untracked native hook definitions in shared settings. It checks prompt
documents and decoded native prompt fields, not command strings, executables,
unrelated settings, transcripts or caches. Command-only hooks remain visible
and non-failing; hooks are never executed. Unreadable or unsupported recognized
content fails with incomplete coverage, including with `--no-drift`/`--no-policy`.
Non-CI JSON exposes `coverage.complete` and `coverage.primitives`; CI includes
inventory in `content-integrity.primitive_coverage`, separate from violation
`details`; SARIF uses invocation `primitiveCoverage` properties, including on
passing checks. Structured findings identify pointers and decoded offsets,
not physical file line/column positions. CI `content_findings` retains the exact
offending file and pointer; SARIF locates that artifact, not the lockfile.
A failing CI JSON check may carry `remediation` (`id`, `command`, `fixes`, optional
`before`/`then`) and `remediations` (all of them): run `before`, then `command`.
Copilot App's SQLite workflow prompts
are not covered by filesystem discovery. Tracking is file-level,
not entry ownership or hash verification. Automatic `--strip` refuses
structured/shared, user-scope or external-root findings and incomplete coverage without
rewriting files. Explicit `--file` remains user-directed.

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm audit [PKG]` | Scan installed primitives for hidden Unicode, drift, and lockfile/policy violations; warns when a deployed file links to a missing `apm_modules/` target (run `apm install`) | `--file PATH`, `--strip`, `--dry-run`, `-v`, `-f [text\|json\|sarif\|markdown]`, `-o PATH`, `--ci`, `--policy SOURCE`, `--no-policy`, `--no-cache`, `--no-fail-fast`, `--no-drift`, `--external NAME` (experimental; ingest a third-party SARIF scanner, e.g. `skillspector`), `--external-sarif PATH`, `--external-llm/--no-external-llm`, `--external-args TEXT` |
| `apm policy status` | Show policy discovery outcome, source, enforcement, cache age, `extends` chain, and rule counts. Exits `0` by default; with `--check`, exits `1` when no usable policy is found | `--policy-source SOURCE`, `--no-cache`, `--json` / `-o [table\|json]`, `--check`, `-g` (user scope: no git remote, source `none`) |
| `apm policy explain PKG` | Explain the effective executable-trust decision (allowed or not, deciding layer, shadowed layers) for an installed package | `-g/--global` for a package `apm install -g` installed |

`apm audit` runs **drift detection by default** (issue #1071). It replays `apm install` cache-only into a temporary scratch tree and diffs the result against your working tree. It catches four failure modes: (1) `.apm/` source added without re-running `apm install`, (2) hand-edits to deployed files that diverge from canonical source, (3) orphan files left after their source was removed, and (4) `unrecorded` files that install deploys but no lockfile entry claims. The scan is read-only -- never writes to your project, lockfile, or live `apm_modules/`. Build IDs, CRLF line endings, and BOMs are normalized away so they cannot trigger false positives.

Native Copilot Agent Plugins replay through the canonical package loader, with or without a plugin `apm.yml`. A clean install or reinstall can audit successfully. Set the consumer's `targets: [copilot]` in `apm.yml`; audit has no `--target` flag. Invalid native manifests fail replay instead of entering legacy conversion. Admitted local native cache units are compared with a scratch copy of their original source using install's validated copying; modified or missing cached skills fail audit. Remote native units use the recorded package content hash, including in cold-cache CI. Run `apm install` to restore drift or establish a missing remote hash. The deployed copy under `.agents/plugins/` is checked against the lock's `native_plugin` fingerprint; settings files are not covered by this content comparison.

Bare `apm audit` uses the warmed local cache and skips drift with an informational message when the cache is absent; run `apm install` before relying on non-CI drift. `apm audit --ci` is stricter: in a project with `apm.yml` and `apm.lock.yaml` but no `apm_modules/`, it prepares a lock-pinned scratch install for `skill-subset-consistency`, `config-consistency`, and drift without touching the checkout. No checkout install is required first, but local dependency sources must be available at their declared paths. Windows long and native 8.3 spellings of the same project retain project scope; they do not require a global install. Auditing the actual user metadata workspace (`~/.apm`) still refuses missing modules when its lockfile records packages or local bundle staging and requires `apm install --global`; a user lockfile with only MCP/LSP servers or `~/.apm` content needs no `apm_modules/`. Missing gitignored deployed outputs do not fail `deployed-files-present`, so repos that gitignore them can use audit-only CI too. Without committed deployed bytes, that pattern lacks deployed-byte integrity and drift coverage; use full-install CI when outputs need to be materialized.

`config-consistency` respects dependency MCP deliberately omitted by `apm install --only apm`, the secure default refusal of transitive self-defined MCP (also when `apm install` or `apm update` without `--trust-transitive-mcp` removes a previously trusted one), or the executables gate while the declaring package is not approved. Install records evidence bound to that dependency and authored declaration, before plugin-root expansion; empty MCP fields alone are not evidence. Direct MCP admission supersedes old omission evidence for that server. Root and previously admitted MCP still participate in drift checks, and a later skills-only install preserves their baseline. Unknown, damaged or stale evidence requires repeating the intended selection: use `apm install --only apm`, or `apm install --global --target agent-skills --only apm` for global skills-only. Do not enable MCP or add `--trust-transitive-mcp` merely to pass audit. `apm install --frozen` applies the same executables gate and requires the recorded evidence to match what install would record now. The MCP consistency check does not rewrite the lockfile or runtime configuration. See the [config-consistency reference](https://microsoft.github.io/apm/reference/baseline-checks/#config-consistency) for the full contract.
`config-consistency` respects dependency MCP deliberately omitted by `apm install --only apm`, the secure default refusal of unapproved transitive MCP (also when `apm install` or `apm update` without `--trust-transitive-mcp` removes a previously trusted one), or the executables gate while the declaring package is not approved, is denied, or no longer matches its pin (`definition-changed`). The lockfile is evidence, never consent: audit re-evaluates admission from `apm.yml` and org policy (never the personal `--user` store), so a server the lock admitted but `apm.yml` does not approve (a one-off `--trust-transitive-mcp` lock or a hand edit) fails with `admitted in apm.lock.yaml but not approved in apm.yml (<reason>); run 'apm approve <pkg>' and re-run 'apm install'`. A passing audit names servers parked as `definition-changed`. Install records evidence bound to that dependency and authored declaration, before plugin-root expansion; empty MCP fields alone are not evidence. Direct MCP admission supersedes old omission evidence for that server. Root and previously admitted MCP still participate in drift checks, and a later skills-only install preserves their baseline. Unknown, damaged or stale evidence requires repeating the intended selection: use `apm install --only apm`, or `apm install --global --target agent-skills --only apm` for global skills-only. Do not enable MCP or add `--trust-transitive-mcp` merely to pass audit. The MCP consistency check does not rewrite the lockfile or runtime configuration. See the [config-consistency reference](https://microsoft.github.io/apm/reference/baseline-checks/#config-consistency) for the full contract.

`apm install --only apm --frozen` also preserves the admitted MCP baseline and
exact lockfile and native-configuration bytes without selecting MCP integration.
Existing frozen source and consent checks still apply: a baseline that already
admitted transitive MCP requires an approval in `apm.yml` or the same explicit `--trust-transitive-mcp`.
Omission evidence does not grant consent.

For project-local content, replay uses the same package interpretation as install.
A sibling `skills/<name>/SKILL.md` alongside `.apm/` is not promoted to a local
skill or reported as `unintegrated`. Other supported source layouts and actual
dependency skill bundles keep their existing discovery behavior.

Use `--no-drift` to opt out with reduced coverage (e.g. fast inner loops); the flag is mutually exclusive with `--strip`/`--file`. Drift is advisory in bare audit by default unless policy enables `security.audit.fail_on_drift`; `--ci` always gates on drift. Invalid selections, manifest/lock mismatches, and deployed-file integrity failures still fail `apm audit --ci`. Remediate `unrecorded` with `apm install`, then commit the regenerated `apm.lock.yaml`.

A stale canonical deployment owner is different: `deployment-ledger-owners` is a hard integrity failure in both modes, exits 1, names the owner and path in text/JSON/SARIF, and blocks `--strip`. Remediate it with `apm prune`, then rerun `apm audit`. Drift output is integrated into JSON (top-level `drift` key) and SARIF (rule IDs `apm/drift/<kind>` where kind is `modified`/`unintegrated`/`orphaned`/`unrecorded`).

With `--ci --policy <path|url|owner/repo>`, a policy that cannot be loaded (missing file, unreachable, malformed, garbage, not found) exits 1 with `[x] Policy '<source>' could not be loaded: <cause>`; `policy.fetch_failure_default` only governs auto-discovery and `--policy org`. CI JSON (top-level `policy`) and SARIF (`invocations[0].properties.policy`) report `enforced`, `source`, and `enforcement`.


**External scanners (experimental, behind `apm experimental enable external-scanners`).** `--external NAME` runs a third-party SARIF scanner (e.g. `skillspector`) and merges its findings. `--external-llm/--no-external-llm` toggles LLM-powered analysis (default off; sends scanned content to a third-party API, so APM prints a `[!]` egress banner and forwards `OPENAI_API_KEY`/`NVIDIA_INFERENCE_KEY` only when on). `--external-args TEXT` is a single shlex-split string of extra scanner flags, validated against a per-adapter allowlist -- non-allowlisted flags, secret-looking flags, and out-of-cwd paths are rejected fail-closed. SkillSpector's `--baseline` must carry exactly one value, in either spelling, and that value is resolved inside the working directory even when it has no path separator. `--external-llm`/`--external-args` without `--external` is a usage error (exit 2). Scanner configuration or infrastructure errors (feature disabled, scanner not found, malformed SARIF) exit **3**. Persist defaults with `apm config set external.<name>.llm true` and `apm config set external.<name>.args -- "--model gpt-4o"`. Precedence: CLI > config > policy floor.

## Lifecycle scripts

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm lifecycle` | List all discovered lifecycle scripts across policy, user, and project sources | -- |
| `apm lifecycle init` | Inject a starter `lifecycle:` block into `apm.yml` | `--force` (overwrite existing block) |
| `apm lifecycle test EVENT` | Fire a synthetic event through all discovered scripts (dry-run) | `--verbose`, `--execute` (actually run scripts, through the same trust and kill-switch gate as install) |
| `apm lifecycle validate` | Check all discovered script files for schema errors, unknown events, missing fields, and non-HTTPS URLs | -- |
| `apm lifecycle trust` | Trust `apm.yml` `lifecycle:` at its current contents so project scripts run | -- |
| `apm lifecycle untrust` | Revoke trust for `apm.yml` `lifecycle:`; project scripts will stop running | -- |

Lifecycle scripts fire on seven events: `pre-install`, `post-install`, `pre-update`, `post-update`, `pre-uninstall`, `post-uninstall`, `pre-compile`. `post-install` fires only after success or partial success; failed and dry-run installs skip it. `pre-compile` fires before `apm compile` reads sources (not with `--dry-run`, `--validate`, `--watch`, `--root`, or `--global`). Script files are discovered from three sources (additive): policy (POSIX: `/etc/apm/policy.d/*.json`; Windows: `%ProgramData%\APM\policy.d\*.json`; JSON), user (`~/.apm/apm.yml`, YAML), project (`apm.yml` `lifecycle:` at repo root, YAML). Two script types: `command` (shell via subprocess, event JSON on stdin) and `http` (HTTPS POST). Script output is appended to `~/.apm/logs/scripts.log`. See the [Lifecycle scripts](/apm/enterprise/lifecycle-scripts/) guide for full documentation.

## Distribution

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm pack` | Build distributable artifacts (bundle and/or marketplace.json -- driven by `apm.yml`). A `dependencies:` mapping, including `dependencies: {}`, produces a bundle of local package content; omitted or null `dependencies:` does not. Default output (no format flag) is a Claude Code plugin directory. Pass `--format agent-plugin` to opt into a portable Agent Plugins v1 bundle instead -- strict portable core only (root `plugin.json`, `skills/`, root `mcp.json` written even when empty; no `agents/`, `commands/`, `instructions/`, `extensions/`, `hooks/`, or LSP payload). That bundle build fails before any output is written if the source project has non-portable agents/commands/instructions/extensions/hooks/LSP, naming the surfaces and pointing to `--format claude-plugin` (which carries LSP servers in the bundle's root `.lsp.json`). A packed Agent Plugin installs through the declarative route: declare it as a dependency in `apm.yml` and run `apm install --target copilot`, which keeps the unit whole under `apm_modules/` and registers it without locating or executing Copilot; stable Copilot CLI 1.0.81 or newer is required when loading the projection. The imperative local-bundle route still fails closed for Agent Plugin bundles. Bundles are **target-agnostic**: `pack.target` is recorded in every bundle for diagnostic purposes (typically `"all"` for target-agnostic packs, or the project's detected target) and is not authoritative at install time; `pack.bundle_files` (path -> sha256) drives integrity verification; the default format always embeds `apm.lock.yaml` with it, even when the project has no lockfile. The consumer's project decides where files land. Dependency content is packed **exclusively** from lockfile-attested `deployed_files` (in every bundle format); the `apm_modules` cache is never packed. A canvas extension the executables gate kept from deploying is not packed; pack names it in one `[!]` line with `apm approve <package>` (or `apm policy explain <package>` for a deny). Each file is verified against its `deployed_file_hashes` SHA-256 before inclusion, so a file tampered after `apm install` (hash mismatch) or deleted (missing on disk) fails the pack with a message pointing at `apm install`; files with no recorded hash (older lockfiles) pack unverified. Dependency hooks-config / MCP-config is not attested, so it is not packed -- `apm pack` warns (`[!]`) and names the dependency (first-party root hooks/MCP are still packed). Marketplace-publishing projects (`marketplace:` block, no `dependencies:`) no longer emit the misleading "No plugin.json found" warning; after a successful build, a vendor-neutral catalog of artifact paths is appended together with a single docs pointer (`consumer/installing-from-marketplaces/#install-patterns`) listing per-assistant install paths. Release-time gates `--check-versions` and `--check-clean` are opt-in and exit non-zero on misalignment / drift (codes 3 and 4 respectively) so release pipelines can fail fast; `--check-clean` is always read-only and never writes pack outputs. The version gate reads a local package's `apm.yml` first; Plugin collections without `apm.yml` use `plugin.json`'s `version`. Invalid or versionless `apm.yml` fails closed, and the fallback likewise rejects malformed or non-object JSON and a missing or blank version. When `apm.yml` declares `claude`, `copilot`, or `codex` in `targets:` (or the singular `target:`), `apm pack` also generates an ecosystem-specific `plugin.json`: `.claude-plugin/plugin.json` for Claude (includes `mcpServers` from `.mcp.json` if present, minus servers `apm install` manages), `.github/plugin/plugin.json` for Copilot (omits `mcpServers`), and `.codex-plugin/plugin.json` for Codex (identity fields plus `"skills": "./skills/"` and `"mcpServers": "./.mcp.json"` references when those root paths exist, never referencing a `.mcp.json` that holds servers `apm install` manages, so `.apm/skills/` and `apm.yml` MCP servers are not referenced; no `interface` block, so Codex shows no listing presentation metadata; warns when `version`, `description`, or `author` is missing or `name` does not start with a letter or digit or uses characters other than letters, digits, `.`, `_`, `-`, which `apm install -g --target codex` rejects). A Codex marketplace entry (`marketplace.outputs.codex`) whose plugin directory has no manifest Codex reads (schema-declared root `plugin.json` from `--format agent-plugin`, or `.codex-plugin/`, `.claude-plugin/` or `.cursor-plugin/plugin.json`) installs under the version `local`. An existing file at the target path is preserved (a warning is emitted and the write is skipped) unless `--force` is passed; `--dry-run` prevents writes. Credential-bearing keys and secret-shaped values in `.mcp.json` are stripped recursively at any depth from the Claude manifest before writing, so a committed manifest never leaks secrets (see the apm pack reference, `reference/cli/pack/#credential-stripping-claude-mcpservers`). In every format, and with `--dry-run`, `apm pack` refuses to pack a file named `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa` or `id_ed25519`: it names each path, writes nothing and exits 1. `apm publish` applies the same refusal to its auto-packed archive. | `-o PATH`, `--archive` (produce a `.zip` archive instead of a directory; changed from `.tar.gz`), `--archive-format [zip\|tar.gz]` (default `zip`; use `tar.gz` for smaller legacy CI artifacts; only active with `--archive`), `--dry-run`, `--format [plugin\|agent-plugin\|claude\|claude-plugin\|apm]` (`agent-plugin` is the sole selector for the Agent Plugin bundle; `plugin` is a compatibility alias for the Claude plugin bundle, not for `agent-plugin`; `claude`/`claude-plugin` also select the Claude plugin bundle; `apm` selects the legacy APM layout; default `claude-plugin`), `--claude-plugin` (shortcut for `--format claude-plugin`; passing more than one of `--claude-plugin`/`--format` is a usage error), `--force`, `--offline`, `--include-prerelease`, `--marketplace=FORMATS`, `--marketplace-path FORMAT=PATH`, `--json` (envelope `bundle` carries the built bundle's `path` and `files`), `--check-versions` (release gate: per-package versions match `marketplace.versioning.strategy`; exit 3 on failure), `--check-clean` (read-only release gate: regenerate-and-diff against the effective marketplace path, including `--marketplace-path` overrides; never writes pack outputs; exit 4 on drift). `-t/--target` is **deprecated** (warn only). Exit codes: `0` success, `1` build/runtime error (including `apm.yml` parse or schema errors), `2` usage error, `3` `--check-versions` misalignment, `4` `--check-clean` drift. |
| `apm unpack BUNDLE` | **[Deprecated]** Deploy a legacy `--format apm` bundle (the only route for that format). Plugin-format bundles (the default) install with `apm install <bundle-path>`; unpack refuses a bundle with a root `plugin.json` and points there. Writes only under known integration directories of the output directory and fails before writing when any lockfile entry is outside them. Refuses bundles with hooks, MCP/LSP config, `bin/` or canvas executables. Applies org policy and `require_hashes` like `apm install <bundle>`; in an APM project, records deployed files in `apm.lock.yaml` unless the lockfile already records them with the same content. | `-o PATH`, `--skip-verify`, `--force`, `--dry-run` |

For Copilot marketplace output, select `marketplace.outputs: {copilot: {}}`
(add `claude: {}` for both). The default path is
`.github/plugin/marketplace.json`; `--marketplace-path copilot=PATH`
overrides `marketplace.copilot.output`, then `marketplace.outputs.copilot.path`.
`APM_MARKETPLACE_COPILOT_PATH` is reserved, not consumed. Local source strings
keep real paths; remote GitHub/Git URL objects retain repository identity,
subdirectory, tag ref and full commit SHA. Raw commit pins omit `ref`;
annotated tags use their peeled commit. Invalid pins/unrepresentable sources
fail explicitly. Existing branch restrictions and Claude-only defaults remain.

For marketplace publishing, `--strict-metadata` preflights every selected
marketplace output before any artifact write and exits `5` if remote metadata
is unavailable. `--check-clean` also exits `4` without comparing bytes when
regeneration is not certifiable. With `--json`, inspect
`metadata_enrichment.certifiable` and each outcome's `status`
(`fetched`, `empty`, `manifestless`, `explicit`, `failed`, `offline`, or
`local`). On GitHub, `manifestless` means `apm.yml` was absent while
`SKILL.md` with non-empty string `name` and `description` frontmatter was
verified at the same resolved repository, ref, and subdirectory. Malformed
frontmatter, missing fields, non-string values, and whitespace-only values stay
`failed` and make `--check-clean` exit `4`. Authenticated fallback for private
GitHub sources checks both files through the `api.github.com` Contents API.
GitLab, Azure DevOps, and other
non-GitHub packages need fixed `description` and `version` fields for strict
certification.

`apm install <BUNDLE-PATH>` -- when the positional argument resolves to a directory containing `plugin.json` at its root, or to a `.zip` (or legacy `.tar.gz`/`.tgz`) archive whose extracted root contains `plugin.json`, install switches to local-bundle mode: the bundle is integrity-verified against its embedded `apm.lock.yaml` (`pack.bundle_files`) and deployed into the consumer's resolved target. Codex agents and Gemini commands are skipped with one `[!]` warning per file (no TOML rendering). For Codex, Gemini and OpenCode, instructions are staged under `apm_modules/<bundle>/.apm/instructions/` as `apm compile` input, recorded in `local_staged_files` with hashes in `local_staged_file_hashes` (not deployed files; an edited staged file makes `apm compile` warn and `apm audit --ci` report hash drift until `apm install <bundle> --force` restages it) and removed by `apm prune`. Root `plugin.json` routing is exclusively schema-driven: only a `plugin.json` with **no** `$schema` key follows the legacy Claude/APM path described below. If `plugin.json` declares the exact recognized Agent Plugins v1 `$schema` (`1.0.0`), the imperative bundle route fails closed instead of dissecting it, with a message pointing at the declarative dependency route (`apm.yml` + `apm install --target copilot`) that registers the plugin natively with GitHub Copilot. Any other schema-bearing `plugin.json` hard-fails rather than falling back to legacy routing: a non-string `$schema`, an Agent Plugins schema at an unsupported version, or a foreign schema id from another tool entirely all raise before any dissection is attempted. Target resolution follows the same precedence as registry installs (`--target` > `apm.yml` > directory detection); the bundle itself carries no target binding. Targets without target-native instruction deployment (opencode, codex, gemini) receive instructions staged under `apm_modules/<slug>/.apm/instructions/` and the install emits a hint to run `apm compile` to merge them. Grok Build deploys native instructions to `.grok/rules/`; run `apm compile` separately when you also want `AGENTS.md`. Other existing paths (e.g. a source-package directory without `plugin.json`, or a directory with `apm.yml` and `plugin.json` whose `apm.lock.yaml` has no `pack:` manifest) still flow through the normal local-path dependency-resolver pipeline. Files are recorded under `local_deployed_files` in the project lockfile -- `apm.yml` is **never** mutated. Bundle MCP servers are recorded with `bundle:<name>` provenance, so installs and uninstalls of other packages keep them and `apm audit --ci` / `apm install --frozen` accept them. Honours `--target`, `--global`, `--force`, `--dry-run`, `--verbose`, plus `--as ALIAS` (log/display label only). Resolver/MCP/registry/policy flags (`--update`, `--mcp`, `--parallel-downloads`, `--allow-insecure-host`, `--skill`, ...) are rejected with a single consolidated error -- local-bundle install is an imperative deploy and bypasses those subsystems.
`apm install <BUNDLE-PATH>` -- when the positional argument resolves to a directory containing `plugin.json` at its root, or to a `.zip` (or legacy `.tar.gz`/`.tgz`) archive whose extracted root contains `plugin.json`, install switches to local-bundle mode: the bundle is integrity-verified against its embedded `apm.lock.yaml` (`pack.bundle_files`) and deployed into the consumer's resolved target. Plugin hooks register through the hook integrator under the bundle's exact-key executables gate; `apm uninstall` does not remove local-bundle content. Codex agents and Gemini commands are skipped with one `[!]` warning per file (no TOML rendering). For Codex, Gemini and OpenCode, instructions are staged under `apm_modules/<bundle>/.apm/instructions/` as `apm compile` input, recorded in `local_staged_files` (not deployed files, so `apm audit --ci` does not check them) and removed by `apm prune`. Root `plugin.json` routing is exclusively schema-driven: only a `plugin.json` with **no** `$schema` key follows the legacy Claude/APM path described below. If `plugin.json` declares the exact recognized Agent Plugins v1 `$schema` (`1.0.0`), the imperative bundle route fails closed instead of dissecting it, with a message pointing at the declarative dependency route (`apm.yml` + `apm install --target copilot`) that registers the plugin natively with GitHub Copilot. Any other schema-bearing `plugin.json` hard-fails rather than falling back to legacy routing: a non-string `$schema`, an Agent Plugins schema at an unsupported version, or a foreign schema id from another tool entirely all raise before any dissection is attempted. Target resolution follows the same precedence as registry installs (`--target` > `apm.yml` > directory detection); the bundle itself carries no target binding. Targets without target-native instruction deployment (opencode, codex, gemini) receive instructions staged under `apm_modules/<slug>/.apm/instructions/` and the install emits a hint to run `apm compile` to merge them. Grok Build deploys native instructions to `.grok/rules/`; run `apm compile` separately when you also want `AGENTS.md`. Other existing paths (e.g. a source-package directory without `plugin.json`, or a directory with `apm.yml` and `plugin.json` whose `apm.lock.yaml` has no `pack:` manifest) still flow through the normal local-path dependency-resolver pipeline. Files are recorded under `local_deployed_files` in the project lockfile -- `apm.yml` is **never** mutated. Bundle MCP servers are recorded with `bundle:<name>` provenance, so installs and uninstalls of other packages keep them and `apm audit --ci` / `apm install --frozen` accept them. Honours `--target`, `--global`, `--force`, `--dry-run`, `--verbose`, plus `--as ALIAS` (log/display label only). Resolver/MCP/registry/policy flags (`--update`, `--mcp`, `--parallel-downloads`, `--allow-insecure-host`, `--skill`, ...) are rejected with a single consolidated error -- local-bundle install is an imperative deploy and bypasses those subsystems.

## Registry publishing (experimental)

Behind `apm experimental enable registries`. Pushes a package version to a REST-based APM registry declared in `apm.yml`'s `registries:` block.

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm publish` | Auto-pack a flat registry zip archive (`apm.yml` + `.apm/` + `README.md`/`CHANGELOG.md`/`LICENSE` when present) and upload to a configured registry via `PUT /v1/packages/{owner}/{repo}/versions/{version}`. Different layout from `apm pack` (no `plugin.json` wrapper). | `--registry NAME` (required when multiple registries are configured), `--package OWNER/REPO` (**required** - registry package identity, e.g. `acme/my-skill`; a host or URL is kept in the identity, so `gitlab.com/acme/x` publishes as owner `gitlab.com/acme`, matching what consumers request), `--zip PATH` (skip auto-pack and upload a pre-built `.zip`), `--dry-run`, `-v`/`--verbose` |

Examples:

```bash
# Auto-pack and publish when only one registry is configured
apm publish --package acme/my-package

# Choose a registry when multiple are configured, preview first
apm publish --package acme/my-package --registry corp-main --dry-run -v
apm publish --package acme/my-package --registry corp-main

# Publish a pre-built zip (skill-only or custom layout)
apm publish --package acme/my-package --zip ./build/my-package-1.0.0.zip --registry corp-main

```

Exit codes: `0` published (or `--dry-run` ok), `1` publish failure (missing `apm.yml`/`.apm/`, auth `401`/`403`, version conflict `409`, server validation `422`, network/registry error, registries feature disabled, no registries configured, unknown `--registry` name), `2` usage error (missing or malformed `--package`, multiple registries without `--registry`, invalid flag combination).

Credentials resolve via `APM_REGISTRY_TOKEN_{NAME}` env var (or `apm config set registry.<name>.token`); see `authentication.md` for the full registry token chain.

## Marketplace (consumer)

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm marketplace add SOURCE` | Register a marketplace. `SOURCE` accepts `OWNER/REPO`, `HOST/OWNER/REPO`, nested `HOST/group/sub/.../REPO` (each with optional `#ref`), HTTPS git URL with optional `#ref`, hosted `marketplace.json` URL, SSH URL (`git@host:org/repo.git` or `ssh://git@host[:PORT]/org/repo.git`), local directory or file path (including a local `apm.yml` read as a live catalog), or `file://` URI. `--ref` applies only to git-backed sources; `--host` applies only to shorthand sources. Without `--path`, the catalog is discovered at `marketplace.json`, `.github/plugin/marketplace.json`, then `.claude-plugin/marketplace.json` (first wins). `--path` selects a repository-relative catalog JSON (for example one per audience, `catalogs/frontend/marketplace.json`), is saved in the registration, and must stay inside the repository; not for hosted `marketplace.json` URLs or a direct file. Separate catalogs are not access control. Registering a second name for an already registered catalog (same origin and path) warns `[!]`. | `-n NAME`, `-r REF`, `--host HOST`, `--path PATH`, `--offline` (local `apm.yml` only) |
| `apm marketplace list` | List registered marketplaces | -- |
| `apm marketplace browse NAME` | Browse marketplace plugins as a list of full install names (`plugin@marketplace`) with wrapped descriptions; reread live local `apm.yml` catalogs | `--offline` (local YAML only) |
| `apm marketplace update [NAME]` | Revalidate marketplace index; reread live local `apm.yml` catalogs. A failed catalog keeps its last valid cache and the command exits non-zero naming it | -- |
| `apm marketplace remove NAME` | Remove a marketplace | `-y` skip confirm |
| `apm marketplace validate NAME` | Validate raw structure, plugin schema, and duplicate plugin names. Add accepts unsupported or malformed entries with a warning; validate reports paths and exits 1 without rewriting the source manifest or registered marketplace entry. | `-v` |
| `apm search QUERY@MARKETPLACE` | Search marketplace | `--limit N` |
| `apm install NAME@MKT[#ref]` | Install from marketplace | Optional `#ref` override |
| `apm view NAME@MARKETPLACE` | View marketplace plugin info | -- |
| `apm install -g` (with `marketplaces:` in `$APM_HOME/apm.yml`) | Reconcile the user manifest `marketplaces:` list (`name`, `source`, optional `ref`) before installing: register, update, or remove entries it added. Manual `apm marketplace add` registrations always win. Discovery only. See dependencies.md. | `--dry-run` previews without fetching or writing |

## Marketplace authoring

For a live local preview, run `apm marketplace add ./apm.yml --name alice-dev`.
The file must contain a valid `marketplace:` declaration. Browse, view, update
and selected installs reread it on later invocations; relative package paths
resolve from the file's directory. Registration neither installs inventory nor
executes hooks, publishes packages or writes generated catalogs. `add --offline`
and `browse --offline` are limited to local YAML: local entries and commit SHAs
need no network, while uncached remote tags/ranges fail. Ref caches are not
persisted across CLI invocations. Remote entries install
through their actual dependency source and existing lock resolution, so locked
replay does not require the preview. Local directory dependencies remain local.
Native generated JSON and repository registration retain their existing flow.

> Source of truth is the `marketplace:` block in `apm.yml`. `apm pack` produces `.claude-plugin/marketplace.json` whenever that block is present. The legacy standalone `marketplace.yml` is deprecated -- use `apm marketplace migrate` to fold it in.

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm marketplace init` | Append a `marketplace:` block to `apm.yml` (scaffolds `apm.yml` if missing; does not create `.claude-plugin/`) | `--force`, `--no-gitignore-check`, `--name`, `--owner`, `-v` |
| `apm marketplace migrate` | Fold a legacy `marketplace.yml` into `apm.yml`'s `marketplace:` block; deletes `marketplace.yml` on success | `--force`/`--yes`/`-y`, `--dry-run`, `-v` |
| `apm marketplace outdated` | Report upgradable plugins, range-aware; respects `tag_pattern` and common monorepo tag layouts | `--offline`, `--include-prerelease`, `-v` |
| `apm marketplace check` | Validate the `marketplace:` block and verify refs resolve | `--offline`, `-v` |
| `apm marketplace audit NAME` | Supply-chain audit for plugin deps; local string sources are contained in the registered root | `--strict` (CI exit-1 on bypasses, skipped sources, verification errors, or no verified plugins), `-v` |
| `apm doctor` | Diagnose git, network, auth, marketplace config readiness, and (when a `marketplace:` block is present) **format coverage** -- which output profiles are configured vs. supported, so producers can spot easy reach wins (e.g. add `codex: {}` or `copilot: {}` to also publish for Codex or Copilot CLI consumers). The executable-trust row names malformed configuration under either `executables` or the deprecated `allowExecutables` key and reports local allows overridden by org policy. GitHub CLI is one auth source, not a separate check. Informational rows never affect exit code. | `-v` |
| `apm marketplace package add <source>` | Add a plugin entry to `marketplace.plugins` (source accepts `owner/repo`, `HOST/OWNER/REPO`, an HTTPS URL, or `./path` with `--no-verify` plus `--version` or `--ref`) | `--name`, `--version`, `--ref` (mutable refs auto-resolved to SHA; a tag must exist on the remote or the command exits 2 without writing, unless `--no-verify`), `-s`/`--subdir`, `--tag-pattern`, `--tags`, `--category`, `--include-prerelease`, `--no-verify`, `-v` |
| `apm marketplace package set <name>` | Update fields on an existing plugin entry | `--version`, `--ref` (mutable refs auto-resolved to SHA; a missing tag exits 2 without writing), `--subdir`, `--tag-pattern`, `--tags`, `--category`, `--include-prerelease`, `-v` |
| `apm marketplace package remove <name>` | Remove a plugin entry from `marketplace.plugins` | `-y`/`--yes`, `-v` |

To build the marketplace, run `apm pack` (it reads `apm.yml` and writes `.claude-plugin/marketplace.json` whenever the `marketplace:` block is present). On a fresh project, `apm marketplace init` scaffolds `apm.yml` with the `marketplace:` block in place (`apm init --marketplace` was removed in 0.37).

## MCP servers

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm mcp install NAME [-- CMD...]` | Add an MCP server (alias for `apm install --mcp`; forwarded options share `apm install` help and completion) | `-g`/`--global`, `-t`/`--target`, `--transport`, `--url`, `--env`, `--header`, `--mcp-version`, `--registry URL`, `--trust-transitive-mcp`, `--dev`, `--force`, `--dry-run`, `-v`/`--verbose`, `--no-policy` |
| `apm mcp list` | List servers in the MCP registry catalog (not the project's servers) | `--limit N` (default `20`), `-v` |
| `apm mcp search QUERY` | Search MCP registry | `--limit N` |
| `apm mcp show SERVER` | Show server details | -- |

Self-defined stdio MCP entries declared in `apm.yml` (`env:` / `args:`) have their placeholders resolved at install time on Codex, Gemini, and Antigravity, which have no runtime interpolation. Copilot preserves env references as `${VAR}`; JetBrains preserves them as `${env:VAR}`. Cursor normalizes references in `env`, `args`, and `headers` to `${env:VAR}` for runtime resolution; this normalization does not cover `command` or `url`. All three env syntaxes are accepted: `${VAR}`, `${env:VAR}`, and the legacy `<VAR>`. Missing variables fall back to an interactive prompt on install-time targets (suppressed in non-TTY contexts). See [Manifest schema -- MCP placeholder syntaxes](https://microsoft.github.io/apm/reference/manifest-schema/) for the per-target matrix.

Set `MCP_REGISTRY_URL` (default `https://api.mcp.github.com`) to point all `apm mcp` commands and `apm install --mcp` at a custom MCP registry. The URL is validated at startup and must use `https://`; set `MCP_REGISTRY_ALLOW_HTTP=1` to opt in to plaintext `http://` for development. The registry must implement the [MCP Registry v0.1 spec](https://github.com/modelcontextprotocol/registry) (apm calls `/v0.1/servers/...`); legacy `/v0/`-only registries will return 404. Direct install pre-flight fails closed when any registry is unreachable and accepts a bare server name only when it has one unique registry match. A no-match error names the searched registry and how to choose another; APM never falls back to another registry. See [Choosing an MCP registry](https://microsoft.github.io/apm/consumer/install-mcp-servers/#choosing-an-mcp-registry).

For a v0.1 package with `registryType: oci`, install generates a `docker`
launcher automatically. Docker run options stay before the image and
package arguments stay after it. Options-only `runtimeArguments` (no
leading `run`) gain `run -i --rm` ahead of them. Docker must be available when the
harness starts the server; no manual launcher override is required.

For Copilot-family adapters, non-container install preserves
typed v0.1 registry arguments and legacy `value_hint` arguments while
keeping exactly one semantic package identity. Registry defaults resolve,
secret variables use target-native references, unresolved optional groups
are omitted atomically, and malformed or unresolved required entries fail
closed.

## Runtime management (experimental)

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm runtime setup {copilot\|codex\|gemini\|llm}` | Install a runtime. Codex verifies the GitHub Releases SHA-256 digest before extracting and fails on missing or mismatched digests. | `--version`, `--vanilla` |
| `apm runtime list` | Show installed runtimes | -- |
| `apm runtime remove {copilot\|codex\|gemini\|llm}` | Remove a runtime | `-y`, `--yes` |
| `apm runtime status` | Show active runtime | -- |

On Linux/macOS, Codex archive downloads through curl reject HTTP errors and retry
transient failures up to three times in a 30-second retry window (10 seconds to
connect, 120 seconds per transfer; an active transfer may outlast the window).
Failed downloads are cleaned up without replacing an existing runtime.
Checksum mismatches fail immediately, without retry or bypass.

Workflow adapters enforce streaming wall-clock deadlines for Copilot (600s)
and Codex (300s), terminating and reaping the child process on expiry.

## Experimental features

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm experimental` | Default to `apm experimental list` | `-v` verbose |
| `apm experimental list` | List registered experimental flags or emit JSON for automation | `--enabled`, `--disabled`, `--json`, `-v` verbose |
| `apm experimental enable NAME` | Enable an opt-in experimental flag | `-v` verbose |
| `apm experimental disable NAME` | Disable an opt-in experimental flag | `-v` verbose |
| `apm experimental reset [NAME]` | Reset one flag or all flags to defaults; also cleans malformed overrides during bulk reset | `-y` skip confirm, `-v` verbose |

Use `apm experimental enable copilot-cowork` to turn on Microsoft 365 Copilot Cowork skill deployment. Once enabled, deploy skills with `apm install --target copilot-cowork --global`.

Use `apm experimental enable copilot-app` to turn on GitHub Copilot desktop App workflow deployment. Once enabled, prompts that carry workflow frontmatter -- any flat top-level key of `interval`, `schedule_hour`, `schedule_day` -- can be deployed to the App's SQLite store at `~/.copilot/data.db` with `apm install --target copilot-app` (project scope) or `--target copilot-app --global` (user scope). A `.prompt.md` belongs to exactly ONE surface: workflow-shape prompts go to the App DB, plain prompts go to slash-command targets. Rows always start `enabled = 0` -- you opt in from the App. `apm install / update / uninstall` preserve user state (`enabled`, `last_run_at`, schedule overrides). Override the database path with `APM_COPILOT_APP_DB=<abs-path>`. Workflows are scoped to a real Copilot App project: when the App is running APM registers the project over the App's loopback WebSocket so the project is immediately known to the webview; when the App is closed APM falls back to a direct-SQLite `BEGIN IMMEDIATE` resolver. The first install in a brand-new repo prints a one-time "restart the Copilot App once" hint (see github/github-app#5483); subsequent installs are silent. `--global` installs that carry workflow-shape prompts warn-and-proceed because workflows run with `CWD=~/.copilot` rather than a repo -- attach the row to a project from the App's Workflows tab to fix.

Use `apm experimental enable openclaw` to turn on OpenClaw agent runtime skill deployment. Once enabled, deploy skills with `apm install --target openclaw` (project scope, `.agents/skills/`) or `apm install --target openclaw --global` (user scope, `~/.openclaw/skills/`). At project scope, output is identical to `agent-skills`; the `--global` user path is the distinguishing capability.

### Cross-client skills (`agent-skills`)

Use `--target agent-skills` to deploy skills to `.agents/skills/` -- the cross-tool standard directory. This is useful when multiple clients (Codex, future tools) read from `.agents/skills/`. Unlike `--target all`, `agent-skills` must be requested explicitly: `apm install --target agent-skills` or `apm install --target all,agent-skills` for both. `apm compile --target agent-skills` is a no-op (skills-only target).

> **Note:** `--target agents` is **deprecated** -- it maps to `copilot` (`.github/`), not `.agents/`. Use `--target copilot` or `--target agent-skills` instead.

### Skill routing convergence

Unmanaged skill conflicts now fail by default instead of warning and exiting
successfully. Install preflights every selected skill destination before
deploying any package in the plan or committing manifest/lock success state.
Rename the local skill or correct an existing `skills:`/`targets:` selection;
an allow-list does not automatically include future skills. Cached preparation
may remain, and old partial state is preserved. A shared name/directory alone
is not a conflict, and a skill directory with no files or symlinks at any
depth is deployed into. `--force` remains destructive and also bypasses the critical
security-scan block. Audit reports any differing file no lockfile entry claims
(skipped user files, refused skills, case-only variants) as
`unmanaged-conflict`, not managed `modified` drift, without the export-patch
hint, and does not repair them.

By default, Copilot, Cursor, OpenCode, Codex, Gemini, Antigravity, and Windsurf deploy skills to `.agents/skills/` (the agentskills.io standard). Claude, Grok Build, and Kiro retain native per-client routing (`.claude/skills/`, `.grok/skills/`, `.kiro/skills/`). Use `--legacy-skill-paths` (or `APM_LEGACY_SKILL_PATHS=1`) to restore the previous per-client layout (`.github/skills/`, `.cursor/skills/`, `.gemini/skills/`, etc.). `apm audit --ci` and `apm uninstall` reuse the skill layout recorded in `apm.lock.yaml`. Legacy per-client skill paths recorded in `apm.lock.yaml` are auto-migrated to `.agents/skills/` on the next `apm install`; foreign / hand-authored skills outside the lockfile are never touched.

`apm install --target kiro` also delivers Kiro CLI prompts as body-only
`.kiro/prompts/<name>.md`; add `--global` for `~/.kiro/prompts/<name>.md`.
This prompt mapping is qualified with Kiro CLI 2.33.0 V3, not the IDE.
See `package-authoring.md` for native arguments and frontmatter limitations.

Experimental flags MUST NOT gate security-critical behaviour (content scanning, path validation, lockfile integrity, token handling, MCP trust, collision detection). Flags are ergonomic/UX toggles only.

## Configuration and updates

Updating selected packages preserves deployment targets in `apm.lock.yaml` for
refreshed and untouched dependencies, including shared `.agents/skills/` paths.
No follow-up install is needed to restore those target records.

| Command | Purpose | Key flags |
|---------|---------|-----------|
| `apm auth HOST` | Report the credential APM resolves for a git host and, if none, walk through creating a token | `--check` validate against the host's REST API (one network call), `--export` print `export VAR=token` on stdout for `eval`, `-v` |
| `apm config` | Show current configuration | -- |
| `apm config get [KEY]` | Get one config value; with no key, prints the user config summary (effective values for core user-settable keys; omits noise-reduction defaults/unset optional values until configured) (`target`, `self-update.channel`, `self-update.install-dir`, `temp-dir`, `allow-protocol-fallback`, `prefer-ssh`, `github-auth-first`, `copilot-cowork-skills-dir`, `mcp-registry-url`) | -- |
| `apm config list` | Show the default user config listing; this aliases `apm config get` with no key and omits noisy defaults/unset optional values until configured. With the `registries` flag on, also lists `registry.<name>` entries | -- |
| `apm config set KEY VALUE` | Set a config value (`target`, `self-update.channel`, `self-update.install-dir`, `temp-dir`, `allow-protocol-fallback`, `prefer-ssh`, `github-auth-first`, `mcp-registry-url`; `copilot-cowork-skills-dir` requires `apm experimental enable copilot-cowork`; `auto-integrate` is deprecated, has no effect, and warns on get/set) | -- |
| `apm config unset KEY` | Remove a stored config value (`target`, `self-update.channel`, `self-update.install-dir`, `temp-dir`, `allow-protocol-fallback`, `prefer-ssh`, `github-auth-first`, `copilot-cowork-skills-dir`, `mcp-registry-url`) | -- |
| `apm lock` | Resolve all dependencies in `apm.yml` and write `apm.lock.yaml` **without** deploying or deleting files in agent targets. Existing deployed-file provenance stays recorded while those bytes remain on disk; the next normal install performs any deferred prune. Mirrors `cargo generate-lockfile` / `pnpm lock`. A symlinked `apm.lock.yaml` is never written through: lockfile-writing commands exit nonzero and leave the link and target unchanged. | `--update` re-resolve to latest upstream SHAs without accepting stale local bare refs, `--verbose`, `-g/--global`, `--no-policy`, `--target` (comma-separated), `--parallel-downloads N` |
| `apm lock export` | Export an SBOM/inventory from the **existing** `apm.lock.yaml`, falling back to legacy `apm.lock` read in place without migration -- no re-resolve, re-hash, or network. Emits component identity (purl: `pkg:github/<owner>/<repo>@<commit>` for GitHub sources, `#<subpath>` for packages inside a repository, `pkg:generic` for other hosts and local content), the lock's SHA-256 content hashes, and the declared license. Output is deterministic (components sorted by purl, pinned timestamp) for byte-identical reproducibility. Diagnostics and startup notices use stderr so stdout stays machine-readable. This is an inventory export, not a security attestation. | `-f/--format [cyclonedx\|spdx]` (default `cyclonedx`), `-o/--output FILE` (default stdout), `-g/--global` read user-scope lockfile, `--timestamp ISO8601` pin the document timestamp (falls back to `SOURCE_DATE_EPOCH`, then the lockfile's legacy `generated_at`, then the Unix epoch) |
| `apm update [PKGS...]` | Refresh APM dependencies: resolves `apm.yml` against authenticated upstream refs, never stale bare-cache refs, prints a structured plan (added/updated/removed/unchanged), and prompts before changing refs (default `[y/N]`). Remote-resolution failure aborts without substituting cached ref state. Full-SHA pins move only to an eligible stable annotated semver tag. If none exists, APM keeps the current SHA, emits one summary warning, and continues unrelated updates; malformed or ambiguous tag records remain fatal before writes. Accepted pin updates are annotated as `# <tag>` in `apm.yml`. Pass `[PKGS...]` to refresh only those deps, or `-g` for user scope (`~/.apm/`). Successful no-op updates still reconcile deployed artifacts, lockfile ownership, and merge-hook config/sidecar entries when the declared target set contracts. If the lock expects dependencies but `apm_modules/` is empty, an unchanged update restores the cache from the same refs without prompting or rewriting the manifest/lock; `--dry-run` remains read-only. `--force` changes collision/security handling, not freshness; use it only after independent verification. Replaces the deprecated `apm deps update` for every option except `--legacy-skill-paths`. Skips the ref-change prompt with `--yes`; previews with `--dry-run`. | `--yes`, `--dry-run`, `--verbose`, `-g/--global`, `--force`, `--parallel-downloads N`, `--target` (comma-separated), `--trust-transitive-mcp` (one-run trust for unapproved transitive MCP servers, as in `apm install`; never saved, keep one with `apm approve`), `--legacy-skill-paths` (or `APM_LEGACY_SKILL_PATHS=1`) keeps per-client skill dirs, `--policy SOURCE` also enforce a policy, as in `apm install` (tighten-only; fails closed when it cannot load; not valid with `-g`) |
| `apm self-update` | Update a standalone APM installation. Homebrew core disables this command; use `brew upgrade apm` instead. | `--check` only check (standalone installs) |

`apm config` output never prints credential values: a stored `registry.<name>.token` shows as `******** (set)` or `Not set`, URLs drop embedded `user:password@` credentials and mask query values, and credential flag values such as `--api-key` show as `[REDACTED]`. There is no reveal flag.

`apm config set prefer-ssh true` and `apm config set allow-protocol-fallback true` persist transport preferences to `~/.apm/config.json` so SSH-only and corporate GHES users no longer need to re-pass `--ssh` / `--allow-protocol-fallback` on every `apm install`. Resolution order: CLI flag > `APM_GIT_PROTOCOL` / `APM_ALLOW_PROTOCOL_FALLBACK` env var > `apm config` value > built-in default (`false`). `apm config unset prefer-ssh` and `apm config unset allow-protocol-fallback` remove the persisted value. In `apm config` / `apm config list` / `apm config get` (no key), the two transport rows surface only when they have been enabled (the `false`-default rows are suppressed to keep the output noise-free); `apm config get <key>` always returns the effective value. Setting `allow-protocol-fallback=true` while `CI=1` emits a warning because the persisted value affects every subsequent `apm install` on a shared `$HOME`; prefer the env var in CI.

`apm config set github-auth-first true` (or `--auth-first` / `APM_GITHUB_AUTH_FIRST=1`) skips the initial anonymous HTTPS attempt for exact-host `github.com` when a credential resolves -- resolution order and the `false`-default noise suppression mirror `allow-protocol-fallback` above. `apm config unset github-auth-first` removes the persisted value. It is a credential-order setting, not a transport selector, so it composes with `--ssh` / `--https` / `--allow-protocol-fallback`.

`apm config set target <value>` persists a default install target (single token or comma-separated list) for `apm install` when both `--target` and `apm.yml target(s)` are absent. `apm config unset target` removes this fallback.

Self-update preferences: `self-update.channel` accepts `stable` or `prerelease`; `self-update.install-dir` is optional. On Unix, a set directory must match the existing installation, while leaving it unset preserves the detected installation. Windows keeps its installer destination/default behavior. `VERSION` pins a release; `APM_SELF_UPDATE_CHANNEL` and `APM_INSTALL_DIR` override config. Both channels pass a normalized `v<version>` to the installer and GitHub/GHES script URL. `APM_INSTALLER_BASE_URL` stays authoritative, receiving only the script name. Config excludes credentials, mirror URLs, commands, and installer arguments.

Linux bootstrap/self-update: prebuilt binaries (x86_64 and ARM64) require glibc 2.28+ (RHEL/Rocky/Alma 8+, Debian 10+, Ubuntu 20.04+). On older systems, use pip with a working Python 3.11+; the floor applies only to prebuilt binaries, not to a system Python running pip.

Unix self-update passes the running binary's identity to preserve its launcher/bundle destinations, never invokes `sudo`, and rejects conflicting overrides or switching an existing install to pip. Set or unset install-directory preferences do not migrate it. Follow [Unix ownership and migration](./installation.md#unix-ownership-and-migration) for administrator/package-manager installs.

`apm self-update` shares the Windows installer codepath used by `install.ps1`: it stages the new release under `%LOCALAPPDATA%\Programs\apm\releases\<tag>` before running `apm.exe --version`, so an AppLocker / WDAC allow-list rule for `%LOCALAPPDATA%\Programs\apm\*` suffices. When the smoke test fails with HRESULT `0x80070005` (`Access is denied`), the installer emits a specific AppLocker/WDAC diagnostic with three remediations (allow-list rule, set `APM_TEMP_DIR` to an allow-listed path, or fall back to `pip install --user apm-cli`) instead of silently retrying via pip.

`apm self-update` (and the startup version-checker) honours the same env vars as `install.sh` for air-gapped and GitHub Enterprise Server (GHE) environments: `GITHUB_URL` overrides the GitHub base URL and API host (`{GITHUB_URL}/api/v3` for GHE), `APM_REPO` overrides the repository (default `microsoft/apm`), and `VERSION` pins a release and skips the GitHub API call entirely. Example: `GITHUB_URL=https://gh.corp.com APM_REPO=corp/apm VERSION=v1.2.3 apm self-update`.

`apm config set copilot-cowork-skills-dir <absolute-path>` persists the Cowork skills directory across shells. `apm config get copilot-cowork-skills-dir` and `apm config unset copilot-cowork-skills-dir` remain available even when the `copilot-cowork` flag is disabled so leftover state can still be inspected or cleared. In `apm config` and bare `apm config get`, the `copilot-cowork-skills-dir` entry is shown only when the `copilot-cowork` flag is enabled.

`apm config set external.<name>.llm true|false` and `apm config set external.<name>.args -- "<flags>"` persist per-scanner external-scanner defaults to `~/.apm/config.json` (JSON section `external_scanners.<name>.{llm,args}`), behind `apm experimental enable external-scanners`. `<name>` is validated against the supported scanners (e.g. `skillspector`). `.args` is shlex-split and stored as a list; use the `--` separator so Click does not parse a leading `--flag` as an option. `apm config get external.<name>.{llm,args}`, `apm config unset external.<name>.{llm,args}`, and `apm config unset external.<name>` (removes both) round out the surface. These keys are reachable only when the flag is enabled; bare `apm config get` lists set external keys when the flag is on. CLI flags (`--external-llm`, `--external-args`) override these values for a single run.

`apm config set mcp-registry-url https://mcp.internal.example.com` persists a private MCP registry URL so users do not need to export `MCP_REGISTRY_URL` every session. Accepts `http://` or `https://` URLs; all other schemes are rejected. Resolution order: `--registry <url>` flag on `apm mcp install` / `apm install --mcp` > `MCP_REGISTRY_URL` env var > `mcp-registry-url` in `~/.apm/config.json` > built-in public default. The flag is used immediately and persisted on the dependency in `apm.yml`. The chain resolves every registry lookup, including the `dependencies.mcp` entries `apm install` reads from `apm.yml` (a per-dependency `registry:` URL overrides it for that entry only). When a non-default layer is active, `apm mcp search` prints `Registry: <url> (<source>)` and `apm install` prints `Using MCP registry: <url> (<source>)`; the source identifies `MCP_REGISTRY_URL`, apm config, or an explicit dependency setting. `apm config unset mcp-registry-url` removes the persisted URL.

`apm approve [PACKAGE_REF...]` grants a package permission to deploy executable primitives (hooks, `bin/`, self-defined MCP servers, LSP servers, canvas extensions). By default it writes the project `apm.yml` `executables.allow` block -- committed to source control, so the whole team inherits the trust decision. `apm approve --user` records a personal grant in `~/.apm/config.json` instead (machine-local, never committed, lower authority than org or project denies). Either way, `apm approve <pkg>` lists what the package runs (type, item, summary, 8-hex fingerprint), then pins each hook file (package-relative path; for a normalized plugin, the publisher file its `.apm/hooks/` copy came from) and self-defined MCP server (name) to a full `sha256:<64 hex>` fingerprint of its definition; `bin`, `lsp` and `canvas` stay `true`. Pinning a transitive registry server with its own `registry:` or an overlay needs registry access. It refuses a package whose installed files differ from `apm.lock.yaml` (run `apm install`, then retry). Re-approving a legacy `true` grant prints `was approved for any content; now pinned to N ...`. Native Copilot/Codex plugins and user-scope (`-g`) Claude plugins are loaded intact, so approve records one `plugin:` pin over the whole plugin root (file paths and bytes; executable bits are left out so the pin holds across operating systems); any change parks the plugin with `Parked native plugin <pkg>: content changed since approval`. An Agent Plugin's root `mcp.json` counts as `mcp`, plus `bin` for a server that runs a command; `--pending`, `--list` and `apm policy explain` list both, and a type-level grant missing one parks the plugin with `not approved (missing grant: bin)`. Naming a package lifts its deny in the store being written (`Cleared deny for <pkg>`); `--all` and `--recommended` skip denied packages. `apm approve -g <pkg>` approves a package `apm install -g` installed (from any directory, writing `$APM_HOME/apm.yml`); user-scope parked notices print this form, and `apm deny -g` mirrors it. A Codex bundle with unapproved executables is parked at `apm install -g` (nothing registered or projected) until approved. `apm approve`, `apm deny` and `apm policy explain` resolve the spec a package was installed with (`plugin@marketplace`) to its store identity, or exit 1 without writing. `apm approve` exits 1 when a requested package is not installed (a spec with `#ref` prints the identity to approve instead), could not be pinned, the first-approval prompt is declined, or a deny in another layer (`--user` or org) still blocks it; the error names that layer. Admission order, store shape and audit consent: see governance.md.

`apm approve` with no package and no `--pending`/`--all`/`--recommended`/`--list` is a usage error (exit 2). `apm update <pkg>` naming a package `apm.yml` does not declare exits 1, also when no APM dependencies are declared.

The project `apm.yml` must carry an `executables:` block (even empty `{}`) to enable the gate; without it, all executables are allowed unconditionally. The first project approval creates that block: when no org policy enables the gate either, it lists the other packages the next install will park and asks `Continue? [y/N]` on a TTY (without one it writes and warns). `--user` never enables the gate. Flags: `--pending` lists each item as `[approved]`, `[approved, any content]`, `[changed since approval]`, `[denied]` or `[not approved]` (before any layer enables the gate, an item that deploys anyway is `[deployed: no executables policy]`); `--all` pins every pending package; `--recommended` pins the org `executables.recommend` set; `--list` shows the effective decision and deciding layer per installed package, labelling hooks/mcp grants `pinned` or `any content`. The interactive install prompt also writes pins (to `~/.apm/config.json`).

`apm deny [OPTIONS] PACKAGES...` runs from an APM project with `apm.yml` and writes a block to the project `executables.deny` (or `~/.apm/config.json` with `--user`); deny always wins. `apm deny` resolves the spec the way `apm approve` does (including `plugin@marketplace`) and exits 1 without writing when it matches no installed package; an installed package that declares no executables yet gets all supported executable types denied. The next `apm install` or `apm update` (even with no ref changes) removes hooks and hook scripts the package deployed earlier from every harness config, as it does when an approval is removed while the gate is on; the skip message names the package as denied. Until then, `apm audit --ci` reports the leftover hooks as drift. `apm policy explain <PACKAGE_REF>` (a subcommand of the `apm policy` group, sibling to `apm policy status`) prints the effective decision for a package: allowed or blocked per type, the deciding policy layer, and any shadowed (overridden) layers. `apm doctor` adds a fleet-level executable-trust drift check that flags packages allowed locally but denied by org policy.
`apm deny [OPTIONS] PACKAGES...` runs from an APM project with `apm.yml` (`--user` also works outside a project, as does `apm approve --user`, reading packages from `apm install -g`) and writes a block to the project `executables.deny` (or `~/.apm/config.json` with `--user`); deny always wins. `apm deny` resolves the spec the way `apm approve` does (including `plugin@marketplace`) and exits 1 without writing when it matches no installed package; an installed package that declares no executables yet gets all supported executable types denied. The next `apm install` removes hooks and hook scripts the package deployed earlier from every harness config, as it does when an approval is removed while the gate is on; the skip message names the package as denied. `apm policy explain <PACKAGE_REF>` (a subcommand of the `apm policy` group, sibling to `apm policy status`) prints the effective decision for a package: allowed or blocked per type, the deciding policy layer, and any shadowed (overridden) layers. `apm doctor` adds a fleet-level executable-trust drift check that flags packages allowed locally but denied by org policy.

The legacy top-level `allowExecutables:` block is a deprecated alias for `executables.allow`, read for one minor cycle and migrated on the next approve/deny write; the standalone `~/.apm/approvals.yml` is removed and migrated into `~/.apm/config.json` on first read. `apm approve` writes version-blind keys as `--pending` prints them (`owner/repo`, `./pkgs/x`, `local:pkgs/leaf`) and folds older `owner/repo#version` spellings, which still match regardless of installed version. Local bundle grants are content-bound to the full `name#version@sha256:<digest>` key printed by `apm install`: run `apm approve "<key>"` (add `-g` for a bundle installed with `apm install -g`, `--user` for the personal store), then rerun `apm install <bundle>`. It writes the same grant as adding the key under `executables.allow`, which stays an equivalent path; `apm approve --pending` lists parked bundles with their keys. A key with a wrong digest, or one printed before the bundle bytes changed, is refused (exit 1, `[x]`, nothing written); changed bundle bytes need a new grant. A dependency redirected by `replace:` is trusted by its selected source: `apm approve <original>` writes the `with:` source key (for example `../local-hooky`), an upstream grant does not unlock it, and `apm deny <original>` denies both the source key and the original. In CI, run `apm approve <pkg>` locally and commit `apm.yml` (also the migration for CI that relied on `--trust-transitive-mcp`); untrusted required executables fail `apm audit` with `required-executable-untrusted`.
