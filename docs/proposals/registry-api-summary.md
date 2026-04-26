# Dedicated Registry API — Technical Summary

**Status:** Draft — for APM maintainer review
**Full proposal:** [registry-api.md](./registry-api.md)
**HTTP API spec:** [registry-http-api.md](./registry-http-api.md) — for server implementers
**Scope:** Plugins, skills, prompts, agents, instructions, hooks, commands, chatmodes (not MCP)

An additive, opt-in REST resolver that sits alongside the existing Git resolver. Fully backwards compatible — a project without a `registries:` block sees no change.

---

## Set me up

### 1. Declare the registry in `apm.yml` (committed)

```yaml
name: my-project
version: 1.0.0

registries:
  corp-main:
    url: https://registry.corp.example.com/apm   # e.g. /artifactory/api/apm/{repo-key}
  default: corp-main                              # omit to keep Git as the default

dependencies:
  apm:
    # String shorthand — routes through registries.default when set
    - acme/web-skills#^1.2

    # Named non-default registry via @scope (reuses marketplace syntax)
    - acme/foo@corp-other#^3.0

    # Virtual package — object form (sub-path can't fit in shorthand)
    - registry: corp-main
      id: acme/prompt-pack
      path: prompts/review.prompt.md
      version: 1.4.0

    # Explicit Git (always available)
    - git: https://github.com/acme/core.git
      ref: v2.0
```

### 2. Set the auth token (per user, never committed)

```
APM_REGISTRY_TOKEN_CORP_MAIN=<token>
```

Convention: `APM_REGISTRY_TOKEN_{NAME}` where `{NAME}` is the uppercased registry name.

### 3. Routing rule

| entry shape | resolver |
|---|---|
| `- git: <url>` … | Git (explicit) |
| `- path: ./local` | Local |
| `- registry: <name>` … (object form) | Registry — required only for virtual packages (sub-path) |
| `- owner/repo@<name>#<semver>` (string) | Registry `<name>` — reuses marketplace `@scope` syntax |
| `- owner/repo#<semver>` (string) | Default registry if `registries.default` is set, else Git |

When routed to a registry, the string-shorthand ref **must** be a semver version or range (`1.0.0`, `^1.0`, `~1.2.3`). Branches and commit SHAs fail at parse — use `- git:` for those.

### 4. Publish

```
apm pack
curl -X PUT --data-binary @bundle.tar.gz \
  -H "Authorization: Bearer $APM_REGISTRY_TOKEN_CORP_MAIN" \
  -H "Content-Type: application/gzip" \
  "$REGISTRY/v1/packages/acme/web-skills/versions/1.2.0"
```

---

## API Contract

Four endpoints. All responses are JSON unless noted. Errors use RFC 7807 Problem Details on 4xx/5xx.

### `GET /v1/packages/{owner}/{repo}/versions` — list versions

Returns all published versions. Client picks one via existing semver logic.

**Response 200:**
```json
{
  "package": "acme/web-skills",
  "versions": [
    { "version": "1.2.0", "published_at": "2026-03-01T12:00:00Z", "digest": "sha256:abc123..." },
    { "version": "1.1.0", "published_at": "...",                 "digest": "sha256:..." }
  ]
}
```

Cacheable (`Cache-Control: max-age=60` recommended) — versions are immutable.

### `GET /v1/packages/{owner}/{repo}/versions/{version}/download` — fetch

Downloads the immutable package archive. Endpoint is format-neutral (same precedent as crates.io `/download` and the Docker Registry's `/blobs`); `Content-Type` tells the client which extractor to use.

**Response 200:**
- `Content-Type:` one of `application/gzip` (tar.gz) or `application/zip` (Anthropic skills format)
- `Digest: sha256=<base64>` (RFC 3230)
- Body: archive bytes

Client MUST verify the sha256 digest against the entry from the list endpoint before extraction. The hash check happens against the raw bytes regardless of archive format. After extraction, the client reads `apm.yml` from `apm_modules/{owner}/{repo}/apm.yml` and recurses for transitive deps — no separate metadata call.

### `PUT /v1/packages/{owner}/{repo}/versions/{version}` — publish

Upload a packaged archive. Versions are immutable.

**Request:**
- `Authorization: Bearer <publish-token>`
- `Content-Type:` one of `application/gzip` (tar.gz) or `application/zip`
- Body: the archive bytes

**Response 201:**
```json
{
  "package": "acme/web-skills",
  "version": "1.2.0",
  "digest": "sha256:abc123...",
  "published_at": "2026-03-01T12:00:00Z"
}
```

**Errors:** `409` republish attempt · `422` server-side validation failure · `403` missing publish permission.

### `GET /v1/search` — search

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

---

## Auth scopes (server-side)

- `read` — required for the list, fetch, and search endpoints.
- `publish:{owner}/{repo}` or `publish:{owner}/*` — required for publish. Server rejects with 403 on mismatch.
