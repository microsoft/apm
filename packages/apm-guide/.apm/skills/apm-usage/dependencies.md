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

    # Custom ports (e.g. Bitbucket Datacenter, self-hosted GitLab)
    - ssh://git@bitbucket.example.com:7999/project/repo.git
    - https://git.internal:8443/team/repo.git

    # FQDN shorthand (non-GitHub hosts keep the domain)
    - gitlab.com/acme/coding-standards
    - gitlab.com/group/subgroup/repo

    # Azure DevOps
    - dev.azure.com/org/project/_git/repo

    # Local paths (development only)
    - ./packages/my-shared-skills
    - ../sibling-repo/my-package
```

### Custom git ports

Non-default git ports are preserved on `https://`, `http://`, and `ssh://` URLs
and threaded through every clone attempt (including any cross-protocol
fallback enabled with `--allow-protocol-fallback`).

- Use the `ssh://` form to specify an SSH port
  (e.g. `ssh://git@host:7999/owner/repo.git`). The SCP shorthand
  `git@host:path` **cannot** carry a port -- the `:` is the path separator.
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

```yaml
- git: https://gitlab.com/acme/repo.git
  path: instructions/security                   # virtual sub-path
  ref: v2.0                                     # tag, branch, or SHA
  alias: acme-sec                               # local alias

- git: git@gitlab.com:group/subgroup/repo.git
  path: prompts/review.prompt.md

- git: ssh://git@bitbucket.example.com:7999/project/repo.git   # custom SSH port
  ref: v1.0

- path: ./packages/my-skills                    # local only
```

## Virtual package types

Virtual packages reference a subset of a repository.

| Type | Detection rule | Example |
|------|---------------|---------|
| File | Ends in `.prompt.md`, `.instructions.md`, `.agent.md`, `.chatmode.md` | `owner/repo/prompts/review.prompt.md` |
| Collection (dir) | Contains `/collections/` (no extension) | `owner/repo/collections/security` |
| Collection (manifest) | Contains `/collections/` + `.collection.yml` | `owner/repo/collections/security.collection.yml` |
| Subdirectory | Does not match file or collection rules | `owner/repo/skills/security` |

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
        #   ${VAR} or ${env:VAR}  -> resolved from host env at install time
        #                            by Copilot (VS Code resolves at runtime;
        #                            Codex passes ${...} through unchanged)
        #   ${input:<id>}         -> VS Code prompts user at runtime
        #   <VAR>                 -> legacy Copilot syntax (still supported)
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
```

## Version pinning

| Strategy | Syntax | When to use |
|----------|--------|-------------|
| Tag | `owner/repo#v1.0.0` | Production -- immutable reference |
| Branch | `owner/repo#main` | Development -- tracks latest |
| Commit SHA | `owner/repo#abc123d` | Maximum reproducibility |
| No ref | `owner/repo` | Resolves default branch at install time |
| Marketplace ref | `plugin@marketplace#ref` | Override marketplace source ref |

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
