# Dependency Reference

## String forms (in apm.yml `dependencies.apm`)

```yaml
dependencies:
  apm:
    # GitHub shorthand
    - microsoft/apm-sample-package
    - microsoft/apm-sample-package#v1.0.0       # pinned tag
    - microsoft/apm-sample-package#main          # branch
    - microsoft/apm-sample-package#abc123d       # commit SHA (7-40 hex)

    # HTTPS URLs (any git host)
    - https://github.com/microsoft/apm-sample-package.git
    - https://gitlab.com/acme/coding-standards.git

    # SSH URLs
    - git@github.com:microsoft/apm-sample-package.git
    - git@gitlab.com:group/subgroup/repo.git

    # SSH with non-default user (EMU accounts, servers where login != "git")
    - myuser@host.example.com:owner/repo.git
    - ssh://myuser@host.example.com/owner/repo.git

    # Custom ports (e.g. Bitbucket Datacenter, self-hosted GitLab)
    - ssh://git@bitbucket.example.com:7999/project/repo.git
    - https://git.internal:8443/team/repo.git

    # Bitbucket Data Center personal repos (~user) and Sourcehut
    - https://bitbucket.example.com/scm/~jdoe/ml-utils.git
    - https://git.sr.ht/~jdoe/dotfiles

    # FQDN shorthand (non-GitHub hosts keep the domain)
    - gitlab.com/acme/coding-standards
    - gitlab.com/group/subgroup/repo

    # Azure DevOps
    - dev.azure.com/org/project/_git/repo

    # Local paths (development only)
    - ./packages/my-shared-skills
    - ../sibling-repo/my-package
```

GitHub and package-registry owner/repository identifiers are normalized to
lowercase before APM derives lock keys, cache identity, canonical strings, or
`apm_modules/` paths. `Owner/Repo` and `owner/repo` therefore identify one
package. Repository path casing is preserved for unknown git hosts because a
self-hosted backend may be case-sensitive.

**Local-path anchor rule:** a `local_path` declared INSIDE another local
package is resolved relative to THAT package's own directory (npm/pip/cargo
parity). Sibling layouts that resolve outside the consuming project root
(e.g. `../sibling-pkg` from a local dep at the project edge) are
supported -- the consuming developer authored the manifest chain and
already trusts the layout.

Remote-cloned packages may declare a relative `path:` only when it resolves
inside the same authenticated remote repo root. APM expands that path to the
parent's remote host/repo/ref and fetches the sibling from the same origin.
Absolute paths, paths that escape the repo root, and cross-repo local paths
are rejected.

**GitLab `path:` fetch transport:** GitLab `path:` files are fetched over git
transport, not the REST API, so self-hosted instances with the API disabled
still install. Path containment is enforced on the materialized file to reject
symlink or traversal escapes. For fallback token setup, see `authentication.md`.

### Custom git ports

Non-default git ports are preserved on `https://`, `http://`, and `ssh://` URLs
and threaded through every clone attempt (including any cross-protocol
fallback enabled with `--allow-protocol-fallback`).

- Use the `ssh://` form to specify an SSH port
  (e.g. `ssh://git@host:7999/owner/repo.git`). The SCP shorthand
  `git@host:path` **cannot** carry a port -- the `:` is the path separator.
- A non-`git` SSH user is honored when present in the dep URL
  (e.g. `myuser@host:owner/repo.git` or `ssh://myuser@host/owner/repo.git`),
  useful for EMU accounts or servers where the SSH login is not `git`.
  Validated against `^[a-zA-Z0-9_][a-zA-Z0-9_.+-]*$` with a 64-char cap;
  percent-encoded userinfo is rejected. The user is presentation-only and
  not part of dependency identity (does not perturb lockfile dedup).
- The lockfile records `port: <int>` (1-65535) only when a non-default port
  is set. Port is a transport detail, not part of the package identity --
  the same repo reachable on different ports dedupes to one entry.

## Transport selection (SSH vs HTTPS)

Strict by default. Pick the transport up front; APM never silently retries
across protocols.

| Dependency form | What APM tries |
|-----------------|----------------|
| `ssh://...` or `git@host:...` | SSH only |
| `https://...` or `http://...` | HTTP(S) only |
| Shorthand with `git config url.<base>.insteadOf` rewriting to SSH | SSH only |
| Shorthand otherwise | HTTPS only |

A failed clone fails loudly, naming the URL and the protocol attempted.
Explicit URL schemes are honored exactly.
This includes in-repository plugins from GitLab and generic git marketplaces:
an SSH registration is persisted as SSH `git:` and `path:`; an HTTPS
registration remains HTTPS.

Force the initial protocol for shorthand:

```bash
apm install owner/repo --ssh           # SSH for shorthand
apm install owner/repo --https         # HTTPS for shorthand
export APM_GIT_PROTOCOL=ssh            # session default
```

`--ssh` and `--https` are mutually exclusive and apply only to shorthand.
URLs with an explicit scheme ignore them.

Match local `git clone` behavior by configuring `insteadOf` once:

```bash
git config --global url."git@github.com:".insteadOf "https://github.com/"
apm install owner/repo                 # APM clones over SSH
```

Restore the legacy permissive chain (escape hatch -- not a long-term
setting):

```bash
apm install --allow-protocol-fallback
export APM_ALLOW_PROTOCOL_FALLBACK=1   # CI / migration window
```

When fallback runs, each cross-protocol retry emits a `[!]` warning naming
both protocols.

## Object form (complex cases)

Use the object form when the string shorthand cannot express what you need:
nested-group repos with virtual paths, custom SSH ports, local path deps,
aliases, or marketplace dependencies.

Three mutually exclusive keys select the form: `git`, `path`, or `marketplace`.

The legacy string suffix `@alias` is not supported; write `alias:` explicitly
instead so `@` remains reserved for git usernames and version syntax.

### Remote (`git`)

| Field | Required | Description |
|-------|----------|-------------|
| `git` | REQUIRED | Clone URL (HTTPS, SSH, or FQDN shorthand). The literal `parent` inherits the consuming package's repo. |
| `path` | OPTIONAL | Subdirectory or file within the repo (virtual package). |
| `ref` | OPTIONAL | Branch, tag, or commit SHA. |
| `alias` | OPTIONAL | Install under a custom directory name (`^[a-zA-Z0-9._-]+$`). |
| `type` | OPTIONAL | Set to `gitlab` for self-managed GitLab on a bespoke hostname. Generic hosts do not receive APM-managed PATs on HTTP file reads. See the [lockfile spec](https://microsoft.github.io/apm/reference/lockfile-spec/#lockfile-identity-keys) for keying rules. |
| `targets` | OPTIONAL | Consumer-side harness subset for that dependency's target-scoped primitives. Non-empty list of target names. |

```yaml
- git: https://gitlab.com/acme/repo.git
  path: instructions/security                   # virtual sub-path
  ref: v2.0                                     # tag, branch, or SHA
  alias: acme-sec                               # local alias

- git: git@gitlab.com:group/subgroup/repo.git
  path: prompts/review.prompt.md

- git: ssh://git@bitbucket.example.com:7999/project/repo.git   # custom SSH port
  ref: v1.0

- git: https://code.acme.com/platform/standards.git               # bespoke GitLab
  type: gitlab
```

### Local (`path`)

| Field | Required | Description |
|-------|----------|-------------|
| `path` | REQUIRED | Filesystem path (must start with `./`, `../`, `/`, or `~/`). |
| `alias` | OPTIONAL | Install under a custom directory name (`^[a-zA-Z0-9._-]+$`). |
| `skills` | OPTIONAL | Consumer-side skill subset for that dependency. Non-empty list of skill names. |
| `targets` | OPTIONAL | Consumer-side harness subset for that dependency's target-scoped primitives. Non-empty list of target names. |

Local-path deps inside another local package resolve relative to that
package's directory, not the project root.

```yaml
- path: ./packages/my-skills

- path: ./packages/local-review-kit
  alias: local-review-kit
  skills: [reviewer]
  targets: [claude]
```

### Marketplace (`name` + `marketplace`)

| Field | Required | Description |
|-------|----------|-------------|
| `name` | REQUIRED | Plugin identifier within the marketplace (`^[a-zA-Z0-9._-]+$`). |
| `marketplace` | REQUIRED | Registered marketplace name (`^[a-zA-Z0-9._-]+$`). |
| `version` | OPTIONAL | Semver range or exact version (e.g. `~2.1.0`, `^2.0`, `>=1.4`, `2.1.0`). Resolved against `{name}--v{version}` git tags on the marketplace repo. |

During resolution, marketplace entries are looked up in the marketplace's
`marketplace.json` and replaced with concrete git coordinates. When `version`
is a semver range or bare version number, the resolver lists git tags
matching `{name}--v{version}`, filters by the constraint, and picks the
highest matching tag. Raw git refs (e.g. `v2.0.0`, `main`) bypass tag
resolution and override the source ref directly. The lockfile records the
resolved ref, not the marketplace placeholder. Unknown keys in a marketplace
entry are rejected.

If the marketplace plugin entry declares `registry`, APM creates a
registry-sourced dependency instead of Git coordinates. Enable registry support
with `apm experimental enable registries` and configure the named registry.
The entry must also declare a valid semver `version`; malformed or unresolvable
registry intent fails closed and never falls back to Git.

```yaml
- name: sec-check
  marketplace: acme-plugins

- name: secrets-vault
  marketplace: acme-plugins
  version: "~2.1.0"
```

## Registry-sourced APM dependencies (experimental)

Behind `apm experimental enable registries`. Registry deps resolve over the
REST [Registry HTTP API](../../../../../docs/src/content/docs/reference/registry-http-api.md)
alongside the Git resolver -- declare registries in `apm.yml` (or in
`~/.apm/config.json`) and reference them from `dependencies.apm`. See
`authentication.md` (Registry tokens) for `APM_REGISTRY_TOKEN_{NAME}`.

```yaml
registries:
  jf-skills:
    url: https://registry.example.com/apm/jf-skills
  default: jf-skills                       # optional; routes shorthand deps

dependencies:
  apm:
    # String shorthand -- requires a default registry; always needs a semver ref
    - acme/foo#^1.2.3                      # semver range -> default registry
    - acme/bar#1.4.0                       # exact semver -> default registry

    # Object form -- whole package via the default registry
    - id: acme/toolkit
      version: ^2.0.0

    # Object form -- explicit registry by name
    - registry: jf-skills
      id: acme/toolkit
      version: ^2.0.0

    # Object form -- virtual package (sub-path inside a published package)
    - registry: jf-skills
      id: acme/prompt-pack
      path: prompts/review.prompt.md
      version: 1.4.0
      alias: review-prompt                 # optional local alias
```

Object-form fields:

| Field | Required | Notes |
|-------|----------|-------|
| `id` | yes | `owner/repo` identity at the registry |
| `version` | yes | Exact semver version or semver range (e.g. `1.4.0`, `^2.0.0`, `>=1.2.0 <2.0.0`). Non-semver refs (labels like `stable`/`latest`, `v`-prefixed tags, branch names, SHAs) are rejected when routed to a registry |
| `registry` | no | Name from the merged registry map; defaults to the effective default |
| `path` | no | Sub-path to a file or directory within the published package |
| `alias` | no | Local alias controlling the install directory name |

Routing rules when a default registry is active:

| Entry form | Routed to |
|------------|-----------|
| `owner/repo#<any-ref>` | Default registry |
| `- id:` object (no `registry:`) | Default registry |
| `- registry:` object | Named registry |
| `- git:` object | Git (explicit override) |
| `- path:` object | Local filesystem |

A shorthand entry with no ref (`acme/foo`) is **rejected** when routed to a
registry -- a semver version selector is always required. Non-semver refs
(labels, `v`-prefixed tags, branch names) are also rejected for registry
sources; use `- git:` to keep a dep on Git when a default registry is
active. Registry-routed deps add `source: registry`, `version`,
`resolved_url`, and `resolved_hash` (sha256 of the archive bytes) to
their lockfile entry, and the lockfile is promoted to
`lockfile_version: "2"` when any dep is registry-sourced OR carries
git-source semver resolution fields (`constraint` / `resolved_at`) or a
revision-pin tag annotation (`resolved_tag`).

The `acme/foo@registry-name#version` shorthand is **not supported** (deferred
to v2) -- the `@` collides with npm/cargo/pip version syntax, with
`git@host`, and with marketplace plugin shorthand. Use the object form.

## Virtual package types

Virtual packages reference a subset of a repository.

| Type | Detection rule | Example |
|------|---------------|---------|
| File | Ends in `.prompt.md`, `.instructions.md`, or `.agent.md` | `owner/repo/prompts/review.prompt.md` |
| Subdirectory | Does not match a file extension above | `owner/repo/skills/security` |

Classification is by extension only. A path like `owner/repo/collections/security` (no extension) is a Subdirectory; the actual shape -- APM package (incl. dep-only `apm.yml` with no `.apm/`), skill bundle, or plugin -- is resolved at fetch time by probing for `apm.yml`.

**Gitea and Gogs (self-hosted or vendor-hosted):** virtual packages resolve via the host's `/{owner}/{repo}/raw/{ref}/{path}` URL first, then fall back to the Contents API (v1 native, v3 Gogs-compat). Direct GitLab nested-group repos (`group/subgroup/repo`) require the object form (`git: <full-url>`, `path: <virtual>`) -- shorthand is ambiguous on >2-segment paths. **Exception:** when the dep routes through a registry proxy (explicit `host/artifactory/<key>/...` FQDN, or bare shorthand under `PROXY_REGISTRY_URL` + `PROXY_REGISTRY_ONLY=1`), the install-time boundary probe HEAD-walks the candidate splits against the proxy and locks in the first one whose archive responds, so nested-group shorthand works without the object form (#1472).

> **Removed (#1094):** the legacy `.collection.yml` / `.collection.yaml` virtual-package form is no longer supported. Convert any `.collection.yml` to an `apm.yml` with a `dependencies:` section, then reference the resulting subdirectory as a regular subdirectory virtual package.

## Canonical storage rules

APM normalizes dependency strings when saving to apm.yml:

| Input | Stored as |
|-------|-----------|
| `microsoft/apm-sample-package` | `microsoft/apm-sample-package` |
| `https://github.com/microsoft/apm-sample-package.git` | `microsoft/apm-sample-package` |
| `git@github.com:microsoft/apm-sample-package.git` | `microsoft/apm-sample-package` |
| `https://gitlab.com/acme/rules.git` | `gitlab.com/acme/rules` |
| Object with `git` + `path: docs` + `ref: main` | `org/repo/docs#main` |
| `./packages/my-skills` | `./packages/my-skills` |

GitHub URLs are stripped to shorthand; non-GitHub hosts keep the FQDN.

## Per-dependency target selection (`targets:`)

`targets:` is an optional per-dependency list on the object form of a
`dependencies.apm` entry. It restricts which active install targets
receive that dependency's target-scoped primitives.

Package-level `targets:` (top-level) selects the package's own
compile/install runtimes; per-dependency `targets:` (inside a
`dependencies.apm` entry) selects which active harnesses receive that
dependency's target-scoped primitives. They compose via intersection. See
`package-authoring.md` for author guidance.

- Type: list of harness keys (`copilot`, `claude`, `cursor`, `codex`,
  `gemini`, `antigravity`, `windsurf`, `kiro`, plus canonical targets
  such as `opencode`, `agent-skills`, `openclaw`, `hermes`,
  `copilot-cowork`, and `copilot-app`). Use `copilot`, not the runtime
  alias `vscode`, for Copilot-family dependency routing.
- Default: omitted means all active install targets.
- Semantics: effective reach is `install_targets INTERSECT dep_targets`.
  A non-empty list narrows reach; it never widens beyond what the install
  resolved. An empty list is a parse error; remove the field to mean
  "all".
- Supported on: both `git:` and `path:` dependency forms.

```yaml
dependencies:
  apm:
    - git: owner/codex-hooks
      targets: [codex]

    - git: owner/universal-hooks
      # no targets: all active install targets

    - path: ./skills/my-local-skill
      targets: [claude]         # local deps support targets: too
```

The lockfile records `target_subset` for audit/display only. Install
routing always recomputes from the live `apm.yml` entry, so editing the
lockfile cannot widen a dependency's reach. The formal schema contract
lives in `docs/src/content/docs/reference/manifest-schema.md` section
4.1.2.

## MCP dependency formats

See also: [MCP Servers guide](../../../../../docs/src/content/docs/guides/mcp-servers.md) for the CLI-first `apm install --mcp` workflow.

```yaml
dependencies:
  mcp:
    # Registry reference (string)
    - io.github.github/github-mcp-server

    # Registry with overlays (object)
    - name: io.github.github/github-mcp-server
      transport: stdio                          # stdio|sse|http|streamable-http (MCP transport names, not URL schemes; remote connects over HTTPS)
      env:
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      args: ["--port", "3000"]
      version: "1.5.0"
      package: npm                              # npm|pypi|oci
      headers:
        X-Custom: "value"
        # Env-var placeholders in headers/env values:
        #   ${VAR} or ${env:VAR}  -> Copilot CLI: preserved as ${VAR} and resolved
        #                            from host env at server-start (no plaintext on disk).
        #                            VS Code and JetBrains: rewritten to ${env:VAR}
        #                            and resolved at runtime.
        #                            Kiro: preserved as ${VAR} and resolved at runtime.
        #                            Cursor/Windsurf/OpenCode/Claude/Gemini: resolved at install time.
        #                            Codex: resolved at install time.
        #   ${input:<id>}         -> VS Code prompts user at runtime
        #   <VAR>                 -> deprecated; auto-translated, emits a warning
        # Registry-declared optional env/input fields are omitted when unset;
        # see manifest-schema section 4.2.4 for reinstall preservation semantics.
        Authorization: "Bearer ${MY_TOKEN}"
      tools: ["repos", "issues"]

    # Self-defined server (not in registry)
    - name: my-private-server
      registry: false
      transport: stdio
      command: ./bin/my-server
      args: ["--port", "3000"]
      env:
        API_KEY: ${{ secrets.KEY }}

    # Self-defined HTTP server
    - name: internal-kb
      registry: false
      transport: http
      url: "https://mcp.internal.example.com"

    # Self-defined remote with harness-specific extra keys
    # Unknown keys (e.g. oauth) are passthrough: preserved and written into
    # the generated config for EVERY installed harness. Keys that collide with
    # a modeled field (command/url/headers/env/...) are rejected with a warning.
    - name: slack
      registry: false
      transport: http
      url: https://mcp.slack.com/mcp
      oauth:
        clientId: "<pre-registered-client-id>"
        callbackPort: 3118
```

At user scope, Claude MCP entries are written to
`$CLAUDE_CONFIG_DIR/.claude.json` when `CLAUDE_CONFIG_DIR` is set to a
non-whitespace absolute path. Unset or blank values use `~/.claude.json`;
relative values are rejected.

## LSP dependency formats

LSP (Language Server Protocol) servers give supported runtimes real-time
code intelligence. APM currently writes LSP config for Claude Code and
GitHub Copilot CLI while keeping the dependency schema runtime-neutral.

```yaml
dependencies:
  lsp:
    # String reference (name only)
    - gopls

    # Full object
    - name: pyright
      command: pyright-langserver
      args: ["--stdio"]
      extensionToLanguage:
        ".py": python
        ".pyi": python
      transport: stdio                          # stdio (default) | socket
      env:
        PYTHONPATH: "./src"
      startupTimeout: 10000

    - name: rust-analyzer
      command: rust-analyzer
      extensionToLanguage:
        ".rs": rust
      restartOnCrash: true
      maxRestarts: 3
```

Required fields (object form): `name`, `command`, `extensionToLanguage`.

Optional fields: `args`, `transport`, `env`, `initializationOptions`,
`settings`, `workspaceFolder`, `startupTimeout`, `shutdownTimeout`,
`restartOnCrash`, `maxRestarts`.

`apm install` writes LSP config to the detected runtime targets:
Claude Code uses `.lsp.json` or `~/.claude.json`, and GitHub Copilot CLI
uses `.github/lsp.json` or `~/.copilot/lsp-config.json`. Copilot CLI
uses `fileExtensions` on disk; manifests continue to use
`extensionToLanguage`. Plugin `.lsp.json` files may use either a flat
server map or a `{ "lspServers": { ... } }` envelope.

## Version pinning

| Strategy | Syntax | When to use |
|----------|--------|-------------|
| Tag | `owner/repo#v1.0.0` | Production -- immutable reference |
| Semver range | `owner/repo#^1.2.0` | Track patch/minor updates within a range; APM lists remote tags and pins the highest match in the lockfile |
| Branch | `owner/repo#main` | Development -- tracks latest |
| Commit SHA | `owner/repo#abc123d` | Maximum reproducibility; `apm update` can move full 40-character SHA pins to the latest annotated semver tag SHA and annotate the line with `# <tag>` |
| No ref | `owner/repo` | Resolves default branch at install time |
| Marketplace ref | `plugin@marketplace#ref` | Override marketplace source ref |

Semver ranges accept `^1.2.0`, `~1.4`, `>=2.0 <3`, or `1.5.x`. At
install time APM runs `git ls-remote` against the dep and picks the
highest tag matching the range; the resolved tag, commit SHA, version,
and original constraint are pinned in the lockfile. Subsequent
`apm install` runs replay the lockfile without network. Use
`apm install --update` (or change the manifest constraint) to
re-resolve against current remote tags. Tag patterns are tried in order:
`v{version}`, `{name}--v{version}`, and `{name}-v{version}`, then a bare
`{version}` fallback. For virtual subdirectory deps, `{name}` is the
final path segment (for example `pkg-a` in `acme/mono/packages/pkg-a`). A
malformed range-like ref is rejected; use a plain range such as `^1.2.0`
or pin a literal tag such as `pkg-a-v1.2.0`.

## Marketplace ref override

When installing from a marketplace, the `#` suffix overrides the `source.ref` from the marketplace entry:

| Syntax | Meaning | Example |
|--------|---------|---------|
| `plugin@mkt` | Use marketplace source ref | `plugin@mkt` |
| `plugin@mkt#v2.0.0` | Override with specific tag | `plugin@mkt#v2.0.0` |
| `plugin@mkt#main` | Override with branch | `plugin@mkt#main` |
| `plugin@mkt#abc123d` | Override with commit SHA | `plugin@mkt#abc123d` |

## HTTP dependencies (opt-in)

HTTP is never attempted implicitly. A dep fetched over `http://` requires
dual opt-in on every install:

1. **Manifest approval** -- the apm.yml entry carries `allow_insecure: true`.
2. **Invocation approval** -- `apm install --allow-insecure` for direct
   deps, or `--allow-insecure-host HOSTNAME` (repeatable) for transitive
   deps. Transitive HTTP deps from hosts not listed are blocked.

Example apm.yml entry:

```yaml
dependencies:
  apm:
    - git: http://mirror.example.com/acme/rules.git
      ref: v1.2.0
      allow_insecure: true
```

Example invocation:

```bash
apm install --allow-insecure --allow-insecure-host mirror.example.com
```

Mental model: HTTP is opt-in per-dep AND per-invocation. Removing either
side re-locks the dependency. The lockfile records `is_insecure: true` and
`allow_insecure: true` on the entry so replays fail-closed when either
approval is dropped. See `commands.md` for full flag syntax and the
enterprise security guide for the threat model.

## What the lockfile pins

`apm.lock.yaml` records the exact commit SHA for every dependency, regardless
of the ref format in apm.yml. Running `apm install` without `--update` always
uses the locked SHA, ensuring reproducible installs across machines.

Lockfile keys keep `github.com` implicit for migration stability while
non-default hosts add the lowercased host segment. See the [lockfile spec](https://microsoft.github.io/apm/reference/lockfile-spec/#lockfile-identity-keys)
for the full keying rules.

Each dependency entry can also record the package-declared `name`. For
git/local deps, `version` is read from the dependency's own `apm.yml` at
resolution time. These package-declared values are **SELF-ASSERTED** author
claims useful for inventory and audit -- they are NOT integrity-verified and
MUST NOT be used for trust decisions. Always cross-reference `repo_url` +
`resolved_commit` (or `resolved_hash`) for provenance. For registry deps,
`version` is the locked registry selection used for reinstall, with
`resolved_hash` as the integrity anchor; for git/local deps it is display
metadata only.
