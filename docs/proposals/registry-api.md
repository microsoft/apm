# Proposal: Dedicated Registry API Resolver

**Status:** Draft — for APM maintainer review
**Scope:** Plugins, skills, prompts, agents, instructions, hooks, commands, chatmodes
**Explicitly out of scope:** MCP (continues to use the existing MCP registry client unchanged)

> **Hard requirement — complete backwards compatibility.** This proposal MUST NOT break any existing flow. Every current `apm.yml`, `apm.lock`, CLI invocation, environment variable, and integration path must continue to work byte-for-byte on a client that has this feature shipped but sees no registry configuration. See §2.1 for the explicit invariants and §13 for the compatibility test matrix.

---

## 1. Motivation

The current APM resolver is Git-based: dependencies are cloned from GitHub/GitLab/Azure DevOps (optionally through a VCS proxy such as an Artifactory or Nexus GitHub remote). This works well for open ecosystems but struggles at enterprise scale:

- **Versioning** relies on Git tags and per-repo semver discipline; resolution requires `git ls-remote` per dep.
- **Graph resolution** is client-side and recursive — every transitive dep is a network round-trip and a clone.
- **Search** is limited to marketplace `marketplace.json` files and is client-side.
- **Curated security** (promotion, signing, revocation) has no first-class primitive.

This proposal introduces a **Dedicated Registry API** — an additive resolver mode that speaks a predictable REST contract. It is **opt-in per-package** with a **default override**, runs **alongside** the existing Git resolver, and maps cleanly onto enterprise artifact registries (Artifactory, Nexus, etc.) as a new package type.

**Guiding principle:** We don't shake the system. `apm.yml` remains the same with a single optional field; all current flows keep working; registry mode is purely additive.

---

## 2. Goals & Non-Goals

### Goals
- **Complete backwards compatibility** (hard requirement — see §2.1).
- Provide a REST contract APM clients can implement against any registry vendor.
- Preserve the current `DependencyReference` identity (`owner/repo`) so apm.yml is backward-compatible.
- Make the mode opt-in per-dep with a global default override.
- Cover install, publish, and search flows end-to-end.
- Keep the lockfile honest about what was resolved (git commit vs. content hash).

### Non-Goals (v1)
- MCP servers (stay on the existing MCP registry client).
- Signing / provenance attestations (deferred — leave a `signatures` extension point).
- Replacing the Git resolver. The Git resolver remains the default.
- Mirroring / cross-registry federation.

### 2.1 Backwards Compatibility Invariants

These are **hard requirements**. Any implementation PR that violates one of them must be rejected.

1. **Zero-config parity.** A user who upgrades the APM CLI but does not configure any registry MUST observe identical behavior to the previous version. No new network calls, no new files written, no new prompts, no new warnings.
2. **apm.yml stability.** Every valid pre-proposal `apm.yml` remains valid and produces the same install result. The new top-level `registries:` block and the `@<name>` registry-scope suffix on dep strings are strictly optional and absent by default.
3. **Default source is `git`.** In the absence of `registries.default` (see §3.2), every string-shorthand entry and every `- git:` / `- path:` object entry routes through the existing Git or local resolver — unchanged. Projects that want the registry as the default for string shorthand set `registries.default: <name>`; individual deps can always pin to Git with the explicit `- git:` form.
4. **apm.lock stability.**
   - An existing v1 lockfile MUST continue to parse without migration on read.
   - A v1 lockfile MUST remain v1 on rewrite if no registry-sourced deps are present. The client only bumps to v2 when it actually needs to record a registry dep.
   - A v2 lockfile authored by a newer client MUST remain readable by clients that understand v2, with every v1 field semantically identical.
5. **No CLI signature changes.** `apm install`, `apm uninstall`, `apm prune`, `apm pack`, `apm unpack`, `apm update`, `apm marketplace *` retain every existing flag, argument, and exit code. New flags are additive only.
6. **No env-var renames or semantics changes.** `GITHUB_TOKEN`, `GITHUB_APM_PAT_*`, `PROXY_REGISTRY_*`, `ARTIFACTORY_APM_TOKEN` (deprecated alias) all behave exactly as today. New registry env vars use a distinct `APM_REGISTRY_*` prefix so they cannot collide.
7. **No integrator changes.** Deployed file layout under `.github/`, `.claude/`, etc. is byte-identical regardless of resolver source. `AgentIntegrator`, `SkillIntegrator`, `PromptIntegrator`, and peers receive no changes.
8. **No identity changes.** `DependencyReference.get_identity()` returns the same string for the same input across both modes. Registry-sourced deps share the same key space as git-sourced deps.
9. **Drift detection compatibility.** Existing drift rules (ref drift, orphan drift, stale-file drift from #666 / #750 / #762) remain unchanged for git-sourced deps. Registry-sourced deps use additional rules that coexist, never replace.
10. **Marketplace unchanged.** The existing `marketplace.json` client-side search path is untouched. Registry server-side search is added alongside, not in place of.
11. **Feature flag opt-in during rollout.** The registry code path is guarded by `APM_ENABLE_REGISTRY=1` (or presence of a top-level `registries:` block in apm.yml) during initial rollout, so `main` can merge registry work without any possibility of disturbing existing users. Flag is removed only after §13 test matrix passes in CI for at least one release cycle.
12. **MCP untouched.** The MCP registry client (`src/apm_cli/registry/client.py` — `SimpleRegistryClient`) and MCP resolution flow are not modified. Namespacing: the new APM registry client lives under `src/apm_cli/deps/registry/` to avoid even the appearance of conflict.

---

## 3. Opt-in Model

### 3.1 Registry declarations

Named registries are declared in a new top-level `registries:` block in `apm.yml`, committed so every contributor sees the same name→URL mapping:

```yaml
# apm.yml (committed)
name: my-project
version: 1.0.0

registries:
  corp-main:
    url: https://registry.corp.example.com/apm   # e.g. /artifactory/api/apm/{repo-key} or equivalent
  default: corp-main                              # omit to keep Git as the default

dependencies:
  apm:
    - acme/web-skills#^1.2
```

Per-user **auth** is one new env var, `APM_REGISTRY_TOKEN_{NAME}` (uppercased registry name) — never committed, never in any config file.

```
APM_REGISTRY_TOKEN_CORP_MAIN=<token>
```

**Why split URL from auth:** URL is project-level (same for all contributors), auth is user-level (tokens differ per user). Same split as npm's `.npmrc` vs. auth token. **Why convention over config:** no new config field, no indirection — the registry name deterministically maps to one env var name. Users can additionally define their own named registries in `~/.apm/config.yml`; those names are user-local and never leak into the lockfile.

### 3.2 How an entry routes to a resolver

Every dep entry in `dependencies.apm:` picks a resolver mechanically:

| entry shape | resolver |
|---|---|
| `- git: <url>` ... | **Git** (explicit; unchanged) |
| `- path: ./local` | **Local** (explicit; unchanged) |
| `- registry: <name>` ... (object form) | **Registry `<name>`** — required only for virtual packages (sub-path) |
| `- owner/repo@<name>#<ref>` (string shorthand) | **Registry `<name>`** (named scope — reuses the marketplace `@scope` convention) |
| `- owner/repo#<ref>` (string shorthand) | **Default registry** if `registries.default` is set, else **Git** (unchanged) |

The `@<name>` suffix is the existing marketplace-scope syntax (`apm install code-review@acme-plugins`, `apm search "linting@awesome-copilot"`) applied to registries — same vocabulary, same mental model. It does not collide with marketplace parsing: [`parse_marketplace_ref`](src/apm_cli/marketplace/resolver.py:26) explicitly rejects strings containing `/`, so `owner/repo@<name>` falls through to the dep parser.

```yaml
dependencies:
  apm:
    # Default path — string shorthand, routes through registries.default
    - acme/web-skills#^1.2

    # Named non-default registry (inline, via @scope)
    - acme/foo@corp-other#^3.0

    # Virtual package — object form (string shorthand can't express a sub-path cleanly)
    - registry: corp-main
      id: acme/prompt-pack
      path: prompts/review.prompt.md
      version: 1.4.0

    # Explicit Git override (works whether or not a default registry is set)
    - git: https://github.com/acme/core.git
      ref: v2.0
```

**Why object form for virtual only.** Non-virtual registry entries compose cleanly in shorthand. Virtual packages need four independent fields (package id, registry name, sub-path, version) that don't combine into a readable string — cramming them in (`acme/prompt-pack@corp-main/prompts/review.prompt.md#1.4.0`) buries the sub-path between two other separators. Object form exists for exactly this case, symmetric with how `- git:` object form exists for the cases string shorthand can't handle (custom URLs, aliases, virtual Git sub-paths). Registry virtual packages are a Git-era workaround we keep for symmetry — dropping them would create an asymmetry between Git-sourced and registry-sourced consumers (see §11 decision).

**Object-form fields (registry virtual packages):**
- `registry:` — required; name of the registry (presence of this key is the object-form discriminator)
- `id:` — required; `owner/repo`
- `path:` — required; virtual sub-path inside the package
- `version:` — required; semver version or range (same grammar as §3.3)
- `alias:` — optional; unchanged meaning

### 3.3 Semver constraint on registry-routed entries

Whenever a string-shorthand entry routes to a registry (either via `registries.default` or via `@<name>`), the ref portion (`#<ref>`) **must** be a valid semver version or range. The parser rejects anything else at load time:

- Accepted: `1.0.0`, `v1.0.0`, `^1.0`, `~1.2.3`, `>=1,<2`, `1.x`
- Rejected: `main`, `develop` (branch names), `abc123d` (commit SHAs), `latest`, arbitrary strings

```
error: apm.yml line 12: 'acme/foo#main' routes through the default registry
       but 'main' is not a semver version or range. Use an explicit
       `- git:` entry for branch or commit-SHA pinning, or change the
       ref to a semver version/range.
```

**Why strict semver here:** registries advertise versions, not refs. A silent route-flip from "Git ref" semantics to "registry version" semantics would produce confusing 404s; a parse-time error tells the user the exact fix. Today's string shorthand is ref-opaque (`#v1.0.0`, `#main`, `#<sha>` all supported — see [reference.py:668](src/apm_cli/models/dependency/reference.py:668), [dependencies.md:10-12](packages/apm-guide/.apm/skills/apm-usage/dependencies.md:10)); this rule only narrows the shape **when the entry routes to a registry**. Branch and SHA pinning remain fully available via the unchanged `- git:` explicit form, and string shorthand routed to Git (no default registry, no `@<name>`) stays byte-for-byte ref-opaque as today.

**Why not infer per-ref:** "treat post-`#` as opaque and let the registry 404" was considered and rejected — it leaks resolver semantics into user error messages and lets mistyped branch names silently become "version not found" errors.

### 3.4 Parser extension

`DependencyReference.from_dict` ([reference.py:430-508](src/apm_cli/models/dependency/reference.py:430)) gains:
1. One additional string-shorthand rule: if the string matches `owner/repo@<name>#<ref>` (or routes to the default registry via `registries.default`), treat `<name>` as a registry scope and validate `<ref>` as semver.
2. One additional object-form branch keyed by `registry:` (for virtual packages only).

Both are **strictly additive** — any pre-existing `apm.yml` that doesn't declare `registries.default` and doesn't use `@<name>` or `- registry:` continues to parse byte-identically.

**On `@` parsing precedence.** Today `@` in a dep string is only consumed by the SSH URL branch (`git@github.com:...`, triggered by the `git@` prefix or `://` in the string) and the alias branch (line 631, triggered inside an SSH repo part). A bare `owner/repo@name#ref` doesn't hit either. The new rule fires before alias parsing for non-SSH strings, which keeps existing alias behavior intact.

### 3.5 Identity preservation

`DependencyReference.get_identity()` stays `owner/repo` (or `host/owner/repo`). The registry is keyed by the same identity — the lockfile records *how* it was fetched via a new `source` field. This keeps `apm.yml` and package ids stable across resolvers (invariant §2.1.8).

---

## 4. End-to-End Flows

### 4.1 Install flow (registry mode)

```python
def apm_install():
    manifest = read("apm.yml")

    for dep in resolve_deps(manifest):              # recursive walk

        # ─── source-specific fetch (the ONLY new branch) ───
        if dep.source == "git":                     # existing
            GitHubPackageDownloader.download(dep)   #   git ls-remote + clone

        elif dep.source == "registry":              # NEW
            versions = registry.list_versions(dep.id)       # GET /versions
            v = pick_version(versions, dep.range)           # existing semver logic
            archive = registry.download(dep.id, v.version)   # GET /download
            verify_sha256(archive, v.digest)
            extract_archive(archive, content_type, f"apm_modules/{dep.owner}/{dep.repo}/")

        # ─── from here: unchanged, source-agnostic ───
        recurse_into(dep.extracted_apm_yml)         # discover transitives

    detect_drift()                                  # unchanged
    run_integrators()                               # AgentIntegrator, SkillIntegrator, …
    write_lockfile()                                # + source / resolved_hash / resolved_url
```

The registry-specific branch spans five lines. Once the tarball is extracted to `apm_modules/{owner}/{repo}/`, every subsequent step — transitive discovery, drift detection, integration, lockfile writing — runs the existing code unmodified.

### 4.2 Publish flow

```
apm pack                              # unchanged — produces .tar.gz
apm publish --registry corp-main     # NEW — reads $APM_REGISTRY_TOKEN_CORP_MAIN, wraps curl
                                      #                    -T bundle.tar.gz \
                                      #                    $REGISTRY/packages/{owner}/{repo}/versions/{ver}
```

Or, in CI with no CLI involvement:

```
apm pack
curl -X PUT --data-binary @bundle.tar.gz \
  -H "Authorization: Bearer $APM_REGISTRY_TOKEN_CORP_MAIN" \
  -H "Content-Type: application/gzip" \
  "$REGISTRY/packages/acme/web-skills/versions/1.2.0"
```

For Anthropic skills published as zip, swap the body and content type:

```
curl -X PUT --data-binary @skill.zip \
  -H "Authorization: Bearer $APM_REGISTRY_TOKEN_CORP_MAIN" \
  -H "Content-Type: application/zip" \
  "$REGISTRY/packages/acme/web-skills/versions/1.2.0"
```

Server-side validation (invalid apm.yml, missing primitives, malformed tarball) happens before acceptance and surfaces as `422 Unprocessable Entity` — see §5.3.

### 4.3 Search flow

```
apm marketplace search "security skills"
  ├─ for each registered marketplace → existing marketplace.json path (client-side)
  └─ for each configured registry    → GET /search?q=... (NEW, server-side)
  └─ merged/ranked results
```

Mode is chosen by the search source: marketplaces stay client-side; registries search server-side. No user-visible mode toggle needed.

### 4.4 Update / uninstall / prune

Unchanged in user-facing behavior. Internally:
- `apm install --update` re-queries `GET /versions` (registry) or `git ls-remote` (git) per dep.
- `apm uninstall` and `apm prune` operate on lockfile + deployed files, which are source-agnostic.

### 4.5 Marketplaces

Marketplaces remain **catalogs of curated pointers**, not artifact stores. This proposal does not change what a marketplace *is* — it only lets a marketplace entry point at a registry-sourced package in addition to a git-sourced one.

**Role separation:**

| Concept | Role | Controlled by |
|---|---|---|
| Marketplace | Curated index (which packages to recommend) | Curator (editorial) |
| Registry | Versioned artifact store (where tarballs live) | Publisher (producer) |

The two are **orthogonal** — a marketplace may list git-sourced packages, registry-sourced packages, or a mix. A curator is never required to run a registry.

**`marketplace.json` schema extension** (strictly additive):

```jsonc
{
  "plugins": [
    {
      "id": "review-skills",
      "name": "Code Review Skills",
      "repo": "acme/review-skills",
      "ref": "v1.0.0",                                       // existing — used when source: git (default)
      "description": "..."
    },
    {
      "id": "enterprise-skills",
      "name": "Enterprise Skills",
      "repo": "acme/enterprise-skills",
      "source": "registry",                                  // NEW — optional, defaults to "git"
      "version": "^3.0.0"                                    // NEW — used when source: registry
    }
  ]
}
```

The schema gains a single field: `source` (per entry, optional, defaults to `"git"`), plus `version` for entries with `source: registry`.

**No `registry_url` field exists in the marketplace.json.** Instead:

- A marketplace served from a **registry** (fetched at `<registry_url>/marketplace.json`) implicitly resolves all `source: registry` entries against `<registry_url>` — the URL it was just fetched from. No duplication, no drift between an inside-the-file value and the actual fetch URL.
- A marketplace served from a **Git repo** (today's flow) has no implicit registry, so its entries can only be `source: git`. To curate registry-sourced packages, host the marketplace on a registry.

**`apm marketplace add` accepts both:**
- `apm marketplace add OWNER/REPO` → Git-hosted marketplace (today, unchanged).
- `apm marketplace add https://registry.example.com/apm` → registry-hosted marketplace; client fetches `<URL>/marketplace.json` and uses `<URL>` as the registry for any `source: registry` entries.

**Existing marketplace.json files are unchanged** — none carry `source: registry` entries, all implicitly `source: git`.

**Install flow via marketplace:**

```
apm install enterprise-skills@corp-marketplace
  ├─ read corp-marketplace's marketplace.json
  ├─ find entry id=enterprise-skills
  ├─ entry has source: registry → route to registry resolver (§5.1 + §5.2)
  │   OR source: git (default) → route to git resolver (unchanged)
  └─ install as if user had declared the dep directly in apm.yml
```

**Publishing is never through a marketplace.** Publishing goes directly to a registry (§5.3). Adding the published version to a marketplace is a separate, manual editorial step by the curator — this preserves the producer/curator separation.

**Search:** already captured in §9 — client-side marketplace search and server-side registry search coexist, results are merged.

**Trust model:** marketplace entries are pointers, not authority. The `resolved_hash` lockfile invariant (§6) remains the sole trust anchor. A compromised marketplace entry fails closed on the hash check for any previously-installed version; first install is trust-on-first-use (same as every package manager).

---

## 5. API Contract

### Conventions

- **Base URL:** `$REGISTRY/` (vendor-defined path prefix — like `/artifactory/api/apm/{repo-key}`)
- **Identity in path:** `{owner}/{repo}` — matches `DependencyReference.get_identity()` for GitHub-origin packages. For non-GitHub origin, identity is percent-encoded: `gitlab.com%2Fowner%2Frepo`.
- **Auth:** `Authorization: Bearer <token>` on all endpoints. Token resolution follows existing `AuthResolver` patterns (see §7).
- **Content types:** `application/json` for metadata, `application/gzip` for tarballs.
- **Versioning:** Server URLs are versioned: `/v1/packages/...`. This proposal is v1.
- **Errors:** RFC 7807 Problem Details JSON on 4xx/5xx.

---

### 5.1 `GET /v1/packages/{owner}/{repo}/versions`

Returns all published versions for a package. Range resolution happens client-side, reusing the existing semver logic in `parse_git_reference()` (`src/apm_cli/deps/apm_resolver.py`) — the same code that matches git tags today.

**Request:** none (path only).

**Response 200:**
```json
{
  "package": "acme/web-skills",
  "versions": [
    {
      "version": "1.2.0",
      "published_at": "2026-03-01T12:00:00Z",
      "digest": "sha256:abc123..."
    },
    { "version": "1.1.0", "published_at": "...", "digest": "..." }
  ]
}
```

The response is cacheable (`Cache-Control: max-age=60` recommended) — published versions are immutable, the only mutations are new versions and yank flips.

**Effort (client):** **small addition**
- New file: `src/apm_cli/deps/registry/client.py` — `RegistryClient.list_versions()`
- Range matching: **reuses existing semver logic** from `src/apm_cli/deps/apm_resolver.py::parse_git_reference`. No new code.
- Tie-in: called from new `RegistryPackageResolver` (§8).

**Effort (server side):** out of scope for APM, but very small — existing artifact registries already expose version enumeration for most package types; this is a thin adapter.

---

### 5.2 `GET /v1/packages/{owner}/{repo}/versions/{version}/download`

Download the immutable package archive.

**Request:** none. Optional `Accept` negotiation deferred to v2 — v1 clients accept whatever the server returns and dispatch on `Content-Type`.

**Response 200:**
- `Content-Type:` one of:
  - `application/gzip` — gzipped tar (same shape as `apm pack` output)
  - `application/zip` — zip archive (Anthropic skills / open-claude-skills format)
- `Digest: sha256=<base64>` (RFC 3230)
- Body: the archive bytes

The endpoint is named `/download` (not `/tarball`) so the URL grammar doesn't lock the format. Same precedent as crates.io (`/api/v1/crates/{name}/{version}/download`) and the Docker Registry's `/blobs/{digest}` — content type is metadata, not URL syntax.

Client MUST verify the sha256 digest against the entry from §5.1 before extraction. The hash check happens against the raw bytes regardless of archive format.

**Format dispatch:** the client picks the extractor from the response's `Content-Type` (with magic-bytes fallback: `\x1f\x8b...` → gzip, `PK\x03\x04...` → zip). Both extractors enforce the same security gates (no absolute paths, no path traversal, no symlinks/hardlinks). A wrong-format guess fails cleanly because the hash has already gated extraction.

**Transitive dep discovery:** after extraction, the client reads `apm.yml` from `apm_modules/{owner}/{repo}/apm.yml` and recurses — the exact same step the Git resolver performs after `git clone`. No separate metadata call is needed.

**Effort (client):** **medium change**
- New: `RegistryClient.download_archive()` — streams to temp, verifies digest, surfaces Content-Type to the caller.
- New: `src/apm_cli/deps/registry/extractor.py` — exposes `extract_tarball()` and `extract_zip()` plus a single `extract_archive()` dispatcher; both extracts into `apm_modules/{owner}/{repo}/`.
- Touch: `apm_modules` layout is the same regardless of source or archive format, so integrators (`AgentIntegrator`, `SkillIntegrator`, etc.) need **no changes**.

---

### 5.3 `PUT /v1/packages/{owner}/{repo}/versions/{version}` — Publish

Upload a packaged tarball. Version is immutable — republishing the same `(owner, repo, version)` triple returns 409.

**Request:**
- `Authorization: Bearer <publish-token>` (scope checked server-side per §7)
- `Content-Type: application/gzip`
- Body: the `apm pack` tarball

**Response 201:**
```json
{
  "package": "acme/web-skills",
  "version": "1.2.0",
  "digest": "sha256:abc123...",
  "published_at": "2026-03-01T12:00:00Z"
}
```

**Errors:**
- `409 Conflict` — version already exists (immutable).
- `422 Unprocessable Entity` — server-side lint/validation failed (invalid apm.yml, missing primitives, malformed tarball).
- `403 Forbidden` — user lacks publish permission for this `owner/repo`.

**Effort (client):** **small addition**
- New command: `src/apm_cli/commands/publish.py` — wraps `apm pack` + HTTP PUT.
- Or: document pure `curl` usage (no CLI change required). Recommend both.

**Effort (existing `apm pack`):** **no change** — the bundle it already produces is the exact payload.

---

### 5.4 `GET /v1/search`

Server-side search across all packages the caller can read.

**Request:** `?q=query&limit=50&offset=0&type=skill&tag=security`

**Response 200:**
```json
{
  "query": "security skills",
  "total": 42,
  "results": [
    {
      "id": "acme/web-skills",
      "latest_version": "1.2.0",
      "description": "Security-hardened web interaction skills",
      "author": "acme",
      "tags": ["security", "web"],
      "type": "skill",
      "score": 0.92
    }
  ]
}
```

**Effort (client):** **small addition**
- Touch: `src/apm_cli/commands/marketplace.py` — `search()` gets a new branch: for each configured registry, call `GET /search` and merge with marketplace results.
- New: `RegistryClient.search()`.

See §9 for the server-vs-client comparison matrix.

---

## 6. Lockfile Changes

### 6.1 New fields on `LockedDependency`

The lockfile is **name-free** — it stores only the URL that was actually fetched from, and the content hash. No registry name appears in the lockfile, because names are a per-user config concern (two users may legitimately use different names for the same URL).

```yaml
dependencies:
  - repo_url: acme/web-skills
    host: github.com                                                                 # existing — origin host if known
    source: registry                                                                 # NEW — "git" | "registry" | "local"
    resolved_url: https://registry.corp/apm/acme/web-skills/versions/1.2.0/download  # NEW — URL actually fetched from
    version: "1.2.0"                                                                 # existing — authoritative for registry deps
    resolved_hash: sha256:abc123...                                                  # NEW — content hash of the tarball. THE trust anchor.
    resolved_commit: ""                                                              # existing — empty string for registry deps
    resolved_ref: ""                                                                 # existing — empty string for registry deps
    # ... all other fields unchanged (depth, deployed_files, deployed_file_hashes, etc.)
```

**Trust model (mirrors npm):** `resolved_hash` is the sole non-negotiable check on every install. `resolved_url` identifies the registry for re-fetch and audit; it may change benignly (registry migration, mirror swap) — the install succeeds as long as the bytes hash correctly.

**On install, for a registry-sourced dep:**
1. Fetch the tarball at `resolved_url` (auth chosen by URL — see §6.2).
2. Verify the tarball's sha256 against `resolved_hash`. **Fail closed on mismatch.** This is the only security-critical check.
3. Extract and proceed.

**Invariants:**
- `source: git` → `resolved_commit` required, `resolved_hash` optional, `resolved_url` absent.
- `source: registry` → `resolved_hash` required, `version` required, `resolved_url` required, `resolved_commit` empty, `resolved_ref` empty.
- `source: local` → unchanged.

**Threat coverage:**
- *Config tampering* (attacker changes the user's config to point at a different registry): the lockfile's `resolved_url` is what's fetched, not whatever config says — so config drift alone doesn't redirect the install. A mismatched config would fail at auth or at hash verification.
- *Registry compromise* (server serves malicious bytes): first install captures the bad hash (trust-on-first-use, same as npm); subsequent installs detect further tampering. Signing (deferred to post-v1) is the only full mitigation.
- *Lockfile tampering* (attacker rewrites both URL and hash): out of scope for any package manager; requires repo-level protections.

### 6.2 Resolving auth when the URL is not configured

A user who clones a repo whose lockfile references a registry URL they've never configured still needs to install. The rules:

1. **Look up auth by URL, not by name.** For each unique `resolved_url`, the client checks registries declared in apm.yml and `~/.apm/config.yml` for any whose URL matches (scheme + host + path prefix). If found, the client reads `APM_REGISTRY_TOKEN_{NAME}` (uppercased) for that entry. Registry *names* in the config are user-local; they never need to match across users.
2. **Anonymous fetch is tried first if no auth match.** If the registry responds 200, proceed (some registries have public read).
3. **401/403 → fail with a clear remediation message.** Example:
   ```
   error: this project depends on a package from
     https://registry.corp.example.com/apm
   but no credentials for that registry are configured on this machine.
   Add a registry entry whose URL matches (in apm.yml or ~/.apm/config.yml)
   and set APM_REGISTRY_TOKEN_<NAME>=<token> in your environment.
   ```
4. **Never prompt interactively.** CI-friendly; fail fast.

**Note on `apm.yml` registry names.** The name→URL mapping for named registries lives in `apm.yml`'s top-level `registries:` block (see §3.1), which is committed. So every user who clones the repo sees the same name→URL mapping at authoring time. User-local config only provides *auth* for those names (or adds new names for private mirrors). This way, `acme/foo@corp-main#^1.2` in a dep entry resolves to the same URL on every machine — there is no cross-user name divergence for project-declared registries.

### 6.3 Schema bump — opportunistic, never gratuitous

`lockfile_version: "1"` → `"2"`, but the bump happens **only when needed**:

- **Read:** v1 lockfiles parse without migration. All v1 deps are implicitly `source: git`.
- **Write:** The client emits v1 if and only if no registry-sourced deps are present in the resolved graph. A project that never opts into the registry will have its lockfile remain at v1 forever, even with a newer client. This is required by invariant §2.1.4.
- **Upgrade trigger:** The first registry-sourced dep added to a project triggers a v1→v2 rewrite of the lockfile, with the v1 entries copied verbatim (no semantic change).
- **Downgrade path:** Drop `source`/`resolved_hash`/`registry_name` fields and reject deps that require them. An older client reading a v2 lockfile MUST refuse to proceed and print a clear "upgrade APM or remove registry deps" message — never silently mis-resolve.

**Effort:** **medium change**
- Touch: `src/apm_cli/deps/lockfile.py` — add fields, add v1→v2 migration on read, bump version on write.
- Touch: `src/apm_cli/drift.py` — source-aware drift rules (for registry deps, ref-drift check is replaced by version-drift + hash-drift).

---

## 7. Auth

Reuses existing `AuthResolver` patterns with a new registry-specific axis.

### 7.1 Token resolution (per registry)

One new env var, `APM_REGISTRY_TOKEN_{NAME}` (uppercased registry name). Matches the existing `PROXY_REGISTRY_TOKEN` convention (single explicit var); avoids the discovery problem that forces `GITHUB_APM_PAT`/`GITHUB_APM_PAT_{ORG}` to have a fallback (registries are always declared by name in apm.yml, so there's nothing to discover).

If the env var is missing, the client tries anonymous fetch; on 401/403 it fails with the remediation message in §6.2.

### 7.2 Scope semantics (server-side)

- `read` — required for §5.1, §5.2, §5.4.
- `publish:{owner}/{repo}` or `publish:{owner}/*` — required for §5.3. Server rejects with 403 on scope mismatch.

### 7.3 Integration with existing `AuthResolver`

**Effort:** **small addition**
- Touch: `src/apm_cli/core/auth.py` — add `resolve_for_registry(registry_name)` method.
- New: `src/apm_cli/deps/registry/auth.py` — registry-specific token manager (thin wrapper).

No changes to the existing Git/VCS auth path.

---

## 8. Graph Resolution

**Client-side recursion only.**

The registry resolver mirrors the Git resolver's shape exactly: for each dep, list versions (§5.1), pick a version via the existing semver logic, fetch the tarball (§5.2), extract, read `apm.yml`, recurse. This is a drop-in `DownloadCallback` — no changes to `APMDependencyResolver` itself. Swapping `git clone` for cacheable HTTPS tarball fetches is already a win, and the BFS walk becomes trivially parallelizable per level as a later optimization.

**Considered, deferred:** returning a dep's declared apm.yml dependencies in the /versions response (or a separate per-version metadata endpoint). See Decisions section.

**Effort:** **small addition**
- New: `src/apm_cli/deps/registry/resolver.py` — implements the `DownloadCallback` protocol.
- Touch: `src/apm_cli/commands/install.py` — route deps to registry callback when `source: registry`.
- **No changes** to `APMDependencyResolver` itself.

---

## 9. Search: Server vs Client

Today, search is **client-side**: `apm marketplace search` fetches each registered marketplace's `marketplace.json` in full and substring-matches locally (`src/apm_cli/marketplace/client.py::search_marketplace`). That stays. The registry adds a second path, **server-side search** (§5.4), used only for registry-backed catalogs.

| | Client-side (today) | Server-side (§5.4) |
|---|---|---|
| Scales with catalog size | Whole index downloaded per search | Zero — server indexes |
| Ranking | Substring match | Whatever the server implements (typically full-text) |
| Permission-aware | No | Yes — server scopes results to the caller's token |
| Freshness | Tied to marketplace.json cache | Real-time |
| Offline | Works with cached index | Requires network |
| Privacy | Entire catalog leaks to every client | Only matched results leave the server |
| Client effort | Already implemented | Small addition — one new `RegistryClient.search()` call, one branch in `commands/marketplace.py` |

**Why both, not one:** client-side search is the natural fit for Git-hosted marketplaces (small, public, already works). Server-side search is the natural fit for registry-hosted catalogs (large, private, permission-scoped). `apm marketplace search` queries whichever applies per source and merges.

---

## 10. Implementation Effort Summary

Sized per file, using: **no change** / **small addition** (<50 LoC) / **medium change** (refactor of one module) / **large change** (cross-cutting refactor).

| File | Change size | What |
|---|---|---|
| `src/apm_cli/models/apm_package.py` | **small addition** | Parse top-level `registries:` block; route string shorthand to default registry with semver validation. |
| `src/apm_cli/models/dependency/reference.py` | **small addition** | Carry `source` + `registry_name` on `DependencyReference`. Identity scheme unchanged. |
| `src/apm_cli/deps/lockfile.py` | **medium change** | New fields (`source`, `resolved_hash`, `registry_name`), v1→v2 migration. |
| `src/apm_cli/drift.py` | **medium change** | Source-aware drift: version-drift + hash-drift for registry deps; ref-drift stays for git deps. |
| `src/apm_cli/deps/apm_resolver.py` | **small addition** | Accept multiple download callbacks keyed by `source`. No refactor needed. |
| `src/apm_cli/deps/registry/client.py` | **NEW — small file** | HTTP client: `list_versions`, `download_tarball`, `publish`, `search`. |
| `src/apm_cli/deps/registry/resolver.py` | **NEW — small file** | Implements `DownloadCallback` for `source: registry`. |
| `src/apm_cli/deps/registry/extractor.py` | **NEW — small file** | Tarball → `apm_modules/{owner}/{repo}/`. |
| `src/apm_cli/deps/registry/auth.py` | **NEW — small file** | Registry token resolution. |
| `src/apm_cli/core/auth.py` | **small addition** | `resolve_for_registry(name)` method. |
| `src/apm_cli/commands/install.py` | **small addition** | Route deps to registry vs git resolver based on `source`. |
| `src/apm_cli/commands/publish.py` | **NEW — small file** | `apm publish` command: wraps `apm pack` + HTTP PUT. |
| `src/apm_cli/commands/marketplace.py` | **small addition** | `search()` also queries configured registries. |
| `src/apm_cli/bundle/packer.py` | **no change** | `apm pack` output is already the publish payload. |
| `src/apm_cli/integration/*.py` | **no change** | Integrators operate on `apm_modules/`, source-agnostic. |
| `src/apm_cli/commands/uninstall/*.py` | **no change** | Operates on lockfile + deployed files, source-agnostic. |
| `src/apm_cli/commands/prune.py` | **no change** | Same. |
| Tests | **new tests + extend existing** | Registry client unit tests, install/publish integration tests against a fake server. |

**Total net-new files:** 5 small.
**Total touched existing files:** 7 (mostly small, two medium).
**No file requires a complete refactor.**

The change is **additive by construction**: the Git resolver path is left alone, and every existing test continues to pass because deps without `source:` default to `git`.

---

## 11. Decisions

- **Virtual packages are sliced client-side.** Virtual package identity includes a sub-path (`owner/repo/prompts/file.prompt.md`). The registry treats `owner/repo` as a single unit — the parent tarball. The client extracts only the requested sub-path, mirroring the current virtual-package handling on the Git side. Keeps the server contract flat.
- **No yank in v1.** There is no yank concept in the current Git-based flow (tag immutability is convention, not enforcement). Introducing yank would be new a capability beyond parity, so it is deferred; §5.1's response has no `yanked` field. Can be added in v2 without a compat break.
- **Rate limiting reuses existing client retry.** The new `RegistryClient` calls the project's existing `resilient_get` helper, inheriting whatever retry / `Retry-After` semantics are already in place for HTTP. No new policy.
- **No server-assisted transitive resolution in v1.** Embedding a version's declared `apm.yml` deps in `/versions` (or a separate per-version metadata endpoint) would let the client build the transitive graph without downloading every tarball. Deferred — it enlarges the server contract (publish must extract and index manifests) and adds a sync-with-tarball invariant. See §8 for the deferred note; future-compatible with v1.
