# Package Authoring

## Supported package layouts

APM recognizes three layouts. The shape of the package root tells APM
how to install it:

| Root signal | Author intent | Install semantic |
|---|---|---|
| `.apm/` (with or without apm.yml) | Multiple independent primitives | Hoist each primitive into the consumer runtime dirs |
| `SKILL.md` (alone, or with apm.yml = HYBRID) | One skill bundle | Copy whole tree to `<target>/skills/<name>/` |
| `plugin.json` / `.claude-plugin/` | Claude plugin collection | Dissect via plugin artifact mapping |

The HYBRID layout (apm.yml + SKILL.md) is a single skill bundle that
also uses APM dependency resolution. APM installs it as a skill -- it
does NOT dissect the bundle into top-level primitives. Co-located
subdirectories like `agents/`, `assets/`, `scripts/` are bundle
resources, not standalone primitives.

In a HYBRID package, `apm.yml` and `SKILL.md` each own their
`description` field **independently** -- APM never merges or
backfills one from the other:
- `apm.yml.description` is a short human-facing tagline rendered by
  `apm view`, `apm search`, `apm deps list`, and registry listings.
- `SKILL.md` `description` (frontmatter) is the agent-runtime
  invocation matcher (per agentskills.io). APM copies `SKILL.md`
  byte-for-byte and never reads or mutates this field.
- `allowed-tools` lives exclusively in `SKILL.md` frontmatter; there
  is no apm.yml-side equivalent.
- `name`, `version`, `license`, `dependencies`, `scripts` live
  exclusively in `apm.yml`.

Populate both descriptions when you ship a HYBRID package. `apm pack`
warns when `apm.yml.description` is missing so listings do not
degrade silently while the agent runtime keeps working.

## Package directory structure (APM layout)

```
my-package/
  apm.yml                              # package manifest (required)
  .apm/                                # local primitives directory
    instructions/
      security.instructions.md
      python.instructions.md
    chatmodes/
      architect.chatmode.md
    contexts/
      codebase.context.md
    prompts/
      code-review.prompt.md
    agents/
      reviewer.agent.md
    skills/
      my-skill/
        SKILL.md
        resource1.md
        resource2.md
```

## Install-time discovery rules

`apm pack` (export) is liberal: it collects primitives from both
`.apm/<type>/` and root convention directories (`agents/`, `skills/`,
`instructions/`, etc.). `apm install` (integration) is per-primitive
and stricter. Authors who rely on root convention directories for
instructions or prompts will produce bundles that pack but install
silently incomplete.

Per-primitive scan paths for `apm install`:

| Primitive | Scanned path | Root alternative? |
|-----------|-------------|------------------|
| instruction | `.apm/instructions/` | No |
| command (prompt) | `.apm/prompts/` | No |
| hook | `.apm/hooks/` | Yes: `hooks/` |
| agent | `.apm/agents/`, `.apm/chatmodes/` | Yes: `*.agent.md` and `*.chatmode.md` at root |
| skill | `.apm/skills/<name>/` | Yes: `skills/<name>/` (SKILL_BUNDLE or MARKETPLACE_PLUGIN) |

**Recommendation for marketplace publishers:** use `.apm/<type>/` for
every primitive. This is the only layout that is symmetric between
`apm pack` and `apm install`. Authoring `instructions/` at the plugin
root will pack cleanly but instructions will be silently dropped when
consumers run `apm install`.

## Hook files

Packages can ship hooks (pre/post tool-use scripts) by placing JSON
files under `hooks/` or `.apm/hooks/`.  When a package targets multiple
tools, use target-specific filenames so each tool receives only its own
hooks:

| Filename pattern | Deployed to |
|---|---|
| `*-copilot-hooks.json` | GitHub Copilot only |
| `*-cursor-hooks.json` | Cursor only |
| `*-claude-hooks.json` | Claude Code only |
| `*-codex-hooks.json` | Codex CLI only |
| `*-gemini-hooks.json` | Gemini CLI only |
| `*-antigravity-hooks.json` | Antigravity CLI only |
| `*-windsurf-hooks.json` | Windsurf only |
| `*-kiro-hooks.json` | Kiro only |
| Any other name (e.g. `hooks.json`, `telemetry-hooks.json`) | All targets |

Example directory tree for a multi-target hook package:

```
my-hooks-pkg/
  hooks/
    hooks.json              # deployed to all targets
    copilot-hooks.json      # Copilot only
    cursor-hooks.json       # Cursor only
    claude-hooks.json       # Claude Code only
    kiro-hooks.json         # Kiro only
```

APM automatically normalises event names per target (e.g. `postToolUse`
becomes `PostToolUse` in Claude) and rewrites path variables
(`${PLUGIN_ROOT}`, `${CURSOR_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_ROOT}`) to
the correct target-specific form. Kiro materializes one JSON document per
hook action under `.kiro/hooks/`.

### Hook command paths: project-scope stays repo-relative

`apm install` (project-scope, no `-g`) keeps hook `command` paths
**repo-relative** in checked-in configs (`<repo>/.claude/settings.json`,
`<repo>/.codex/hooks.json`, the `<repo>/.claude/apm-hooks.json`
sidecar, and equivalents for Cursor / Gemini / Antigravity / Windsurf / Kiro) so clones,
contributors, and CI runners do not see the installer's machine-local
absolute prefix. `apm install -g` (user-scope, e.g.
`~/.claude/settings.json`) rewrites `${PLUGIN_ROOT}` and relative `./`
references to absolute paths because the user-scope config is read
without a fixed cwd. If a referenced hook script is missing at install
time the installer emits a warning either way; user-scope additionally
rewrites the unexpanded variable to an absolute source path so the hook
fails loudly at runtime, while project-scope leaves the variable in
place to avoid baking the installer's prefix into committed config. To
clean up an older repo whose committed configs still carry absolutized
paths, re-run `apm install` -- the installer rewrites them back to
repo-relative.

## Manifest fields: `targets:` validation contract

Two keys control which output runtimes a package compiles and installs to:

- **`targets:` (canonical, plural list)** -- `targets: [claude, copilot]`.
- **`target:` (singular sugar)** -- `target: claude` or `target: "claude,copilot"` (CSV-string form).

Setting both keys in the same `apm.yml` is a parse error (`ConflictingTargetsError`); pick one. An empty `targets: []` is also a parse error -- omit the line if you mean auto-detect.

Both `apm.yml`'s `targets:`/`target:` and the `--target` CLI flag share the same validator, so identical input is rejected or accepted the same way at every entry point. Invalid values fail at parse time with a message naming the apm.yml path and the offending token -- they do **not** silently fall through to auto-detect.

| Form | Behaviour |
|------|-----------|
| `targets: [claude, copilot]` | Canonical list form; only listed targets are compiled/installed |
| `target: copilot` | Singular sugar; allowed values: `vscode`, `agents`, `copilot`, `claude`, `cursor`, `opencode`, `codex`, `gemini`, `antigravity`, `windsurf`, `kiro`, `all` |
| `target: claude,copilot` | CSV-string sugar; parses identically to the list form (the shared validator splits on `,`) |
| `targets:` and `target:` both set | **Parse error** -- pick one |
| `targets: []` (empty list) | **Parse error** -- remove the line if you meant auto-detect |
| `targets:`/`target:` omitted | Resolution falls through to auto-detect from filesystem signals (`.claude/`, `CLAUDE.md`, `.cursor/`, `.cursorrules`, `.github/copilot-instructions.md`, `.github/instructions/`, `.github/agents/`, `.github/prompts/`, `.github/hooks/`, `.codex/`, `.gemini/`, `GEMINI.md`, `.opencode/`, `.windsurf/`, `.kiro/`) |
| `target: bogus` (unknown token) | **Parse error** -- fix the typo |
| `target: [all, claude]` (`all` mixed with other targets) | **Parse error** -- use `all` alone |

Error messages always name the `apm.yml` path and the offending token, so the fix point is unambiguous. The list form (`targets: [a, b]`) is the recommended shape; the singular `target:` and CSV-string forms are supported indefinitely as sugar.

The package-authored `targets:`/`target:` field overrides auto-detect but is itself overridden by an explicit `--target` flag at install/compile time. Run `apm targets` in the consumer's directory to see what the resolution chain produces.

## Manifest fields: `license:` (declared license for SBOM)

`apm.yml` accepts an optional top-level `license:` field -- an SPDX
expression that *declares* the package's license:

```yaml
name: my-package
version: 1.0.0
license: MIT                 # or "(MIT OR Apache-2.0)", "Apache-2.0", ...
```

This mirrors how npm treats `package.json`'s `license`: it is an author
**claim**, not a conclusion drawn from the `LICENSE` file text. APM records
the declared value into the consumer's `apm.lock.yaml` (`declared_license`)
at resolve time and passes it through to `apm lock export` SBOMs. APM never
reads or interprets the `LICENSE` file -- declared is not concluded.

The value is syntax-validated **offline** against the bundled SPDX id set.
An unrecognized string (or a special token like `UNLICENSED` or
`SEE LICENSE IN <file>`) is **never** rejected -- it is recorded verbatim
and emitted in the SBOM as a named license. Authoring never blocks on a
license value.

If you omit `license:`, `apm pack` and `apm publish` print an actionable
warning (`No 'license:' field in apm.yml; the SBOM will record NOASSERTION
for this package. Add a 'license:' field ...`). The SBOM still exports
correctly -- the component just records NOASSERTION (genuinely unknown).
This warning fires only on the **authoring** path (your own `apm.yml`);
installing or exporting other people's dependencies is silent.


## The 7 primitive types

### 1. Instruction (`*.instructions.md`)

Contextual guidance scoped to file patterns.

```yaml
---
description: "Security best practices for Python"
applyTo: "**/*.py"
tags: [security, validation]
---
```

`applyTo` accepts a single glob (`"**/*.py"`) or a comma-separated list
(`"**/src/**,**/api/**"`). A YAML sequence (`applyTo: ['**/*.py']`) is also
accepted; when multiple sequence elements are given, the first is used.
Commas inside brace alternation (`**/*.{css,scss}`) are part of the glob
and are NOT separators -- only top-level commas split the list. On Copilot
the value is preserved verbatim; on Claude/Cursor/Windsurf/Kiro comma-lists are
expanded to a YAML array under `paths:` / `globs:` / `fileMatchPattern:`.

### 2. Chatmode (`*.chatmode.md`)

Chat persona configuration.

```yaml
---
name: "architect"
description: "System architecture expert"
system_prompt: "You are an expert..."
temperature: 0.7
---
```

### 3. Context (`*.context.md`)

Domain knowledge and background information.

```yaml
---
description: "Company coding standards"
applyTo: "**/*"
---
```

### 4. Prompt / Agent Workflow (`*.prompt.md`)

Executable workflows with parameters. Use the `input:` key to declare
parameters, and `${input:name}` to reference them in the prompt body.
Deployed as slash commands to targets that support them:

- Claude Code: `.claude/commands/*.md` (normalized to supported command frontmatter)
- Cursor: `.cursor/commands/*.md` (Cursor 1.6+; Cursor is de-emphasizing commands in favor of rules/skills)
- OpenCode: `.opencode/commands/*.md` (normalized to supported command frontmatter)
- Gemini CLI: `.gemini/commands/*.toml` (converted to TOML command format)

```yaml
---
description: "Code review workflow"
input:
  - pr_url
  - focus_areas
---
Review ${input:pr_url} focusing on ${input:focus_areas}.
```

When installed as a Claude Code slash command, APM maps `input:` to
Claude's `arguments:` frontmatter and converts `${input:name}` to `$name`
placeholders. An `argument-hint` is auto-generated unless one is already set.

#### Optional workflow frontmatter (GitHub Copilot App, experimental)

When the `copilot_app` experimental flag is enabled and the package is
installed with `apm install --target copilot-app` (project scope) or
`apm install --target copilot-app --global` (user scope), prompts that
carry workflow frontmatter -- any flat top-level key of `interval`,
`schedule_hour`, `schedule_day` -- are deployed as rows in the desktop
App's SQLite store at `~/.copilot/data.db`. ``mode``, ``model``, and
``reasoning_effort`` are optional fields on a workflow but do NOT mark
a plain prompt as a workflow (they overload with plain VSCode / Copilot
slash-command prompts); declare ``interval: manual`` to opt a no-schedule
prompt into the App.

```yaml
---
name: "Daily Digest"
interval: daily           # manual | hourly | daily | weekly
schedule_hour: 9          # 0-23 (UTC); ignored for manual / hourly
schedule_day: 1           # 0-6 (weekly only)
mode: interactive         # interactive | plan
model: claude-opus-4.7    # optional
reasoning_effort: high    # optional
---
```

Rows are always inserted with `enabled = 0`; the user opts in from the
App. A `.prompt.md` belongs to exactly ONE surface: workflow-frontmatter
prompts go ONLY to the App DB, plain prompts go ONLY to file-based
slash-command targets (`copilot`, `claude`, `cursor`, ...). Pointing a
plain prompt at `--target copilot-app` is a hard error with an
actionable diagnostic. `interval` is optional and defaults to `manual`
when any other execution-shape key is present, so a parameterised
prompt with no schedule still works as a manually-fired App workflow.
The App also defines an `autopilot` mode, but APM intentionally does
not accept it via this target -- a third-party package could otherwise
auto-run the moment the user enables the row. Users who want autopilot
can still set it themselves per-row from the App UI after install.

### 5. Agent (`*.agent.md`)

Agent persona and behavior definition.

```yaml
---
name: "code-reviewer"
description: "Reviews code for quality"
instructions: |
  Focus on:
  - Security
  - Performance
---
```

#### OpenCode target: frontmatter constraints

OpenCode (`target: opencode`, deploys to `.opencode/agents/`) parses
agent frontmatter through a strict Zod schema and refuses to load
the agent on any mismatch. APM installs OpenCode agents verbatim
and emits an install-time warning when it detects either of these
known incompatibilities -- the file is still copied so you can fix
it in place, but OpenCode will fail to start until you do.

- `tools:` must be a **mapping of tool-name to boolean**, not a list
  or comma-separated string:

  ```yaml
  # OK
  tools:
    Read: true
    Grep: true
    Edit: false

  # Rejected by OpenCode (Claude/Copilot-style):
  # tools: [Read, Grep]
  # tools: "Read, Grep"
  ```

- `color:` must be either a **hex value** (`#abc` or `#aabbcc`) or
  one of the OpenCode theme tokens: `primary`, `secondary`, `accent`,
  `success`, `warning`, `error`, `info`. Free-form names such as
  `cyan` or `magenta` are rejected:

  ```yaml
  # OK
  color: "#aabbcc"
  color: accent

  # Rejected by OpenCode:
  # color: cyan
  ```

If you target multiple agent runtimes from one source file, keep the
frontmatter to the intersection of their schemas (or maintain
target-specific copies) until APM ships a per-target frontmatter
transformer (tracked as Phase 2 of #581 -- contributions welcome).

### 6. Skill (folder-based, `SKILL.md`)

Reusable capability with supporting resources.

```
my-skill/
  SKILL.md                             # skill metadata and entry point
  resource1.md                         # supporting documentation
  resource2.md
```

### 7. Marketplace Plugin (`plugin.json`)

Packaged distribution format created with `apm pack --format plugin`.

When `apm.yml` declares `target: claude` or `target: copilot` (or the plural `targets:` equivalent), `apm pack` also generates an ecosystem-specific `plugin.json` automatically -- authors no longer need to maintain this file manually. The manifest is synthesised from `apm.yml` identity fields (`name`, `version`, `description`, `author`, `license`). See the apm pack reference (reference/cli/pack/#plugin-manifests) for output paths, credential stripping, and per-ecosystem differences, or run `apm pack --help`.

#### Shipping `bin/` executables (Claude Code only)

A marketplace plugin may ship a root `bin/` directory of executable
scripts. On `apm install`, APM deploys them under the Claude Code skills
directory as a skills-directory plugin (a folder containing
`.claude-plugin/plugin.json`), which puts `bin/` on Claude Code's Bash
tool PATH so the agent can invoke them as bare commands.

This is a **Claude-Code-specific** contract -- no other harness has an
equivalent, so `bin/` deploys only when an active Claude Code skills
target is present. Authoring rules:

- Place executables in a top-level `bin/` directory; APM marks them
  `0o755` on POSIX.
- Deploy is **user-scope only**. A project-scope install (`apm install`
  without `-g`) skips `bin/` and prints a hint to re-run with `-g`.
- Deployed executables land on Claude Code's PATH and are invoked
  **without per-call confirmation** -- treat them as trusted code and
  keep them minimal.
- Governance: a `bin_deploy` policy rule can deny deployment per package.
  See the [policy schema](../../../../../docs/src/content/docs/reference/policy-schema.md#bin_deploy).

## Canvas extensions (experimental, Copilot-only)

Behind the `canvas` experimental flag (`apm experimental enable canvas`), a
package may ship a GitHub Copilot CLI canvas extension. Place a directory bundle
under `.apm/extensions/<name>/` with an `extension.mjs` entry file (executable
Node.js) plus any sibling assets; a directory without `extension.mjs` is ignored.

On `apm install --target copilot`, APM deploys it verbatim to
`.github/extensions/<name>/`. The `<name>` segment is validated strictly
(`[A-Za-z0-9._-]+`, no leading/trailing dot, no `..`, no separators, no reserved
names). It is **Copilot-only**. Dependency-provided canvases are executable code
and are blocked unless the consumer passes `--trust-canvas-extensions`; a
first-party canvas in the root package deploys once the flag is on. With
`--global`, a dependency canvas deploys to `~/.copilot/extensions/<name>/`
(always requiring the trust flag; default `~/.copilot` only; first-party root
canvases are project-scope only). `apm pack` preserves `.apm/extensions/`. See
the [canvas integration guide](../../../../../docs/src/content/docs/integrations/canvas.md).

## Marketplace source bases

Marketplace publishers can declare `marketplace.sourceBase` when package
repositories share an enterprise git base path:

```yaml
marketplace:
  sourceBase: https://gitlab.corp.example.com/platform/agent-marketplace
  packages:
    - name: review
      source: review
      ref: v1.0.0
    - name: pinned
      source: team/pinned
      ref: main
```

Relative `packages[].source` values compose onto the base, including
`owner/repo` shapes like `team/pinned`. Host-prefixed sources, full HTTPS
URLs, and local `./` paths remain per-entry overrides. Without `sourceBase`,
existing `owner/repo` source behavior is unchanged. The manifest schema
Section 7.5 is canonical for the full validation and override rules.

The base may target any supported host -- GitHub.com, GitHub Enterprise,
self-hosted GitLab, or Azure DevOps. For Azure DevOps, use a
`https://dev.azure.com/{org}/{project}/_git` base; the `dev.azure.com` host is
preserved through to the consumer and authenticated with `ADO_APM_PAT`:

```yaml
marketplace:
  sourceBase: https://dev.azure.com/contoso/platform/_git
  packages:
    - name: agent-skills
      source: agent-skills          # -> contoso/platform/_git/agent-skills
      ref: 3f2a9b1c
```

## Step-by-step: create and publish

```bash
# 1. Initialize a package project
apm init my-package --plugin

# 2. Add primitives to .apm/ subdirectories
#    (instructions, agents, prompts, skills, etc.)

# 3. Test locally
apm install ./my-package               # install from local path
apm compile --verbose                  # verify compilation output

# 4. Validate
apm audit                              # check for security issues
apm audit --ci                         # run baseline CI checks

# 5. Publish
#    Push to a Git repository (GitHub, GitLab, ADO)
git init && git add . && git commit -m "Initial package"
git remote add origin git@github.com:org/my-package.git
git push -u origin main
git tag v1.0.0 && git push --tags

# 6. Consumers install via
apm install org/my-package#v1.0.0
```

## Publishing to a registry (experimental)

REST-based APM registries are an alternative distribution channel to Git
(and a separate surface from marketplaces). Use `apm publish` to push a
package version to a registry that implements the [Registry HTTP API](../../../../../docs/src/content/docs/reference/registry-http-api.md).

```bash
# 1. Enable the feature
apm experimental enable registries

# 2. Declare the target registry in apm.yml
cat >> apm.yml <<'EOF'
registries:
  corp-main:
    url: https://registry.example.com/apm/corp-main
EOF

# 3. Set a publish token (per-registry env var)
export APM_REGISTRY_TOKEN_CORP_MAIN=eyJ...

# 4. Preview then publish
apm publish --registry corp-main --dry-run -v
apm publish --registry corp-main
```

`apm publish` auto-packs a **flat registry archive** in the project root
(`{name}-{version}.tar.gz`) containing `apm.yml` and `.apm/` at the
tarball root. This layout differs from the plugin bundle that
`apm pack` produces (`{name}-{version}/plugin.json`). Auto-pack skips
macOS `._*` / `.DS_Store` sidecars.

Auto-pack requires:
- `apm.yml` with `name:` and `version:` (and `source:` when the registry
  identity differs from the package name)
- A `.apm/` directory with at least one primitive

Skill-only or custom layouts: build the tarball yourself and pass
`--tarball`:

```bash
tar czf my-skill-0.0.1.tar.gz apm.yml SKILL.md
apm publish --tarball my-skill-0.0.1.tar.gz --registry corp-main
```

Upload contract: `PUT /v1/packages/{owner}/{repo}/versions/{version}`.
Re-publishing an existing version returns `409 Conflict` (registry
versions are immutable) -- bump `version:` in `apm.yml` to publish again.

**Supported registries:** any backend that implements the
[Registry HTTP API](../../../../../docs/src/content/docs/reference/registry-http-api.md)
(JFrog Artifactory, custom services). GitHub / Git remotes are NOT
registries -- they remain the default Git resolver. APM marketplaces
(`apm pack` + `.claude-plugin/marketplace.json`) are a separate surface.

See `commands.md` for the `apm publish` command reference,
`authentication.md` for registry token resolution, and `governance.md`
for the `registry_source` policy field.

## Marketplace authoring

A **marketplace** is a curated index of plugins that consumers install via
`apm install <name>@<marketplace>`. Maintainers declare the marketplace in a
`marketplace:` block inside `apm.yml`; running `apm pack` builds an
Anthropic-compliant `.claude-plugin/marketplace.json`. Both files are committed.

### When to run `apm marketplace init`

- The user is setting up a new marketplace repository.
- The user wants to convert an ad-hoc list of plugins into a proper index.

`apm marketplace init` appends a `marketplace:` block to the project's
`apm.yml` and creates `.claude-plugin/`. It does NOT scaffold a standalone
`marketplace.yml`. Use `apm init --marketplace` when starting a brand-new
project that will publish its own marketplace.

### apm.yml `marketplace:` block

```yaml
name: my-project
version: 0.1.0
description: Short summary

marketplace:
  # name / description / version inherit from apm.yml top level
  # (omit unless you need to override).
  owner:
    name: acme-org
    url: https://github.com/acme-org
  versioning:                  # optional; used by `apm pack --check-versions`
    strategy: lockstep         # lockstep | tag_pattern | per_package
  build:                       # APM-only, stripped at compile time
    tagPattern: "v{version}"
  metadata:                    # pass-through, copied verbatim
    homepage: https://example.com
  plugins:
    - name: example-plugin
      description: What this plugin does
      source: acme-org/example-plugin    # owner/repo (remote)
      version: "^1.0.0"                  # semver range OR 'ref:' below
      # ref: 3f2a9b1c                    # explicit SHA/tag/branch
      # subdir: tools/x                  # optional subdirectory
      # tag_pattern: "{name}-v{version}" # optional per-plugin override
      # include_prerelease: false        # optional

    - name: local-tool
      description: Plugin shipped alongside this repo
      source: ./plugins/local-tool       # local path (no remote fetch)
      version: 0.1.0

    - name: enterprise-plugin
      description: Hosted on GitHub Enterprise
      source: ghe.corp.example.com/platform/agents   # host.tld/owner/repo
      version: "^0.3.0"
      # Equivalent full URL form (trailing .git is stripped):
      # source: https://ghe.corp.example.com/platform/agents.git
```

Schema rules:
- `owner.name` is required. `name`, `description`, `version` are
  optional inside the block (inherited from apm.yml top level).
- Each remote plugin needs either `version` or `ref`.
- `ref` takes precedence over `version`.
- `source: ./...` marks a local-path entry: skips git resolution,
  emits the path verbatim into `marketplace.json`.
- `source` accepts three remote forms: `owner/repo` (default host),
  `host.tld/owner/repo` (non-default host shorthand), or
  `https://host.tld/owner/repo[.git]` (full URL).  Non-default hosts
  resolve auth via the standard APM token chain
  (`docs/getting-started/authentication.md`); the default-host token is
  never forwarded.
- `versioning.strategy` is optional. When present, it is consumed by
  the `apm pack --check-versions` release gate to enforce alignment
  between each local package's `version:` field and the marketplace
  version: `lockstep` (all packages match `marketplace.version`),
  `tag_pattern` (each package renders a unique tag via `tagPattern`),
  or `per_package` (each package versions independently, gate only
  checks that `version:` is present). Omit entirely to skip the gate.
- Unknown keys raise a schema error -- do not invent fields.

### Cross-repo plugin sources on enterprise marketplaces

When a marketplace published on a `*.ghe.com` host references a plugin
in a different repo via the YAML mapping form of `source:` -- with
nested `type:` and `repo:` keys (rather than the simple `source: owner/repo`
string) -- the `repo:` field **must be host-qualified**. A bare
`owner/repo` value is refused at install time because it cannot be
disambiguated from a public-github.com dependency-confusion attempt
(see CHANGELOG entry for #1326). Two valid forms:

```yaml
plugins:
  - name: shared-tool
    source:
      type: github
      # Enterprise dep (most common): host-qualify to the marketplace host
      repo: corp.ghe.com/platform-team/shared-tool
      path: plugins/shared

  - name: opensource-helper
    source:
      type: github
      # Declared cross-host dep: host-qualify to github.com explicitly
      repo: github.com/opensource-org/helper
      path: plugins/helper
```

In-marketplace plugins (`source: ./...` or `source: owner/marketplace-repo`
when it matches the marketplace project) are unaffected -- the resolver
backfills the host automatically.

### Build semantics

`apm pack` runs `git ls-remote` against each remote plugin source, picks the
highest tag satisfying the range (under the applicable `tagPattern`), leaves
local-path entries untouched, and writes `.claude-plugin/marketplace.json`.
The compiler:

1. Emits `plugins:` verbatim (Anthropic's key name).
2. Copies `metadata:` byte-for-byte.
3. Strips `build:`, per-plugin `version`, `tag_pattern`, `include_prerelease`.
4. Omits empty `tags:` and inherited top-level `description`/`version`
   from the output (matches Anthropic's canonical hand-authored shape,
   e.g. microsoft/azure-skills).
5. Does not emit `versions[]` -- each plugin carries a single resolved ref.

`apm pack` also produces a bundle if `apm.yml` declares `dependencies:`. With
only a `marketplace:` block present, bundle flags (`--archive`, `-o`, `--format`,
`--target`, `--force`) are silent no-ops.

Marketplace-relevant flags on `apm pack`: `--dry-run`, `--offline`,
`--include-prerelease`, `--marketplace-path FORMAT=PATH`, `-v`.

Exit codes: `0` success, `1` build error, `2` schema error.

### Migrating from legacy `marketplace.yml`

Earlier APM versions stored this configuration in a standalone
`marketplace.yml`. That file is deprecated; `apm marketplace init` no longer
creates one. Run the one-shot migration:

```bash
apm marketplace migrate --dry-run    # preview the apm.yml change
apm marketplace migrate --yes        # apply: rewrite apm.yml, delete marketplace.yml
```

`--force`, `--yes`, and `-y` are equivalent. Both files present at once
is a hard error -- run `migrate` to consolidate.

### Full guide

See [docs/guides/marketplace-authoring](../../../../../docs/src/content/docs/guides/marketplace-authoring.md)
for the complete maintainer workflow (quickstart, version ranges, `check`,
`doctor`, and `outdated`).

## Org-wide packages

For organization-wide standards, create a single repository with shared
primitives and have all team repos depend on it:

```yaml
# In each team repo's apm.yml
dependencies:
  apm:
    - contoso/engineering-standards#v2.0.0
```

This ensures consistent instructions, agents, and policies across the org.
Local `.apm/` primitives in each repo can extend or override the shared ones
(local always takes priority over dependencies).
