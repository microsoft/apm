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
and threaded through all clone attempts. When the SSH clone fails, the HTTPS
fallback reuses the same port instead of silently dropping it.

- Use the `ssh://` form to specify an SSH port
  (e.g. `ssh://git@host:7999/owner/repo.git`). The SCP shorthand
  `git@host:path` **cannot** carry a port -- the `:` is the path separator.
- The lockfile records `port: <int>` (1-65535) only when a non-default port
  is set. Port is a transport detail, not part of the package identity --
  the same repo reachable on different ports dedupes to one entry.

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

```yaml
dependencies:
  mcp:
    # Registry reference (string)
    - io.github.github/github-mcp-server

    # Registry with overlays (object)
    - name: io.github.github/github-mcp-server
      transport: stdio                          # stdio|sse|http|streamable-http
      env:
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      args: ["--port", "3000"]
      version: "1.5.0"
      package: npm                              # npm|pypi|oci
      headers:
        X-Custom: "value"
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

## What the lockfile pins

`apm.lock.yaml` records the exact commit SHA for every dependency, regardless
of the ref format in apm.yml. Running `apm install` without `--update` always
uses the locked SHA, ensuring reproducible installs across machines.
