# APM Dedicated Registry — HTTP API Specification

**Status:** v1 — implementable
**Audience:** server implementers (Artifactory plugin authors, Nexus format authors, OSS reference servers)
**Companion docs:**
- [registry-api.md](./registry-api.md) — full design proposal (rationale, integration plan)
- [registry-api-summary.md](./registry-api-summary.md) — short summary for client maintainers

This document is the wire-level contract. It is self-contained — a server implementer should be able to build a conformant registry from this doc alone.

---

## Table of contents

1. [Conventions](#1-conventions)
2. [Authentication](#2-authentication)
3. [Endpoints](#3-endpoints)
   1. [`GET /v1/packages/{owner}/{repo}/versions`](#31-get-v1packagesownerrepoversions--list-versions)
   2. [`GET /v1/packages/{owner}/{repo}/versions/{version}/download`](#32-get-v1packagesownerrepoversionsversiondownload--download-archive)
   3. [`PUT /v1/packages/{owner}/{repo}/versions/{version}`](#33-put-v1packagesownerrepoversionsversion--publish)
   4. [`GET /v1/search`](#34-get-v1search--server-side-search)
   5. [`GET /v1/marketplace.json`](#35-get-v1marketplacejson--optional-marketplace-host)
4. [Error model](#4-error-model)
5. [Conformance: required vs optional](#5-conformance-required-vs-optional)
6. [Server validation rules (publish)](#6-server-validation-rules-publish)
7. [Caching, rate limiting, and headers](#7-caching-rate-limiting-and-headers)
8. [Security checklist](#8-security-checklist)
9. [Reference test fixtures](#9-reference-test-fixtures)

---

## 1. Conventions

### 1.1 Base URL

The registry's **base URL** is vendor-defined. Examples:

- Artifactory APM repo: `https://artifactory.example.com/artifactory/api/apm/{repo-key}`
- Nexus APM proxy: `https://nexus.example.com/repository/{repo-name}`
- OSS reference: `https://registry.example.com/apm`

Clients always append paths starting with `/v1/...` to the base URL. The base URL MUST NOT contain a trailing slash; if vendors return one in their UI, the client strips it (see `RegistryClient.__init__`).

### 1.2 Identity

`{owner}/{repo}` in path segments matches `DependencyReference.get_identity()` for GitHub-origin packages. For non-GitHub origins, identity is **percent-encoded** (each `/` in the identity becomes `%2F`):

| Origin | Identity | Path segment |
|---|---|---|
| GitHub | `acme/web-skills` | `acme/web-skills` (two segments) |
| GitLab | `gitlab.com/acme/web-skills` | `gitlab.com%2Facme%2Fweb-skills` (one encoded segment) |
| Azure DevOps | `dev.azure.com/org/proj/repo` | `dev.azure.com%2Forg%2Fproj%2Frepo` (one encoded segment) |

Servers MUST decode percent-encoded path segments before lookup.

### 1.3 Versions

Version strings follow [semver 2.0](https://semver.org/) with the following constraints:

- Pre-release tags (`-rc.1`, `-beta.2`) are **reserved for v2** — v1 servers MAY accept them on publish but MUST NOT advertise them in `/versions` responses (yank-equivalent behavior).
- Build metadata (`+sha.abc`) is **stripped on publish** — the canonical version is the part before `+`.
- Versions are **case-sensitive** strings; `v1.0.0` and `1.0.0` are distinct identifiers.

### 1.4 Content types

| Resource | Type |
|---|---|
| List versions, search, problem details, publish response | `application/json; charset=utf-8` |
| RFC 7807 problem detail | `application/problem+json; charset=utf-8` |
| Archive download (gzip) | `application/gzip` |
| Archive download (zip) | `application/zip` |

Servers SHOULD set `charset=utf-8` on JSON responses but clients MUST NOT depend on it.

### 1.5 Versioning the API itself

The leading `/v1/` path segment is the API version. Future breaking changes ship under `/v2/`. Servers SHOULD support multiple versions in parallel during migration.

### 1.6 Idempotency and immutability

- **Versions are immutable.** A successful `PUT .../versions/{version}` cannot be overwritten — subsequent PUTs return `409 Conflict`. This is a hard requirement; clients depend on it for the lockfile trust model.
- **List queries are idempotent.** `GET /versions` MUST return the same set of versions on identical inputs.
- **Search results are best-effort.** The server MAY rerank or reorder results between calls.

---

## 2. Authentication

### 2.1 Bearer token

Every endpoint accepts `Authorization: Bearer <token>`. Tokens are opaque strings issued by the registry; the client treats them as bytes (no parsing, no inspection).

When the header is absent, the server MAY:
- accept the request (anonymous read on a public registry), OR
- reject with `401 Unauthorized` (authenticated reads only).

Clients try anonymous first when no env var is configured for a URL (per design §6.2). Servers SHOULD return `401` rather than `403` for missing-credential cases so clients can distinguish "auth required" from "auth provided but not authorized."

### 2.2 Scopes (server-side enforcement)

Tokens MUST carry one or more of these scopes. Clients never see scope strings — the server enforces them on each request.

| Scope | Required for | Notes |
|---|---|---|
| `read` | `GET /versions`, `GET /download`, `GET /search`, `GET /marketplace.json` | Coarse read access |
| `read:{owner}/{repo}` | Same as `read` but scoped | Optional fine-grained variant |
| `publish:{owner}/{repo}` | `PUT .../versions/{version}` | Per-package publish authority |
| `publish:{owner}/*` | Same, all repos under owner | Convenience wildcard |

Servers MUST reject mismatched-scope requests with `403 Forbidden` and an RFC 7807 body citing which scope is missing.

### 2.3 Anti-collision with existing env vars (client-side)

Clients use `APM_REGISTRY_TOKEN_{NAME}` where `{NAME}` is the uppercased registry name (`-` and `.` mapped to `_`). The prefix is distinct from `GITHUB_TOKEN`, `GITHUB_APM_PAT`, `PROXY_REGISTRY_*`, and `ARTIFACTORY_APM_TOKEN`. Servers don't see this — included here for protocol completeness.

---

## 3. Endpoints

### 3.1 `GET /v1/packages/{owner}/{repo}/versions` — list versions

Returns all published versions for a package.

**Request**

```
GET /v1/packages/acme/web-skills/versions HTTP/1.1
Host: registry.example.com
Authorization: Bearer <token>
Accept: application/json
```

**Response 200**

```json
{
  "package": "acme/web-skills",
  "versions": [
    {
      "version": "1.2.0",
      "digest": "sha256:abc123...",
      "published_at": "2026-03-01T12:00:00Z",
      "size_bytes": 24576
    },
    {
      "version": "1.1.0",
      "digest": "sha256:def456...",
      "published_at": "2026-02-14T08:00:00Z",
      "size_bytes": 23000
    }
  ]
}
```

**Field requirements**

| Field | Required | Notes |
|---|---|---|
| `package` | yes | Echoes the requested identity. Useful for clients that fetched via percent-encoded path. |
| `versions[]` | yes | May be empty (`[]`); MUST NOT be omitted. |
| `versions[].version` | yes | Semver string. |
| `versions[].digest` | yes | sha256 of the archive bytes. Format: `sha256:<64 lowercase hex chars>`. |
| `versions[].published_at` | yes | ISO 8601 UTC timestamp. |
| `versions[].size_bytes` | optional | Archive size; informational. |

**Ordering.** Servers SHOULD return versions in publish-time descending order (newest first). Clients MUST NOT depend on order — they sort by semver client-side.

**Errors**

| Status | Reason |
|---|---|
| `401` | Missing/invalid token, anonymous reads disabled |
| `403` | Token lacks `read` scope for this package |
| `404` | Package not found |

**Caching.** Versions are immutable, but the SET of versions changes when new releases ship. Servers SHOULD set `Cache-Control: max-age=60` (or shorter); clients MAY honor it.

---

### 3.2 `GET /v1/packages/{owner}/{repo}/versions/{version}/download` — download archive

Streams the immutable package archive. The endpoint is named `/download` (not `/tarball`) because both gzip and zip archives are valid responses; `Content-Type` discriminates.

**Request**

```
GET /v1/packages/acme/web-skills/versions/1.2.0/download HTTP/1.1
Host: registry.example.com
Authorization: Bearer <token>
Accept: application/gzip, application/zip
```

Clients SHOULD send the `Accept` header to advertise both formats. Servers SHOULD honor `Accept` if they store both, but MAY ignore it and return whatever was published.

**Response 200**

```
HTTP/1.1 200 OK
Content-Type: application/gzip          ← or application/zip
Content-Length: 24576
Digest: sha256=<base64-of-binary-digest>   (RFC 3230)
ETag: "sha256:abc123..."

<binary archive body>
```

**Required headers**

| Header | Required | Notes |
|---|---|---|
| `Content-Type` | yes | One of `application/gzip` or `application/zip`. |
| `Content-Length` | recommended | Streamed delivery is fine; if absent, clients buffer to memory. |
| `Digest` | recommended | RFC 3230 hash. Clients verify against `versions[].digest` from /versions, not this header. |
| `ETag` | optional | Conditional GET support; clients may use it for caching across runs. |

**Body.** Raw archive bytes. The same bytes that hash to the `digest` advertised in `/versions`.

**Format selection at publish time.** APM publishes via `apm pack` (tar.gz). Anthropic skills publish via standard zip. Servers store and return whatever was uploaded; format conversion is NOT a server responsibility.

**Errors**

| Status | Reason |
|---|---|
| `401`, `403` | Same semantics as 3.1 |
| `404` | No such (owner, repo, version) tuple |
| `410` | Version yanked (v2; reserved) |

**Hash verification on client.** Per design §6.1, clients re-hash the body against `versions[].digest` from a fresh `/versions` call OR from the lockfile's `resolved_hash`. A mismatch fails closed before extraction. Servers SHOULD NOT rely on this — they provide bytes; the trust gate is client-side.

---

### 3.3 `PUT /v1/packages/{owner}/{repo}/versions/{version}` — publish

Uploads a new version. Versions are immutable: re-publishing returns `409`.

**Request**

```
PUT /v1/packages/acme/web-skills/versions/1.2.0 HTTP/1.1
Host: registry.example.com
Authorization: Bearer <publish-token>
Content-Type: application/gzip
Content-Length: 24576

<binary archive body>
```

**Body.** Archive bytes — either tar.gz (`application/gzip`) or zip (`application/zip`). The server records the Content-Type and replays it on subsequent `GET /download`.

**Response 201**

```json
{
  "package": "acme/web-skills",
  "version": "1.2.0",
  "digest": "sha256:abc123...",
  "published_at": "2026-03-01T12:00:00Z",
  "size_bytes": 24576
}
```

**Errors**

| Status | Reason |
|---|---|
| `400` | Malformed body (e.g. corrupt gzip, invalid zip directory). Body: RFC 7807 with `detail` describing the parse error. |
| `401`, `403` | Auth missing / scope mismatch |
| `409` | Version already exists. Body: RFC 7807 with `detail: "version 1.2.0 already published at 2026-02-14T08:00:00Z"`. |
| `413` | Body exceeds the registry's per-archive size limit. |
| `415` | `Content-Type` is neither `application/gzip` nor `application/zip`. |
| `422` | Server-side validation failed (see §6). Body lists validation errors in `extensions.errors[]`. |

Idempotency for `PUT` is **not** the standard "same request always succeeds" — it's "same `(owner, repo, version)` always returns 409 after the first success." This is the immutability invariant clients depend on for the lockfile trust model.

---

### 3.4 `GET /v1/search` — server-side search

Free-text search across packages the caller can read.

**Request**

```
GET /v1/search?q=security+skills&limit=50&offset=0&type=skill&tag=auth HTTP/1.1
Host: registry.example.com
Authorization: Bearer <token>
Accept: application/json
```

**Query parameters**

| Param | Required | Type | Notes |
|---|---|---|---|
| `q` | yes | string | URL-encoded free-text query. Server-defined matching (typically full-text). |
| `limit` | optional | int (1-100) | Default 50. |
| `offset` | optional | int (≥0) | For pagination. |
| `type` | optional | string | Filter by primitive type: `skill`, `prompt`, `agent`, `instruction`, `hook`, `command`, `chatmode`. |
| `tag` | optional | string | Filter by tag. May be repeated (`?tag=a&tag=b`) for AND. |

**Response 200**

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

**Field requirements**

| Field | Required | Notes |
|---|---|---|
| `query` | yes | Echoes the query for client-side debugging. |
| `total` | yes | Total matches across all pages (for pagination UI). |
| `results[]` | yes | Sorted by `score` descending. |
| `results[].id` | yes | `{owner}/{repo}` — the canonical identity. |
| `results[].latest_version` | yes | Highest semver-valid version published. |
| `results[].description` | optional | Plain text. |
| `results[].author` | optional | Free-form string. |
| `results[].tags` | optional | Array of strings. |
| `results[].type` | optional | One of the primitive types in the `type` filter list. |
| `results[].score` | optional | Float in [0, 1]. Higher is better. |

**Permission scoping.** Results MUST be scoped to packages the bearer token has `read` permission for. A registry with public reads MAY return public-only results when the request is anonymous.

---

### 3.5 `GET /v1/marketplace.json` — (optional) marketplace host

A registry MAY also serve a marketplace catalog at this path. When present, the response is a `marketplace.json` document conforming to the schema in [registry-api.md §4.5](./registry-api.md#45-marketplaces) (with the `registry: <name>` extension for registry-routed entries).

This endpoint is optional. Registries focused purely on package storage SHOULD return `404` here; clients fall back to Git-hosted marketplaces.

---

## 4. Error model

All `4xx` and `5xx` responses use **RFC 7807 Problem Details** in `application/problem+json`:

```json
{
  "type": "https://docs.apm.dev/errors/version-conflict",
  "title": "Version already published",
  "status": 409,
  "detail": "Version 1.2.0 of acme/web-skills was already published at 2026-02-14T08:00:00Z",
  "instance": "/v1/packages/acme/web-skills/versions/1.2.0",
  "extensions": {
    "previous_publish": "2026-02-14T08:00:00Z",
    "previous_digest": "sha256:..."
  }
}
```

**Required fields:** `title`, `status`. All others optional but recommended.

**Vendor extensions** belong under `extensions.*` per RFC 7807. Clients MUST ignore unknown extensions.

---

## 5. Conformance: required vs optional

A **conformant v1 server** MUST implement:

- `GET /v1/packages/{owner}/{repo}/versions`
- `GET /v1/packages/{owner}/{repo}/versions/{version}/download`
- `PUT /v1/packages/{owner}/{repo}/versions/{version}`
- RFC 7807 error bodies on all 4xx/5xx
- Bearer auth on all endpoints (anonymous reads optional)
- sha256 digest accuracy (the byte sequence served at `/download` MUST match the digest advertised at `/versions`)
- Version immutability (a successful PUT cannot be overwritten)

A **fully-featured v1 server** SHOULD additionally implement:

- `GET /v1/search`
- `GET /v1/marketplace.json` (when serving as a marketplace host)
- `Cache-Control` and `ETag` on read endpoints
- Conditional `GET` (`If-None-Match`) returning `304`
- Per-version `size_bytes` field

Clients MUST NOT crash on missing optional fields; they MUST parse `versions[]` even with no `published_at` and treat `description`/`author`/`tags` as optional in search results.

---

## 6. Server validation rules (publish)

On `PUT .../versions/{version}`, the server MUST validate (returning `422` on failure with errors in `extensions.errors[]`):

1. **Version is parseable semver** (matches `^v?\d+(\.\d+)?(\.\d+)?$` with optional pre-release/build metadata).
2. **Archive parses cleanly** as the declared `Content-Type` (gzip or zip).
3. **Archive contains an `apm.yml` at the root** of the extraction tree.
4. **`apm.yml` is valid YAML** with required fields (`name`, `version`).
5. **`apm.yml.version` matches the URL path version** (mismatch → `422`, not silent acceptance).
6. **`apm.yml.name` matches the URL path identity** (or its repo-name suffix — implementation-defined).
7. **Archive entries are safe** — no absolute paths, no `..` traversal, no symlinks/hardlinks.
8. **Archive size is within limits** — vendor-defined; suggested default 50 MB.

**Out of scope for v1:**

- License-text validation
- Vulnerability scanning (servers MAY block but it's not required by the spec)
- Signature verification (deferred to v2)

---

## 7. Caching, rate limiting, and headers

### 7.1 Caching

| Endpoint | Recommended `Cache-Control` |
|---|---|
| `GET /versions` | `max-age=60, public` |
| `GET /download` | `max-age=86400, immutable` (versions are immutable) |
| `GET /search` | `max-age=10` or `no-cache` (results may rerank) |
| `GET /marketplace.json` | `max-age=300, public` |

Clients MAY ignore these. APM v1 client does no HTTP caching.

### 7.2 Rate limiting

Servers SHOULD return `429 Too Many Requests` with a `Retry-After` header (seconds) when limits are exceeded. The body SHOULD be RFC 7807 with `extensions.limit` and `extensions.remaining`.

### 7.3 Required response headers

`Content-Type` is always required. Other headers are recommended but optional.

---

## 8. Security checklist

For server implementers:

- [ ] **TLS only.** Plain HTTP MUST NOT be supported in production. (Local dev is fine.)
- [ ] **Token storage.** Use a one-way hash (bcrypt/argon2) for stored bearer tokens; never store plaintext.
- [ ] **Path traversal prevention.** Reject `..` segments in `{owner}`, `{repo}`, `{version}` path params before any storage lookup.
- [ ] **Archive scanning at publish.** Validate per §6 before persistence; reject zip slip / symlink attacks.
- [ ] **Constant-time digest comparison.** When comparing a client-provided digest (e.g. for conditional GET) to the stored value.
- [ ] **Audit log.** Record every successful `PUT` with (token-id, owner/repo, version, sha256, timestamp).
- [ ] **Quota enforcement.** Per-token / per-owner archive size and count limits.

For client implementers (informational):

- [ ] Verify sha256 against `versions[].digest` before extraction.
- [ ] Reject `..`, absolute paths, symlinks, and hardlinks during extraction.
- [ ] Persist `resolved_url` in the lockfile (not the registry name) — it's the trust anchor for re-installs.
- [ ] On `401/403`, surface a remediation message pointing at `APM_REGISTRY_TOKEN_<NAME>`.

---

## 9. Reference test fixtures

A conformance test suite for server implementers SHOULD exercise:

### 9.1 Round-trip publish-then-fetch

1. `PUT .../versions/1.0.0` with a valid tar.gz body → `201` with the right digest.
2. `GET .../versions/1.0.0/download` → returns the same bytes.
3. sha256 of the returned bytes equals the digest from the `201` response.
4. `GET .../versions` → contains the `1.0.0` entry with the same digest.

### 9.2 Immutability

1. `PUT .../versions/1.0.0` → `201`.
2. `PUT .../versions/1.0.0` (same body) → `409`.
3. `PUT .../versions/1.0.0` (different body) → `409`.

### 9.3 Format dispatch

1. `PUT .../versions/1.0.0` with `Content-Type: application/zip` → `201`.
2. `GET .../versions/1.0.0/download` → returns `Content-Type: application/zip` and the same bytes.
3. Hash matches.

### 9.4 Validation

1. `PUT` with a tarball missing `apm.yml` → `422` with appropriate error message.
2. `PUT` with `apm.yml.version` ≠ URL version → `422`.
3. `PUT` with absolute paths in tar → `422`.
4. `PUT` with symlink in zip → `422`.

### 9.5 Auth

1. Anonymous `GET /versions` on a public package → `200` (or `401` on private registry).
2. `GET /versions` with token lacking `read` scope → `403`.
3. `PUT` with token lacking `publish` scope → `403`.
4. `PUT` with no token → `401`.

### 9.6 Error format

1. Any `4xx` response has `Content-Type: application/problem+json`.
2. Body is valid JSON with at least `title` and `status`.

---

## Changelog

- **v1.0** (initial) — `versions`, `download`, `publish`, `search`, optional `marketplace.json`. tar.gz + zip both supported. RFC 7807 errors. Bearer auth. Immutable versions. No yank, no signing, no pre-release advertising — those are v2.
