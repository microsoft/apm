---
title: OpenAPM v0.1
description: Normative specification for the Agent Package Manager (APM) format and conformance.
sidebar:
  order: 1
---

OpenAPM v0.1 is the normative specification of the APM package format, manifest, lockfile, and policy semantics. It is the contract implementers, conformance testers, and enterprise reviewers build against. If you are learning how to USE APM, start with the consumer, producer, or enterprise guides -- this page defines what APM IS, not how to operate it.

## Status of This Document

This document is an **editor's Working Draft** of OpenAPM, version **0.1**.
It is published to invite review, implementation feedback, and adversarial
critique. It MAY be updated, replaced, or made obsolete at any time. Citing
this document as anything other than work in progress is inappropriate.

OpenAPM is published under the **MIT License**.

Version **0.1** is a `0.x` editor's draft under semantic-version-zero
discipline: no backward-compatibility guarantee applies until version
**1.0**. Each `0.x` minor MAY introduce breaking changes with the
migration window described in [Section 9](#9-versioning-and-amendment-process).

Editors track feedback in `microsoft/apm` and amend per the process in
[Section 9.3](#93-amendment-process).

## Abstract

OpenAPM defines the on-disk file formats, dependency-resolution semantics,
primitive type system, deployment matrix, and governance policy format used
by the Agent Package Manager (APM). A conforming producer authors an
`apm.yml` manifest; a conforming consumer resolves the declared
dependencies, writes an `apm.lock.yaml` lockfile, and deploys primitives
to the directories the targets matrix specifies; a conforming governance
implementation evaluates an `apm-policy.yml` policy against the install
plan before any byte is written to disk. The wire contract between
consumers and registry servers is **not normative in v0.1** and is
reserved for v0.2 (see [Appendix B](#appendix-b-registry-http-api-reserved-for-v02)).

## Table of Contents

1. [Introduction](#1-introduction)
2. [Conventions](#2-conventions)
3. [Terminology](#3-terminology)
4. [Manifest format (apm.yml)](#4-manifest-format-apmyml)
5. [Lockfile format (apm.lock.yaml)](#5-lockfile-format-apmlockyaml)
6. [Policy format (apm-policy.yml)](#6-policy-format-apm-policyyml)
7. [Dependency resolution](#7-dependency-resolution)
8. [Primitive type system and target matrix](#8-primitive-type-system-and-target-matrix)
9. [Versioning and amendment process](#9-versioning-and-amendment-process)
10. [Security considerations](#10-security-considerations)
11. [Conformance](#11-conformance)
12. [Conformance test methodology](#12-conformance-test-methodology)
13. [Appendix A: Normative JSON Schemas (inline)](#appendix-a-normative-json-schemas-inline)
14. [Appendix B: Registry HTTP API (reserved for v0.2)](#appendix-b-registry-http-api-reserved-for-v02)
15. [Appendix C: Index of normative statements](#appendix-c-index-of-normative-statements)
16. [Appendix D: Revision history](#appendix-d-revision-history)
17. [Appendix E: Editorial reconciliation notes](#appendix-e-editorial-reconciliation-notes)

---

## 1. Introduction

### 1.1 Goals and non-goals

**Goals.**

- Define an interoperable on-disk format (`apm.yml`, `apm.lock.yaml`,
  `apm-policy.yml`) that any conformant tool can read and write.
- Specify dependency resolution semantics precisely enough that two
  independent implementations produce equivalent lockfiles from the
  same manifest and remote state.
- Specify a target-matrix contract (detection signals and deploy
  directories) so consumers can pin deploy paths against the spec
  rather than against a single implementation.
- Specify a governance policy format that lets organisations gate
  installs without forking the consumer toolchain.

**Non-goals (v0.1).**

- The registry HTTP wire contract. The companion document
  [registry-http-api.md](../../reference/registry-http-api/) is
  **informational** in v0.1 and is reserved for normative inclusion
  in v0.2 once independent server implementations exist.
- The on-disk format of any third-party plugin distribution channel
  (such as the Claude-Code plugin marketplace). The OPTIONAL
  `marketplace:` block in the manifest is normative as **input**;
  the generated `marketplace.json` artifact is governed externally
  and tracked additively.
- The runtime API of any harness or IDE. OpenAPM describes what is
  written to disk and where; it does not describe what a harness
  reads from that disk.
- Account, billing, identity, or audit-log semantics for hosted
  registries.
- Publisher identity, signature verification, and attestation
  envelopes are out of scope for OpenAPM v0.1 and reserved for v0.2.
  See [Section 10.12](#1012-publisher-provenance-and-attestations-reserved-for-v02).
- Reproducible-build determinism of registry archives
  (mtime/uid/gid normalisation, tar member ordering beyond
  filesystem-natural order) is out of scope for OpenAPM v0.1 and
  reserved for v0.2.
- Version withdrawal (yank, deprecate, supersede) for published
  versions is out of scope for OpenAPM v0.1 and reserved for v0.2.
  See [Section 7.9](#79-version-withdrawal-reserved-for-v02).
- Workspace / monorepo composition (shared lockfile across sibling
  packages, intra-workspace resolve-to-local, workspace publish) is
  out of scope for OpenAPM v0.1 and reserved for v0.2; current
  monorepo usage is supported via local-path dependencies per
  [Section 4.3.5](#435-local-path-dependencies). See
  [Section 4.8](#48-workspaces-reserved-for-v02).
- The v0.1 consumer-side integrity model is self-sufficient against
  a non-conforming registry (hash-verify-before-extract,
  re-verify-on-frozen); residual gaps (availability,
  version-immutability, publisher identity) are closed normatively
  in v0.2.

### 1.2 Relationship to existing APM reference documentation

The reference pages under `docs/src/content/docs/reference/` are
**non-normative companions** to this specification:

| Companion page                                                                       | Role                              |
|--------------------------------------------------------------------------------------|-----------------------------------|
| [`manifest-schema.md`](../../reference/manifest-schema/)                              | Field reference and examples for [Section 4](#4-manifest-format-apmyml). |
| [`lockfile-spec.md`](../../reference/lockfile-spec/)                                  | Lifecycle table and examples for [Section 5](#5-lockfile-format-apmlockyaml). |
| [`policy-schema.md`](../../reference/policy-schema/)                                  | Field reference and examples for [Section 6](#6-policy-format-apm-policyyml). |
| [`primitive-types.md`](../../reference/primitive-types/)                              | Conceptual model for [Section 8](#8-primitive-type-system-and-target-matrix). |
| [`package-types.md`](../../reference/package-types/)                                  | Producer-side layout decision tree for [Section 8](#8-primitive-type-system-and-target-matrix). |
| [`targets-matrix.md`](../../reference/targets-matrix/)                                | Per-target support matrix; informational supplement to [Section 8](#8-primitive-type-system-and-target-matrix). |
| [`registry-http-api.md`](../../reference/registry-http-api/)                          | Reserved; v0.2 normative surface, v0.1 informational only. |

Where a companion describes behaviour, this specification binds it.
Where a companion and this specification disagree, **this specification
wins**. Editorial notes in this document call out reconciliations
between the companion corpus and the implementation.

### 1.3 Document conventions

- OpenAPM v0.1 carries **95 normative statements** indexed in
  [Appendix C](#appendix-c-index-of-normative-statements).
- All on-disk files defined by this specification are **YAML 1.2**
  parsed under the safe subset defined in
  [req-mf-020](#req-mf-020). A complete YAML safe-subset profile
  (anchor/alias handling, tag whitelist, octal-coercion treatment)
  is reserved for v0.2.
- Field names are `snake_case` unless they mirror an external
  contract (such as `tagPattern` in the OPTIONAL marketplace block).
- All examples in this document are ASCII-only. Implementations
  encountering non-ASCII bytes in OpenAPM files MUST treat them as
  UTF-8 and either preserve them on round-trip or reject them at
  parse time with a diagnostic. Unicode normalisation (NFC), IDNA
  for hosts, bidi-safe rendering, and locale-of-diagnostics are
  out of scope for v0.1 and reserved for v0.2 (see
  [Section 1.4](#14-terminology-preliminaries)).
- "Implementation" means any program that produces, consumes, or
  evaluates an OpenAPM file. A given implementation MAY claim more
  than one conformance class.

### 1.4 Terminology preliminaries

OpenAPM v0.1 distinguishes two host-related concepts that earlier
drafts conflated:

- **Implementation-default host** -- the host an implementation uses
  when the manifest omits `default_host:` (see
  [Section 4.2.4](#424-default_host)). This is an implementation
  choice, not a spec mandate; the reference APM CLI's
  implementation-default host is `github.com`.
- **Wire-format host** -- the host literal that appears in a
  dependency identifier or lockfile entry. Canonical normalisation
  (see [req-mf-009](#req-mf-009)) strips the project's
  `default_host:` only.

**Internationalization considerations (reserved for v0.2).** v0.1
does not normatively specify IDNA normalisation for hosts, Unicode
normalisation form (NFC) for package or owner names, bidi-safe
rendering of identifiers, or diagnostic locale. v0.2 will close
this gap. v0.1 implementations SHOULD compare names byte-for-byte
after the canonical normalisation defined in
[req-mf-009](#req-mf-009).

---

## 2. Conventions

The key words "**MUST**", "**MUST NOT**", "**REQUIRED**", "**SHALL**",
"**SHALL NOT**", "**SHOULD**", "**SHOULD NOT**", "**RECOMMENDED**",
"**MAY**", and "**OPTIONAL**" in this document are to be interpreted
as described in BCP 14
([RFC 2119](https://datatracker.ietf.org/doc/html/rfc2119),
[RFC 8174](https://datatracker.ietf.org/doc/html/rfc8174)) when, and
only when, they appear in all capitals.

Lowercase variants of these words ("must", "should", and so on) carry
no normative weight and are descriptive prose.

Every normative statement in this document carries a stable identifier
of the form `req-<group>-<NNN>` (for example `req-mf-001`) anchored
directly above the statement. These identifiers are stable across
errata; a renumbering requires a minor version bump as defined in
[Section 9.2](#92-breaking-vs-non-breaking-change-definition).

Conformance classes are defined normatively in
[Section 11.1](#111-conformance-classes-normative); the four roles are
**Producer**, **Consumer**, **Registry** (one MUST applies in
v0.1; the wire contract remains reserved), and **Governance**.
An implementation MUST declare which conformance class(es) it
claims when asserting OpenAPM conformance (see
[Section 11.2](#112-how-to-claim-conformance)).

---

## 3. Terminology

This section defines the terms used throughout this document. Where a
term has a separate normative definition (for example "primitive
type"), the definition section is cross-linked.

| Term | Definition |
|---|---|
| **Manifest** | The `apm.yml` file at the root of a package. Defined in [Section 4](#4-manifest-format-apmyml). |
| **Lockfile** | The `apm.lock.yaml` file at the root of a project. Defined in [Section 5](#5-lockfile-format-apmlockyaml). |
| **Policy** | An `apm-policy.yml` file evaluated by a Governance implementation. Defined in [Section 6](#6-policy-format-apm-policyyml). |
| **Package** | A unit identified by a manifest (`apm.yml`) or by a recognised package layout (see [Section 8.1](#81-primitive-types)). |
| **Primitive** | A typed unit of agent configuration (instruction, prompt, agent, skill, command, hook, or mcp server). Defined in [Section 8.1](#81-primitive-types). |
| **Target** | A named runtime harness (for example `copilot`, `claude`, `cursor`). Defined in [Section 8.4](#84-target-detection-signals-normative). |
| **Deploy directory** | The on-disk root under which a target's primitives are placed by `apm install`. Defined in [Section 8.5](#85-deploy-directory-contract-normative). |
| **Direct dependency** | A dependency declared in the consumer's own `apm.yml`. |
| **Transitive dependency** | A dependency declared in the `apm.yml` of a resolved package, not in the consumer's own `apm.yml`. |
| **Virtual package** | A dependency targeting a subdirectory or file within a repository rather than the whole repository. Defined in [Section 4.3.3](#433-virtual-packages). |
| **Registry** | A remote service that serves package archives over HTTP. The wire contract is reserved for v0.2. |
| **git-semver** | A dependency form whose `ref:` is a semver range matched against remote git tags. Defined in [Section 7.3](#73-git-semver-resolution). |
| **Constraint** | The version selector recorded for a dependency (a semver range, a literal tag, a branch name, a commit SHA, or `None`). |
| **Drift** | A divergence between the lockfile and either the manifest (declaration drift) or the deployed files on disk (integrity drift). |
| **Self-entry** | The synthesized lockfile entry that accounts for primitives the project itself contributes. Defined in [Section 5.3](#53-self-entry-semantics). |
| **Frozen install** | An install operation that refuses to mutate the lockfile and fails on any missing pin. Defined in [Section 5.5](#55-drift-and-integrity-model). |
| **Host class** | The equivalence set of network hosts that share a single credential scope (see [Section 10.3](#103-token-leakage-across-hosts)). Two hosts are in the same class **iff** their registrable domain (the eTLD+1 per the Public Suffix List) is identical, OR they are explicitly aliased via `registries.<name>.aliases:` (see [Section 4.2.3](#423-registries)). For example, `github.contoso.com` shares a host class with `contoso.com`, not with `github.com`. |
| **Implementation-default host** | The host an implementation uses when the manifest omits `default_host:`. The choice is implementation-defined; see [Section 1.4](#14-terminology-preliminaries). |
| **Wire-format host** | The host literal as it appears in a dependency identifier or lockfile entry after canonical normalisation. |
| **Hash envelope** | A digest serialised as `<algo>:<hex>` (for example `sha256:abcd...`). See [req-lk-016](#req-lk-016). |
| **Conformance class** | One of the four roles defined in [Section 11.1](#111-conformance-classes-normative). |
| **Conformant** | Satisfies all MUST-level requirements for a claimed conformance class. |
| **Conforming file** | An OpenAPM file that parses without error under the rules of [Sections 4](#4-manifest-format-apmyml)-[6](#6-policy-format-apm-policyyml). |

---

## 4. Manifest format (apm.yml)

### 4.1 Document structure and required fields

The manifest is a single YAML 1.2 document located at the project root,
filename `apm.yml`.

<a id="req-mf-001"></a>
**[req-mf-001]** A conforming **producer** implementation MUST emit a
manifest whose top-level document node is a YAML 1.2 mapping. A
conforming **consumer** implementation MUST reject any manifest whose
top-level node is not a mapping, with a diagnostic naming the file.

<a id="req-mf-002"></a>
**[req-mf-002]** A conforming **producer** implementation MUST include
a top-level field `name` whose value is a non-empty string.

<a id="req-mf-003"></a>
**[req-mf-003]** A conforming **producer** implementation MUST include
a top-level field `version` whose value is a string.

<a id="req-mf-004"></a>
**[req-mf-004]** A conforming **producer** implementation SHOULD emit
a `version` value matching the official semver 2.0.0 reference
regular expression (semver 2.0.0 Section 9):

```
^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$
```

A conforming **consumer** SHOULD emit a non-blocking diagnostic
when `version` does not match this pattern. Numeric-looking version
strings MUST be quoted in YAML to prevent integer/float coercion.

<a id="req-mf-006"></a>
**[req-mf-006]** A conforming **consumer** implementation MUST
preserve unknown top-level keys when rewriting the manifest, so that
manifests authored against a later revision of this specification
round-trip through an older consumer without data loss.

<a id="req-mf-020"></a>
**[req-mf-020]** A conforming **consumer** implementation MUST parse
manifest, lockfile, and policy documents under the YAML safe
subset: (a) scalars are strings unless explicitly typed via the
canonical YAML 1.2 tags `!!int`, `!!float`, or `!!bool`; (b)
`&anchor` / `*alias` constructs MUST be rejected with a diagnostic;
(c) custom (non-`!!`) tags MUST be rejected; (d) YAML 1.1 octal
coercion (`0NN` interpreted as base-8) MUST NOT be applied. The
complete safe-subset profile is reserved for v0.2; v0.1 implementations
MUST at minimum enforce clauses (a)-(d).

<a id="req-ext-001"></a>
**[req-ext-001]** A conforming **consumer** implementation MUST
treat any mapping key matching the pattern `x-[a-z][a-z0-9-]*` at
any nesting level of a manifest, lockfile, or policy document as a
**vendor-extension key**. Vendor-extension keys MUST be ignored
during semantic interpretation, MUST NOT cause parse-time errors,
and MUST be preserved byte-equivalent on round-trip. The same rule
applies to vendor-extension keys inside `dependencies.apm[]`,
`dependencies.mcp[]`, lockfile per-entry mappings, and policy
sub-blocks. See [Section 4.6.2](#462-vendor-extensions).

<a id="req-ext-002"></a>
**[req-ext-002]** This specification and all future revisions of
OpenAPM MUST NOT define normative keys beginning with the prefix
`x-`. The namespace is reserved exclusively for vendor extensions.

A minimal conforming manifest:

```yaml
name: my-project
version: 1.0.0
```

### 4.2 Field reference

The manifest top-level fields are:

| Field           | Required | Type                                                                                          |
|-----------------|----------|-----------------------------------------------------------------------------------------------|
| `name`          | yes      | string                                                                                        |
| `version`       | yes      | string (semver 2.0.0 per [req-mf-004](#req-mf-004))                                           |
| `description`   | no       | string                                                                                        |
| `author`        | no       | string                                                                                        |
| `license`       | no       | string (SPDX identifier RECOMMENDED)                                                          |
| `default_host`  | no       | string; see [Section 4.2.4](#424-default_host) and [req-mf-019](#req-mf-019)                  |
| `target`        | no       | string or list of strings (see [Section 4.2.1](#421-target))                                  |
| `type`          | no       | string (advisory, see [Section 4.2.2](#422-type-advisory))                                    |
| `scripts`       | no       | mapping `string -> string`                                                                    |
| `includes`      | no       | literal `auto` or list of paths                                                               |
| `registries`    | no       | mapping; see [Section 4.2.3](#423-registries)                                                 |
| `dependencies`  | no       | mapping with OPTIONAL keys `apm`, `mcp`, `conflict_resolution`                                |
| `devDependencies` | no     | mapping with OPTIONAL keys `apm`, `mcp`                                                       |
| `compilation`   | no       | mapping                                                                                       |
| `policy`        | no       | mapping; consumer-side policy hooks                                                           |
| `marketplace`   | no       | mapping; producer authoring block, see [Section 4.7](#47-marketplace-authoring-block-normative-input) |
| `x-<name>`      | no       | vendor extension, see [Section 4.6.2](#462-vendor-extensions)                                 |

#### 4.2.1 `target`

The canonical set of `target` identifiers registered by this
specification at v0.1 is:

```
copilot, claude, cursor, codex, gemini, opencode, windsurf, agent-skills, all
```

The legacy aliases `vscode` and `agents` MAY appear in input manifests
and MUST be normalised to `copilot` when the manifest is rewritten.
The internal fallback value `minimal` MUST NOT be set explicitly in a
manifest; it is reserved for the auto-detection fallback described in
[Section 8.4](#84-target-detection-signals-normative). In an
auto-detect context, `minimal` denotes the no-target-detected profile
that emits `AGENTS.md` only; `all` denotes the union of every
registered target.

Concrete per-target detection signals and deploy roots are documented
in the non-normative companion **"OpenAPM Target Registry v0.1"**
(see [Section 8](#8-primitive-type-system-and-target-matrix)); the
companion holds the table contents and is amended additively; this
specification binds the schema only.

<a id="req-mf-005"></a>
**[req-mf-005]** A conforming **producer** implementation MUST reject
any value of `target` (or any element of a `target` list) that is not
either: (a) a member of the canonical set or a recognised alias; OR
(b) a vendor-extension identifier matching
`x-[a-z][a-z0-9-]*-[a-z][a-z0-9-]*` (the
`x-<vendor>-<name>` form). The diagnostic MUST name the offending
token.

<a id="req-tg-004"></a>
**[req-tg-004]** A conforming **consumer** implementation MUST accept
target identifiers matching `x-[a-z][a-z0-9-]*-[a-z][a-z0-9-]*` at
parse time and MUST route detection, deployment, and conformance
evaluation for such identifiers to a vendor-registered handler. In
the absence of a registered handler, the consumer MUST emit a
diagnostic naming the unsupported identifier and MUST NOT silently
ignore the entry. Vendors MAY register new targets without spec
amendment via this namespace.

> **Editorial note.** Editorial reconciliation between the canonical
> set above and the legacy aliases is consolidated in
> [Appendix E](#appendix-e-editorial-reconciliation-notes).

#### 4.2.2 `type` (advisory)

The `type` field MAY take one of the values `instructions`, `skill`,
`hybrid`, or `prompts`. Its semantic content is **advisory** in v0.1:
package behaviour is driven by the on-disk layout recognised in
[Section 8.1](#81-primitive-types), not by this field. Future
revisions MAY assign behavioural meaning; conformant consumers MUST
NOT reject a manifest solely on the basis of the `type` field's value
when that value is one of the four listed above. Editorial
reconciliation with the companion is in
[Appendix E](#appendix-e-editorial-reconciliation-notes).

#### 4.2.3 `registries`

The `registries` block MAY declare REST-based registries the project
consumes. The block is OPTIONAL; absence of the block means
git-resolution-only.

<a id="req-mf-014"></a>
**[req-mf-014]** A conforming **producer** implementation MUST ensure
that every `registries.<name>.url` value begins with `https://` or
`http://`. Other URL schemes MUST be rejected at parse time.

<a id="req-mf-015"></a>
**[req-mf-015]** A conforming **producer** implementation MUST reject
unknown keys inside any `registries.<name>` entry at parse time,
**except** for vendor-extension keys matching `x-[a-z][a-z0-9-]*`
(see [req-ext-001](#req-ext-001)). This constraint is a typo guard
and prevents silent acceptance of mistyped keys.

<a id="req-sc-006"></a>
**[req-sc-006]** A conforming **consumer** implementation MUST treat
any `registries.<name>.url` that uses the `http://` scheme as a
parse-time error **unless** one of the following is true: (a) the
entry sets `insecure: true` explicitly; OR (b) the host is the
loopback address (`127.0.0.0/8`, `::1`) or an RFC 1918 private
address (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`). The
diagnostic MUST name the offending registry.

Optional fields on a `registries.<name>` mapping:

| Field      | Type      | Notes                                                                 |
|------------|-----------|-----------------------------------------------------------------------|
| `url`      | string    | REQUIRED; scheme constrained by [req-mf-014](#req-mf-014).            |
| `insecure` | boolean   | When `true`, allows `http://`. See [req-sc-006](#req-sc-006).         |
| `aliases`  | string[]  | Additional host names that share this registry's host class. See [Section 10.3](#103-token-leakage-across-hosts). |

Example:

```yaml
registries:
  internal:
    url: https://artifactory.example.com/artifactory/api/skills/internal
    aliases:
      - mirror.example.com
  default: internal
```

#### 4.2.4 `default_host`

The OPTIONAL `default_host:` top-level field selects the host that
canonical normalisation strips from shorthand dependency identifiers
(see [Section 4.3.4](#434-canonical-normalisation-writer-requirements)).
When omitted, the consumer uses its implementation-default host;
this specification does not mandate `github.com` or any other value
for the implementation-default.

<a id="req-mf-019"></a>
**[req-mf-019]** A conforming **consumer** implementation that
encounters a `default_host:` value MUST treat that value as the only
host stripped by canonical normalisation per
[req-mf-009](#req-mf-009). When the manifest omits `default_host:`,
the consumer MAY apply its implementation-default host but MUST
document that choice in its conformance statement (see
[Section 11.2](#112-how-to-claim-conformance)). A consumer MUST NOT
strip any host other than the one selected by `default_host:` or the
declared implementation-default.

### 4.3 Dependencies block (apm + mcp)

The OPTIONAL `dependencies` block has two OPTIONAL list-valued keys:
`apm` for agent primitive packages, and `mcp` for MCP servers.
Implementations MAY encounter unknown sibling keys (such as future
dependency kinds) and MUST preserve them on rewrite per
[req-mf-006](#req-mf-006).

#### 4.3.1 String form

Each `dependencies.apm` entry MAY be a string conforming to the
following grammar (RFC 5234 ABNF):

```
dependency       = url-form / shorthand-form / local-path-form

url-form         = url-scheme clone-url
url-scheme       = "https://" / "http://" / "ssh://git@" / "git@"
clone-url        = host [ ":" port ] "/" owner "/" repo
                   [ "/" virtual-path ] [ "#" ref ]

shorthand-form   = [ host "/" ] owner "/" repo
                   [ "/" virtual-path ] [ "#" ref ]

local-path-form  = local-prefix path-tail
local-prefix     = "./" / "../" / "/" / "~/" / ".\" / "..\" / "~\"
path-tail        = 1*pchar

host             = 1*( ALPHA / DIGIT / "-" / "." )
port             = 1*DIGIT       ; range 1-65535
owner            = 1*( ALPHA / DIGIT / "-" / "_" )
repo             = 1*( ALPHA / DIGIT / "-" / "_" / "." )
virtual-path     = segment *( "/" segment )
segment          = 1*( ALPHA / DIGIT / "-" / "_" / "." )
ref              = 1*VCHAR
pchar            = ALPHA / DIGIT / "/" / "\" / ":" / "." / "-" / "_" / "~"
```

`clone-url` MAY include a `:port` segment on `https://`, `http://`,
and `ssh://git@` forms. `local-path-form` paths beginning with a
backslash-prefixed `local-prefix` are normalised to POSIX form on
read; see [Section 4.3.4](#434-canonical-normalisation-writer-requirements).

ABNF productions are interpreted per RFC 5234. `ALPHA`, `DIGIT`, and
`VCHAR` are the core rules from RFC 5234 Appendix B.1.

<a id="req-mf-007"></a>
**[req-mf-007]** A conforming **consumer** implementation MUST parse
string-form `dependencies.apm` entries per the grammar above.
Implementations MUST reject any string that does not satisfy one of
the three productions, with a diagnostic identifying the offending
entry.

#### 4.3.2 Object form

An object-form entry uses one of two identity keys, `git:` or `id:`,
and MUST NOT use both on the same entry.

| Field    | Required                                | Notes                                                                 |
|----------|-----------------------------------------|-----------------------------------------------------------------------|
| `git`    | yes for git-sourced; mutually excl. `id`| Clone URL or shorthand. Special value `parent` defined below.         |
| `id`     | yes for registry-sourced; mutually excl. `git` | `<owner>/<repo>` registry identity.                            |
| `registry` | no                                    | Registry name; defaults to project default if omitted.                |
| `version`| yes (registry form)                     | Opaque version selector; semver range when registry publishes semver. |
| `ref`    | no                                      | Branch, tag, semver range, or commit SHA (git form).                  |
| `path`   | no / yes (local form)                   | Subpath within repo, or local filesystem path.                        |
| `alias`  | no                                      | Local alias.                                                          |
| `skills` | no                                      | Skill-subset selection for skill collections (see [Section 8.1](#81-primitive-types)). |

<a id="req-mf-011"></a>
**[req-mf-011]** A conforming **consumer** implementation MUST reject
any object-form entry that sets both `id:` and `git:` on the same
entry. The diagnostic MUST name the entry and the conflicting keys.

<a id="req-mf-010"></a>
**[req-mf-010]** A conforming **consumer** implementation MUST treat
the literal sentinel `git: parent` as valid **only** inside a
transitively resolved package whose clone coordinates are known to
the resolver. The resolver MUST expand `parent` to the parent
package's `host`, `repo_url`, and resolved `ref`, with `virtual_path`
taken from `path`. The literal `parent` MUST NOT appear in the
lockfile as durable identity (`repo_url` or `source`).

#### 4.3.3 Virtual packages

A dependency MAY target a subdirectory or a file within a repository
rather than the whole repository.

<a id="req-mf-008"></a>
**[req-mf-008]** A conforming **consumer** implementation MUST
classify virtual packages by **file extension only** and MUST NOT
infer kind from path segments. A `virtual_path` ending in
`.prompt.md`, `.instructions.md`, `.agent.md`, or `.chatmode.md` is a
file; any other path is a subdirectory. On-disk shape of a
subdirectory virtual package is resolved by probing for `apm.yml`
first.

#### 4.3.4 Canonical normalisation (writer requirements)

<a id="req-mf-009"></a>
**[req-mf-009]** A conforming **consumer** implementation MUST
normalise dependency entries to canonical form when rewriting the
manifest. The canonical form strips **only** the host that matches
the project's `default_host:` value (per
[req-mf-019](#req-mf-019)) or, if `default_host:` is omitted, the
consumer's declared implementation-default host. SCP-style git
URLs (`git@host:owner/repo.git`) and `https://` URLs targeting the
selected default host MUST be normalised to the shorthand form
`owner/repo`. Non-default hosts MUST retain their FQDN. A consumer
MUST NOT hard-code stripping of any specific host literal; the
selection is configured per project.

#### 4.3.5 Local-path dependencies

<a id="req-mf-016"></a>
**[req-mf-016]** A conforming **consumer** implementation MUST
recognise dependency strings beginning with `./`, `../`, `/`, `~/`,
`.\`, `..\`, or `~\` as local-path entries. The resolver MUST refuse
any local-path entry whose normalised form contains `..` segments
that would escape the project root, with a diagnostic naming the
offending path.

#### 4.3.6 MCP dependencies

The OPTIONAL `dependencies.mcp` list declares MCP servers. Each entry
is either a registry string or an object. Object-form fields are
defined in [`manifest-schema.md` Section 4.2](../../reference/manifest-schema/#42-dependenciesmcp----listmcpdependency).

<a id="req-mf-012"></a>
**[req-mf-012]** A conforming **consumer** implementation MUST reject
any self-defined MCP server entry (one where `registry: false`) that:
(a) omits `transport`; (b) sets `transport: stdio` but omits
`command`; (c) sets `transport` to `http`, `sse`, or `streamable-http`
but omits `url`. When `transport: stdio` is in effect, the `command`
value MUST be a single binary path with no embedded whitespace
**unless** the entry also supplies an `args` key (including an
explicit empty list); a path containing spaces without an `args`
sibling MUST be rejected at parse time.

### 4.4 devDependencies

The OPTIONAL `devDependencies` block has the same structure as
`dependencies`. Entries declared under `devDependencies` are
installed locally but excluded from packed plugin bundles produced
by the producer toolchain.

### 4.5 Variable references in MCP env/headers

Values inside `mcp[].env` and `mcp[].headers` MAY contain three
placeholder syntaxes:

| Syntax            | Source                | Resolution                                                              |
|-------------------|-----------------------|-------------------------------------------------------------------------|
| `${VAR}`          | host environment      | Normalised to `${env:VAR}` for native interpolation; resolved at install for others. |
| `${env:VAR}`      | host environment      | Passed through where natively supported; resolved at install otherwise. |
| `${input:<id>}`   | interactive prompt    | Native where supported; otherwise the placeholder MUST NOT be silently rendered as literal text. |

GitHub Actions templates (`${{ ... }}`) MUST be left untouched.

<a id="req-mf-013"></a>
**[req-mf-013]** A conforming **consumer** implementation MUST
resolve `${VAR}`, `${env:VAR}`, and `${input:<id>}` placeholders per
the dispatch matrix above and MUST NOT emit a generated config file
in which an unsupported placeholder is silently passed through as
literal text. When an unsupported placeholder is encountered for the
active target, the consumer MUST emit a diagnostic and MAY refuse to
write the generated config.

### 4.6 Manifest extension surfaces

#### 4.6.1 `policy` (consumer-side controls)

The OPTIONAL `policy` block records consumer-side controls for
governance integration (such as a pinned hash of the leaf
`apm-policy.yml`). Its sub-fields are documented in the companion
page and are enforced normatively in [Section 6](#6-policy-format-apm-policyyml).
The manifest `policy:` block pins the **discovered** policy bytes
by hash, defending against MITM or registry-side rewrite between
discovery and evaluation; see [Section 6.1](#61-loading-and-discovery).

<a id="req-mf-018"></a>
**[req-mf-018]** A conforming **consumer** implementation MUST accept
only `sha256`, `sha384`, or `sha512` as the value of
`policy.hash_algorithm`. Values of `md5`, `sha1`, or any other
algorithm MUST be rejected at parse time. When `policy.hash` is set
and `policy.hash_algorithm` is omitted, the algorithm MUST be
inferred from the `<algo>:` prefix of the digest value. The
algorithm allow-list applies to the digest of the discovered policy
bytes, not to a recursive hash of the manifest field itself.

#### 4.6.2 Vendor extensions

OpenAPM reserves the key prefix `x-` (lowercase ASCII) for
vendor-defined extensions at every mapping level of every document
this specification defines: manifest top-level, every nested
mapping within `dependencies`, `devDependencies`, `registries`,
`policy`, `compilation`, `marketplace`; lockfile top-level and
per-entry mappings (see [Section 5.2](#52-per-entry-fields)); and
policy top-level and every nested mapping (see
[Section 6](#6-policy-format-apm-policyyml)).

Key shape is `x-[a-z][a-z0-9-]*`. Vendors SHOULD further namespace
their keys as `x-<vendor>-<name>` (for example
`x-acme-telemetry`) to avoid collision between independent vendors.

[req-ext-001](#req-ext-001) and [req-ext-002](#req-ext-002) govern
reader and writer behaviour for this namespace. Round-trip
preservation is the load-bearing guarantee: a manifest authored
with `x-acme-telemetry:` MUST survive a `read -> mutate -> write`
cycle through any conforming consumer byte-equivalent, even if the
consumer ascribes no semantics to the key.

### 4.7 Marketplace authoring block (normative input)

The OPTIONAL `marketplace` block declares the producer's marketplace
authoring metadata. The on-disk shape of the `marketplace.json`
artifact that a producer toolchain emits from this block is
**outside the scope** of this specification; the input format defined
here is normative.

<a id="req-mf-017"></a>
**[req-mf-017]** A conforming **producer** implementation MUST
validate every `marketplace.packages[].source` value against the
following rules and MUST reject any entry that fails them at parse
time: (a) `..` path segments are refused; (b) URL forms with
userinfo (`user@host`), ports, or query strings are refused;
(c) URL schemes other than `https://` are refused for remote
sources; (d) local sources MUST begin with `./`.

### 4.8 Workspaces (reserved for v0.2)

Workspace / monorepo composition (shared lockfile across sibling
packages, intra-workspace resolve-to-local, workspace publish) is
**out of scope for v0.1 and reserved for v0.2**. Current monorepo
usage is fully supported in v0.1 via local-path dependencies per
[Section 4.3.5](#435-local-path-dependencies). A future v0.2
surface will reserve a top-level `workspaces:` glob list, declare
the root `apm.yml` lockfile as single source of truth, and
normatively pin intra-workspace deps to local paths.

<a id="req-mf-021"></a>
**[req-mf-021]** In v0.1, a conforming **producer** MUST NOT
declare a top-level `workspaces:` key in `apm.yml`. A conforming
**consumer** encountering a top-level `workspaces:` key in a v0.1
manifest MUST emit a non-blocking diagnostic naming the key as
reserved for v0.2 and MUST NOT attach any semantics to its value.
The diagnostic MUST NOT fail install.

### 4.9 Conformance requirements (manifest)

This section's normative statements are:

- Producer: [req-mf-001](#req-mf-001), [req-mf-002](#req-mf-002),
  [req-mf-003](#req-mf-003), [req-mf-005](#req-mf-005),
  [req-mf-014](#req-mf-014), [req-mf-015](#req-mf-015),
  [req-mf-017](#req-mf-017), [req-mf-021](#req-mf-021).
- Producer (SHOULD): [req-mf-004](#req-mf-004).
- Consumer: [req-mf-006](#req-mf-006), [req-mf-007](#req-mf-007),
  [req-mf-008](#req-mf-008), [req-mf-009](#req-mf-009),
  [req-mf-010](#req-mf-010), [req-mf-011](#req-mf-011),
  [req-mf-012](#req-mf-012), [req-mf-013](#req-mf-013),
  [req-mf-016](#req-mf-016), [req-mf-018](#req-mf-018),
  [req-mf-019](#req-mf-019), [req-mf-020](#req-mf-020),
  [req-mf-021](#req-mf-021), [req-ext-001](#req-ext-001),
  [req-ext-002](#req-ext-002),
  [req-tg-004](#req-tg-004), [req-sc-006](#req-sc-006).

---

## 5. Lockfile format (apm.lock.yaml)

### 5.1 Top-level structure

The lockfile is a single YAML 1.2 document at the project root,
filename `apm.lock.yaml`. It records the pinned resolved state of
every dependency the consumer has resolved from the manifest, plus
the set of files the consumer itself contributes (the self-entry).

<a id="req-lk-001"></a>
**[req-lk-001]** A conforming **consumer** implementation MUST emit a
lockfile whose top-level document node is a YAML 1.2 mapping with at
minimum the keys `lockfile_version` (string) and `dependencies`
(list). Additional top-level keys defined by this specification are
`generated_at`, `apm_version`, `mcp_servers`, `mcp_configs`,
`local_deployed_files`, `local_deployed_file_hashes`, and
`attestations` (the last reserved for v0.2 per
[Section 10.12](#1012-publisher-provenance-and-attestations-reserved-for-v02)).
Vendor-extension top-level keys (`x-*`) are permitted per
[req-ext-001](#req-ext-001).

Example (informative, minimal):

```yaml
lockfile_version: "1"
generated_at: "2026-05-10T20:14:00+00:00"
apm_version: "0.6.4"
dependencies:
  - repo_url: github.com/octocat/example
    resolved_commit: "7f3c9a4d2e1b8c7f0a9e6d5c4b3a2918f7e6d5c4"
    resolved_ref: v1.2.0
    tree_sha256: "sha256:a1b2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff00"
    depth: 1
    deployed_files:
      - .github/instructions/example.instructions.md
```

### 5.2 Per-entry fields

Each element of `dependencies` describes one resolved package. The
following fields are recognised by this specification; producers and
consumers MUST emit only fields whose values are set and MUST preserve
unknown fields on round-trip. Field availability is **monotonic** in
`lockfile_version`: a field defined here is valid in both `"1"` and
`"2"`. The `"v2 only"` annotation used in earlier drafts is removed
(see [req-lk-002](#req-lk-002)).

| Field                     | Notes                                                                           |
|---------------------------|---------------------------------------------------------------------------------|
| `repo_url`                | Canonical repo identity. REQUIRED for git-sourced entries.                       |
| `host`                    | FQDN when not inferable from `repo_url`.                                        |
| `port`                    | Non-standard port. Validated to `1..65535` on read.                             |
| `registry_prefix`         | Path prefix when resolved via registry proxy.                                   |
| `resolved_ref`            | User-supplied ref (branch, tag, SHA).                                           |
| `resolved_commit`         | Exact 40-character lowercase hexadecimal commit SHA-1.                          |
| `tree_sha256`             | Hash envelope (`sha256:<hex>`) over the canonicalised git tree. See [req-lk-015](#req-lk-015). |
| `version`                 | Resolved version selector (semver string for semver-bearing sources); for other sources MAY carry a self-asserted version per [req-lk-019](#req-lk-019). |
| `virtual_path`            | Subpath inside repo for virtual packages.                                       |
| `is_virtual`              | Boolean.                                                                        |
| `depth`                   | Tree depth (0 = self, 1 = direct, >1 = transitive).                             |
| `resolved_by`             | `repo_url` of the parent that pulled this transitive dep.                       |
| `package_type`            | One of `apm_package`, `skill_bundle`, etc.                                      |
| `skill_subset`            | Selected skill names for `skill_bundle` packages.                               |
| `deployed_files`          | Project-relative paths the consumer wrote for this entry.                       |
| `deployed_file_hashes`    | `path -> <algo>:<hex>` for the files in `deployed_files`.                       |
| `source`                  | `local` for path deps, `registry` for registry deps; absent for git.            |
| `resolved_url`            | Registry archive download URL (advisory; see [req-rs-009](#req-rs-009)).        |
| `resolved_hash`           | Hash envelope (`sha256:<hex>`) of the registry archive bytes. Trust anchor.     |
| `local_path`              | Original path for local deps.                                                   |
| `content_hash`            | Hash envelope (`sha256:<hex>`) of a local package's source tree.                |
| `is_dev`                  | True when declared under `devDependencies`.                                     |
| `constraint`              | git-semver: the original semver range from the manifest (verbatim).             |
| `resolved_tag`            | git-semver: the literal tag the range resolved to.                              |
| `resolved_at`             | git-semver: ISO 8601 UTC timestamp; advisory (see [Section 7.3](#73-git-semver-resolution)). |
| `name`                    | Self-asserted display/inventory name; non-identity (see [req-lk-019](#req-lk-019)). |
| `attestations`            | Reserved for v0.2 (publisher provenance).                                       |
| `x-<name>`                | Vendor extension (per [req-ext-001](#req-ext-001)).                             |

<a id="req-lk-003"></a>
**[req-lk-003]** A conforming **consumer** implementation MUST record
both `repo_url` and `resolved_commit` for every git-sourced
dependency entry. For every registry-sourced dependency entry the
consumer MUST instead record `resolved_url` and `resolved_hash`
(in addition to `repo_url`, which carries package identity).

<a id="req-lk-011"></a>
**[req-lk-011]** A conforming **consumer** implementation MUST omit
fields whose values are unset (no `null` placeholders) and MUST
preserve fields it does not recognise when round-tripping a lockfile.
This includes vendor-extension keys per [req-ext-001](#req-ext-001).

<a id="req-lk-012"></a>
**[req-lk-012]** A conforming **consumer** implementation MUST
compute `deployed_file_hashes` and `local_deployed_file_hashes` as
SHA-256 hash envelopes (`sha256:<hex-lowercase>`) of the deployed
file bytes as written to disk. Directory entries (paths ending in
`/`) MUST NOT have a hash entry. The hash envelope `<algo>:<hex>`
form defined by [req-lk-016](#req-lk-016) applies uniformly.

<a id="req-lk-013"></a>
**[req-lk-013]** A conforming **consumer** implementation MUST verify
the `resolved_hash` against the actual SHA-256 of the registry
archive bytes **before** extracting the archive to disk. On
mismatch, the install MUST fail closed with a diagnostic naming the
entry, the expected hash, and the actual hash, and MUST NOT extract
or partially extract the archive.

<a id="req-lk-014"></a>
**[req-lk-014]** A conforming **consumer** implementation MUST
preserve vendor-extension keys (`x-[a-z][a-z0-9-]*`) at every
mapping level of the lockfile -- top-level and per-entry -- on
round-trip. See [req-ext-001](#req-ext-001).

<a id="req-lk-019"></a>
**[req-lk-019]** A conforming **consumer** implementation MUST treat
the optional `name` and `version` fields of a dependency entry as
**self-asserted inventory metadata** only -- recorded to support
human-readable listing and audit reporting, never as a trust
anchor. A consumer MUST preserve both fields on round-trip per
[req-lk-011](#req-lk-011), and MUST NOT derive any identity,
deduplication, or frozen-replay decision from them; package
identity and replay derive solely from `repo_url`,
`resolved_commit`, `resolved_tag`/`constraint`, and the recorded
hash envelopes (see [req-lk-003](#req-lk-003),
[req-lk-008](#req-lk-008)). The presence of either field is
additive and MUST NOT change `lockfile_version` (both are valid in
`"1"` and `"2"`).

<a id="req-lk-016"></a>
**[req-lk-016]** A conforming **consumer** implementation MUST emit
hash values as `<algo>:<hex>` envelopes (for example
`sha256:abcd...`) in every position where this specification records
a digest: `resolved_hash`, `deployed_file_hashes` (each value),
`local_deployed_file_hashes` (each value), `content_hash`,
`tree_sha256`, and any future hash field. The `<algo>` token MUST
be one of `sha256`, `sha384`, or `sha512` per
[req-mf-018](#req-mf-018). Readers MUST accept bare 64-character
lowercase hexadecimal values as `sha256:<hex>` for v0.1
backward-compatibility; writers MUST emit the explicit envelope
form. Bare-hex reader-tolerance is retained for v0.1
backward-compat only. v0.2 will remove reader-tolerance and
require the `sha256:` envelope unconditionally. v0.1 Writers
SHOULD already emit the envelope on every hash field for forward
compatibility (this deprecation horizon supersedes earlier drafts
that left the bare-hex form open-ended).

<a id="req-lk-017"></a>
**[req-lk-017]** A conforming **consumer** implementation
executing a frozen install (see
[req-lk-006](#req-lk-006)) MUST re-verify every entry in
`deployed_file_hashes` and `local_deployed_file_hashes` against the
bytes written to disk and MUST fail closed on mismatch. The
diagnostic MUST name the offending path, the expected envelope, and
the observed envelope. The same re-verification MUST run on `apm
audit`.

### 5.3 Self-entry semantics

A project that ships its own primitives records the files it deploys
under `local_deployed_files` and `local_deployed_file_hashes` at the
top level. When the lockfile is loaded, consumers MAY synthesize an
in-memory virtual dependency entry keyed by `"."` for uniform
iteration over owned files. The synthesized entry MUST NOT be written
back to YAML; the flat `local_deployed_*` fields are the on-disk
source of truth.

The synthesized entry, when present in memory, has:

- `repo_url: <self>`
- `source: local`
- `local_path: "."`
- `depth: 0`
- `is_dev: true`

This isolation prevents the orphan-cleanup logic of one dependency
from removing files attributed to another (see
[Section 10.7](#107-unverified-content-cleanup-file-integrators)).

### 5.4 Lockfile versions (1, 2) and bumping rules

This specification defines two lockfile schema versions, `"1"` and
`"2"`. Both are valid on-disk formats; `"2"` is a strict superset of
`"1"`. Editorial reconciliation with the companion is consolidated
in [Appendix E](#appendix-e-editorial-reconciliation-notes).

<a id="req-lk-002"></a>
**[req-lk-002]** A conforming **consumer** implementation MUST set
`lockfile_version: "2"` when at least one entry in `dependencies`
has `source: registry`. When no entry has `source: registry`, the
consumer MAY emit either `"1"` or `"2"`. The version is
**monotonic**: once a consumer writes `lockfile_version: "2"` to a
given lockfile, subsequent rewrites of that lockfile by any
conforming consumer MUST NOT demote the version to `"1"`, even if
the registry-sourced entry is removed. A consumer SHOULD tolerate
reading either `"1"` or `"2"` regardless of which version it
prefers on write.

<a id="req-lk-004"></a>
**[req-lk-004]** A conforming **consumer** implementation MUST refuse
to operate on a lockfile whose `lockfile_version` value is not one
of the versions it recognises, with a diagnostic that explicitly
offers the user a choice of either upgrading the consumer or
regenerating the lockfile from the manifest.

### 5.5 Drift and integrity model

The lockfile is the contract `apm audit` validates the workspace
against.

<a id="req-lk-005"></a>
**[req-lk-005]** A conforming **consumer** implementation MUST treat
two lockfiles as semantically equivalent if they differ only in the
values of `generated_at` and `apm_version`. A no-op install
operation MUST NOT rewrite a lockfile whose only changed fields
would be these two. Consumers operating in privacy-sensitive
deployments MAY omit `generated_at` and `apm_version` entirely;
their absence MUST NOT affect content-equivalence comparison.
Consumers SHOULD expose a `--no-provenance` (or equivalent) flag
that suppresses these fields on write. Consumers SHOULD NOT include
`generated_at` or `apm_version` in lockfiles persisted by
deployments that have declared privacy sensitivity. When a
consumer writes a lockfile, the `dependencies` list MUST be
ordered ascending lexicographically by the tuple (`repo_url`,
`virtual_path`); entries without `virtual_path` sort as if
`virtual_path` were the empty string. Two lockfiles differing
only in entry order are semantically equivalent under this
requirement, but a write-back MUST canonicalise to the pinned
order so frozen-install diffs are stable across implementations.

<a id="req-lk-006"></a>
**[req-lk-006]** A conforming **consumer** implementation MUST
support a frozen-install mode in which the lockfile is never written
or rewritten and the install fails on any direct dependency for
which the lockfile has no pin. The frozen-install operation is
opt-in in v0.1 via `--frozen` (or equivalent); a future minor
revision will flip the default to "frozen when a lockfile is
present" (deferred to v0.x minor, see
[Section 9.2](#92-breaking-vs-non-breaking-change-definition)).

<a id="req-lk-018"></a>
**[req-lk-018]** A conforming **consumer** implementation SHOULD
default to frozen-install behaviour when the `CI` environment
variable is truthy (defined as: present and not the literal strings
`""`, `"0"`, `"false"`, case-insensitive). The user MAY override
the SHOULD-default via explicit non-frozen invocation. This
SHOULD-on-CI rule is a transition step toward the v0.x default
flip in [req-lk-006](#req-lk-006).

<a id="req-lk-007"></a>
**[req-lk-007]** A conforming **consumer** implementation SHOULD
skip the download step when a local checkout already matches the
locked commit. This optimisation MUST NOT change observable
behaviour; the post-install workspace state MUST be identical to a
fresh install.

### 5.6 git-semver fields (constraint, resolved_tag, resolved_at)

When the resolver picks a git tag from a semver range (see
[Section 7.3](#73-git-semver-resolution)), it records three
additional fields on the resolved entry. These fields are valid in
both `lockfile_version: "1"` and `"2"` (see [req-lk-002](#req-lk-002)).

<a id="req-lk-008"></a>
**[req-lk-008]** A conforming **consumer** implementation MUST
record `constraint`, `resolved_tag`, and `resolved_at` on every
git-semver lockfile entry. `constraint` MUST be the original semver
range from the manifest (verbatim). `resolved_tag` MUST be the
literal tag string the range resolved to. `resolved_at` MUST be an
ISO 8601 UTC timestamp of the resolution event and is advisory; it
MUST NOT be used as a tie-breaker in replay.

<a id="req-lk-009"></a>
**[req-lk-009]** A conforming **consumer** implementation MUST
replay a previously locked git-semver resolution (reusing the
locked `resolved_tag`) when the manifest's current semver constraint
is **equal** to the locked `constraint`. A different manifest
constraint MUST trigger re-resolution against the remote.

<a id="req-lk-010"></a>
**[req-lk-010]** A conforming **consumer** implementation MUST, when
performing an explicit update operation against a direct git-semver
dependency, purge the dependency's install path before re-resolving
so that the download callback re-runs even when the resolved tag is
unchanged. This guards against the regression where a cached
install path masks a re-resolution event.

#### 5.6.4 Git-source tree integrity hash

`resolved_commit` is a SHA-1 identifier and serves as a stable
content pointer in v0.1, but SHA-1 alone is below the 2026
collision-resistance floor and MUST NOT be relied on as the sole
integrity anchor. To close the SHA-1 gap, every git-sourced lockfile
entry carries a `tree_sha256` envelope.

The **canonical git tree hash** for `tree_sha256` is the SHA-256
over the following byte representation of the resolved tree:

```
<line>           ::= <mode-octal> SP <name-utf8> SP <blob-sha256-hex> LF
<canonical-tree> ::= <line>*   (entries sorted lexicographically by name)
```

`<mode-octal>` is the four- or six-digit POSIX-style file mode
(`100644`, `100755`, `120000`, `040000`); `<name-utf8>` is the
filesystem name; `<blob-sha256-hex>` is the lowercase hexadecimal
SHA-256 of the blob bytes. Subdirectories recurse: a subdirectory
entry uses mode `040000` and its blob-sha256 is itself the SHA-256
of the subdirectory's canonical tree representation. Lines are
LF-terminated and UTF-8 encoded; entries are sorted byte-wise by
name.

<a id="req-lk-015"></a>
**[req-lk-015]** A conforming **consumer** implementation MUST
compute and record `tree_sha256` for every git-sourced lockfile
entry. On a frozen install (see [req-lk-006](#req-lk-006)) and on
`apm audit`, the consumer MUST re-compute `tree_sha256` from the
working tree at `resolved_commit` and MUST fail closed when the
recomputed value differs from the recorded value. The diagnostic
MUST name the entry, the expected envelope, and the observed
envelope.

> **Editorial note.** `resolved_commit` is a SHA-1 identifier. The
> git project's SHA-1-to-SHA-256 object-format transition is
> ongoing; until SHA-1 collisions are observed in the wild against
> the git object format, `resolved_commit` is retained as the
> canonical pointer, with `tree_sha256` providing collision-resistant
> integrity. A future revision will track `resolved_commit_sha256`
> once git's SHA-256 object-format is widely deployed.

> **Editorial note.** Canonical-tree definition for local-path
> `content_hash` is reserved for v0.2; v0.1 consumers MAY use
> platform-native walk order but MUST document their choice in their
> conformance statement.

### 5.7 Conformance requirements (lockfile)

This section's normative statements are:

- Consumer: [req-lk-001](#req-lk-001), [req-lk-002](#req-lk-002),
  [req-lk-003](#req-lk-003), [req-lk-004](#req-lk-004),
  [req-lk-005](#req-lk-005), [req-lk-006](#req-lk-006),
  [req-lk-008](#req-lk-008), [req-lk-009](#req-lk-009),
  [req-lk-010](#req-lk-010), [req-lk-011](#req-lk-011),
  [req-lk-012](#req-lk-012), [req-lk-013](#req-lk-013),
  [req-lk-014](#req-lk-014), [req-lk-015](#req-lk-015),
  [req-lk-016](#req-lk-016), [req-lk-017](#req-lk-017).
- Consumer (SHOULD): [req-lk-007](#req-lk-007),
  [req-lk-018](#req-lk-018).

---

## 6. Policy format (apm-policy.yml)

### 6.1 Loading and discovery

A Governance implementation reads zero or one `apm-policy.yml` file
per install operation.

<a id="req-pl-001"></a>
**[req-pl-001]** A conforming **governance** implementation MUST
discover the active policy in the following priority order: (1) an
explicit `--policy <ref>` argument provided by the user; (2) any
registered **discovery provider** invoked in the configured order
(see [Section 6.1.1](#611-discovery-providers) and
[req-pl-011](#req-pl-011)). No other discovery mechanism MAY be
substituted for this order. When no provider yields a policy, no
policy is applied.

#### 6.1.1 Discovery providers

OpenAPM v0.1 defines discovery as a **pluggable extension point**.
A discovery provider is a named function that, given a project's
remote git context, MAY return a policy reference (URL or local
path). The reference initial provider registered by this
specification is `github-owner-dotgithub`, which fetches
`<owner>/.github/apm-policy.yml` from the same host as the
project's remote when the remote host matches the consumer's
implementation-default host. Additional providers (for example
`gitlab-project-yml`, `local-fallback`) are registered in the
non-normative **"OpenAPM Discovery Provider Registry v0.1"**
companion document.

<a id="req-pl-011"></a>
**[req-pl-011]** A conforming **governance** implementation MUST
expose discovery as a registered, ordered list of providers (the
default order is implementation-defined and MUST be documented in
the conformance statement). Providers MUST be selectable per
project via the policy `discovery:` block. A consumer MUST NOT
hard-code a host-specific discovery convention as the sole
discovery path. The `github-owner-dotgithub` provider is the
registered v0.1 default, NOT a specification-mandated discovery
mechanism.

<a id="req-pl-012"></a>
**[req-pl-012]** A conforming **governance** implementation that
needs to identify the "project's remote" for discovery MUST select
the git remote named `origin` if present; if `origin` is absent but
exactly one git remote exists, that remote MUST be used; if
multiple non-`origin` remotes exist, discovery MUST fail closed
with a diagnostic naming the candidates; if no remote exists, no
discovery is attempted.

#### 6.1.2 Interoperability note (informative)

Because the default discovery host is implementation-defined (see
[req-mf-019](#req-mf-019)), two conformant Consumers MAY yield
different policy discoveries for the same project when they
default to different hosts (for example, one defaults to
`github.com`, another to `gitlab.com`). Projects wanting
deterministic discovery across heterogeneous Consumers SHOULD (a)
declare `default_host:` explicitly in `apm.yml`, and (b) select
the discovery provider explicitly in `apm-policy.yml`'s
`discovery:` block. No new normative statement is added by this
note; it is interpretive guidance for the existing MUSTs in
[Section 6.1.1](#611-discovery-providers).

### 6.2 Enforcement modes

A policy declares an `enforcement` value of `off`, `warn`, or `block`.

| Mode    | Behaviour                                                                    |
|---------|------------------------------------------------------------------------------|
| `off`   | Policy is reported but never gates an operation.                             |
| `warn`  | Violations print warnings, exit code remains 0. Default.                     |
| `block` | Violations print errors and exit non-zero; install aborts before disk write. |

When `fetch_failure` is unset, the effective value is `warn`. The
default applies independently of the `enforcement` mode default and
is the value a conforming governance implementation MUST use when
the field is absent from the policy document.

<a id="req-pl-002"></a>
**[req-pl-002]** A conforming **governance** implementation MUST, when
the effective `enforcement` value is `block` and at least one
violation is detected, cause the install operation to abort
**before** any byte is written to disk for the proposed install.

<a id="req-pl-010"></a>
**[req-pl-010]** A conforming **governance** implementation MUST,
when the effective `fetch_failure` value is `block` and the policy
cannot be fetched or parsed, abort the install operation with a
fail-closed diagnostic. The same MUST hold when `fetch_failure` is
`block` and a transitively `extends:`'d policy fails to fetch.

### 6.3 Field reference

#### 6.3.1 `dependencies`

The `dependencies` policy block governs APM dependency declarations.

| Field                  | Semantic                                                                                  |
|------------------------|-------------------------------------------------------------------------------------------|
| `allow`                | List of patterns matched against `<owner>/<repo>`. Tri-state (see [Section 6.5](#65-allow-list--deny-list-tri-state-semantics)). |
| `deny`                 | Always wins over `allow`.                                                                 |
| `require`              | Packages every consumer manifest must include.                                            |
| `require_resolution`   | `project-wins` / `policy-wins` / `block` for required-package version conflicts. Default `project-wins` when unset. |
| `max_depth`            | Maximum transitive dependency depth. Default 50.                                          |
| `require_pinned_constraint` | When true, flags unbounded direct deps as violations.                                 |

<a id="req-pl-007"></a>
**[req-pl-007]** A conforming **governance** implementation that
honours `require_pinned_constraint: true` MUST flag, as a violation
routed through the active `enforcement` value, every **direct** APM
dependency whose constraint is one of: (a) no `ref` at all; (b) the
literal `*`; (c) a bare branch name; (d) an unbounded lower-bound
range such as `>=X.Y` without an upper bound. Transitive dependencies
MUST NOT be flagged by this rule.

<a id="req-pl-008"></a>
**[req-pl-008]** A conforming **governance** implementation that
honours `require_pinned_constraint: true` MUST classify the following
as **pinned** (no violation): (a) a 40-character commit SHA; (b) a
literal semver tag matching `v?\d+\.\d+\.\d+`; (c) a bounded semver
range (containing an upper bound); (d) a dependency with
`source: registry`; (e) a local-path dependency.

#### 6.3.2 `mcp`

The `mcp` block governs MCP server declarations, including
transitive ones.

#### 6.3.3 `compilation`

The `compilation` block governs `apm compile` outputs. Sub-field
semantics are documented in the companion `policy-schema.md` and
are non-normative in v0.1; the merge rules in
[Section 6.4](#64-inheritance-and-merge-rules) reference only the
field family `compilation.*`.

#### 6.3.4 `manifest`

The `manifest` block governs the shape of the consumer manifest
itself (required fields, scripts allow/deny, explicit-includes
requirement).

#### 6.3.5 `unmanaged_files`

The `unmanaged_files` block governs files in primitive target
directories that are not recorded in `apm.lock.yaml`. `directories`
names the managed primitive target trees to scan, `action` selects
the response (`ignore` | `warn` | `deny`), and `exclude` is a glob
allow-list of workspace paths to suppress from the report. Its glob
patterns are matched with the same pattern semantics as the policy
allow-list and deny-list fields (see
[Section 6.5](#65-allow-list--deny-list-tri-state-semantics)).

<a id="req-pl-015"></a>
**[req-pl-015]** A conforming **governance** implementation MUST,
when it evaluates policy over a populated primitive target tree (for
example during an audit), report unmanaged artifacts with the
following completeness guarantees:

(a) It MUST surface every file under a managed primitive target
directory that is neither recorded in `apm.lock.yaml` nor matched by
a configured `unmanaged_files.exclude` glob.

(b) Each surfaced path MUST be reported with the reason it is
unmanaged (that it is not tracked in `apm.lock.yaml`). Where the path
also matches a configured dependency or MCP deny pattern, the report
MUST additionally carry a supplemental conflict note naming that
pattern; this note is enrichment only and never itself causes a
tracked path to be surfaced. Where the primitive type is
determinable, the surfaced entry MUST carry its
inferred primitive type; where it is not determinable, the type
annotation MUST be omitted.

(c) A path matched by a configured `unmanaged_files.exclude` glob
MUST NOT be surfaced, even when it also matches a deny pattern.

This requirement governs the **completeness** of unmanaged-artifact
reporting only: whether a surfaced artifact yields a non-passing
audit result remains governed by `unmanaged_files.action` per the
merge table in [Section 6.4](#64-inheritance-and-merge-rules), so
req-pl-015 is not an enforcement claim.

#### 6.3.6 `security`

The `security` block declares opt-in supply-chain controls. Every
key defaults to `false` (off), so a policy that omits the block
behaves exactly as it did before these keys existed.

- `security.integrity.require_hashes` (boolean, default `false`):
  when `true`, the install operation fails closed if any resolved
  non-local dependency selected for installation lacks a recorded
  content hash (the `content_hash` lockfile field) in `apm.lock.yaml`.
  Local dependencies are exempt (they are anchored by deployed-file
  hashes, not a package digest).
- `security.audit.fail_on_drift` (boolean, default `false`): when
  `true`, a bare `apm audit` exits non-zero if lockfile drift is
  detected or the drift check cannot complete.

The normative behaviour for both keys is specified in
[Section 6.8](#68-integrity-controls-governance). Both merge by
logical OR across an `extends:` chain (see
[Section 6.4](#64-inheritance-and-merge-rules)): once any ancestor
sets a key to `true`, a descendant cannot relax it back to `false`.

### 6.4 Inheritance and merge rules

Policies form a chain via `extends:`.

<a id="req-pl-003"></a>
**[req-pl-003]** A conforming **governance** implementation MUST
limit the `extends:` chain depth to **5** layers maximum and MUST
reject cycles in the chain with a diagnostic naming the cycle
members.

<a id="req-pl-004"></a>
**[req-pl-004]** A conforming **governance** implementation MUST
pin an `extends:` reference to the host class (see
[Section 10.3](#103-token-leakage-across-hosts)) of the **leaf**
policy. A policy fetched from one host class MUST NOT extend a
policy fetched from any other host class; cross-host-class
`extends:` MUST be rejected at parse time.

<a id="req-pl-006"></a>
**[req-pl-006]** A conforming **governance** implementation MUST
merge a policy chain per the following table:

| Field family                          | Merge rule                                                             |
|---------------------------------------|------------------------------------------------------------------------|
| `enforcement`                         | Stricter wins (`block` > `warn` > `off`).                              |
| `fetch_failure`                       | Child overrides if set.                                                |
| `*.allow` lists                       | Set intersection. `null` is transparent.                               |
| `*.deny` lists                        | Union, deduplicated, parent order preserved.                           |
| `*.require` lists                     | Union, deduplicated, parent order preserved.                           |
| `dependencies.max_depth`              | `min(parent, child)`.                                                  |
| `dependencies.require_resolution`     | Stricter wins (`block` > `policy-wins` > `project-wins`).              |
| `mcp.self_defined`                    | Stricter wins (`deny` > `warn` > `allow`).                             |
| `mcp.trust_transitive`                | Logical AND.                                                           |
| `manifest.scripts`                    | Stricter wins (`deny` > `allow`).                                      |
| `unmanaged_files.action`              | Stricter wins (`deny` > `warn` > `ignore`).                            |
| `unmanaged_files.exclude`             | Union, deduplicated (byte-exact on each pattern's UTF-8 string), parent order preserved (additive: a child adds exclusions and cannot clear a parent's; `null` and `[]` both preserve the parent list). |
| `security.integrity.require_hashes`   | Logical OR (once `true`, stays `true`).                                |
| `security.audit.fail_on_drift`        | Logical OR (once `true`, stays `true`).                                |

### 6.5 Allow-list / deny-list tri-state semantics

<a id="req-pl-005"></a>
**[req-pl-005]** A conforming **governance** implementation MUST
treat list-valued allow/deny/require fields as a three-state value:
(a) field omitted (or explicit `null`) means "no opinion" and is
transparent during merge; (b) explicit empty list `[]` means
"explicitly empty" and overrides the parent for that field; (c)
non-empty list `[...]` carries the listed entries and merges per the
table in [Section 6.4](#64-inheritance-and-merge-rules).

### 6.6 Forward compatibility

<a id="req-pl-009"></a>
**[req-pl-009]** A conforming **governance** implementation MUST
emit a warning (never a parse error) when it encounters an unknown
top-level key in `apm-policy.yml`. This guarantees newer policy
files load on older clients without breaking the install.
Vendor-extension keys matching `x-[a-z][a-z0-9-]*` are recognised
per [req-ext-001](#req-ext-001) and MUST NOT produce a warning;
they are preserved silently. See also
[req-ext-001](#req-ext-001).

### 6.7 Worked example (informative)

A small policy chain:

```yaml
# .github/apm-policy.yml in contoso/.github
name: contoso-baseline
extends: contoso-enterprise/policy
enforcement: block
fetch_failure: block

dependencies:
  allow:
    - contoso/*
    - microsoft/apm-skills-*
  deny:
    - "*/legacy-*"
  require:
    - contoso/security-baseline
  require_pinned_constraint: true
  max_depth: 25

manifest:
  scripts: deny
  required_fields:
    - description
    - license
```

### 6.8 Integrity controls (governance)

The `security.integrity` and `security.audit` blocks declare opt-in,
fail-closed controls. Both are default-off; a policy that omits them
is unaffected.

<a id="req-pl-013"></a>
**[req-pl-013]** A conforming **governance** implementation that
honours `security.integrity.require_hashes: true` MUST fail the
install operation with a fail-closed diagnostic when any resolved
non-local dependency selected for installation lacks a recorded
content hash (the `content_hash` lockfile field) in `apm.lock.yaml`.
The same fail-closed behaviour
MUST hold when the lockfile is absent or unreadable at the point of
the check. Local dependencies are exempt; they are anchored by
deployed-file hashes rather than a package content hash.

<a id="req-pl-014"></a>
**[req-pl-014]** A conforming **governance** implementation that
honours `security.audit.fail_on_drift: true` MUST cause the audit
operation to terminate with a non-zero exit status when lockfile
drift is detected, or when the drift check fails to complete (for
example, an unreadable or corrupt local dependency graph). A drift
check that is merely skipped for an advisory reason, such as a cache
miss, does not by itself alter the exit status. When
`security.audit.fail_on_drift` is absent or `false`, detected drift
MUST be reported without, by itself, altering the audit exit status.

### 6.9 Conformance requirements (governance)

This section's normative statements are:

- Governance: [req-pl-001](#req-pl-001), [req-pl-002](#req-pl-002),
  [req-pl-003](#req-pl-003), [req-pl-004](#req-pl-004),
  [req-pl-005](#req-pl-005), [req-pl-006](#req-pl-006),
  [req-pl-007](#req-pl-007), [req-pl-008](#req-pl-008),
  [req-pl-009](#req-pl-009), [req-pl-010](#req-pl-010),
  [req-pl-011](#req-pl-011), [req-pl-012](#req-pl-012),
  [req-pl-013](#req-pl-013), [req-pl-014](#req-pl-014),
  [req-pl-015](#req-pl-015).

---

## 7. Dependency resolution

### 7.1 Reference kinds

A consumer MUST classify every dependency declaration into exactly
one of the following five reference kinds, evaluated in the order
listed:

1. **local** -- `local-path-form` strings or object entries lacking
   a `git:` and `id:` key with a `path:` to the filesystem.
2. **registry** -- object entries with `id:` and (implicit or
   explicit) `registry:`.
3. **git-semver** -- object or string entries whose `ref:` value
   matches a semver-range pattern (for example `^1.2.0`, `~2.0`,
   `>=1.0,<2.0`).
4. **git-literal** -- git URL or shorthand with a literal `ref:`
   (commit SHA, tag, branch).
5. **marketplace** -- non-normative in v0.1; producer-side
   authoring artifact only.

<a id="req-rs-008"></a>
**[req-rs-008]** A conforming **consumer** implementation MUST
classify every dependency by the priority above as a deterministic
function of the entry alone (no remote calls, no implementation
defaults). Two conforming consumers presented with the same entry
MUST produce the same kind classification.

### 7.2 Resolution algorithm

<a id="req-rs-001"></a>
**[req-rs-001]** A conforming **consumer** implementation MUST
resolve dependencies by **breadth-first** traversal of the
dependency tree, in the **declaration order** of each manifest.
When the same package identity (`<owner>/<repo>` or registry
identity) is reached via multiple constraint paths (a "diamond"),
the consumer MUST apply the following tri-modal policy:

1. **Intersection-pick (default).** If every reachable constraint
   for the identity has a non-empty intersection, the consumer
   MUST select the **highest** version satisfying every constraint
   in the intersection. The selected version is recorded in the
   lockfile; the chain that contributed the binding tightest
   constraint is recorded as `resolved_by`.
2. **Empty-intersection fail-closed.** If the intersection of
   reachable constraints is empty, the install MUST fail with a
   diagnostic naming both root-to-conflict chains. Silent
   first-wins resolution MUST NOT be substituted.
3. **Nest mode (opt-in).** The manifest MAY declare
   `dependencies.conflict_resolution: nest`, which instructs the
   consumer to allow multiple versions of the same identity
   co-existing under distinct deploy paths (npm-style nesting).
   Nest mode is OPTIONAL in v0.1; its on-disk layout normative pin
   is reserved for v0.2. In v0.1, the on-disk deploy layout for
   `conflict_resolution: nest` is reserved (see
   [Section 4.8](#48-workspaces-reserved-for-v02) and the future
   workspaces semantics). A conforming **consumer** encountering
   `dependencies.conflict_resolution: nest` in a v0.1 manifest
   MUST refuse the install with a normative diagnostic naming the
   key as reserved-for-v0.2 and citing this section.

<a id="req-rs-013"></a>
**[req-rs-013]** A conforming **consumer** implementation MUST
refuse to install a v0.1 manifest declaring
`dependencies.conflict_resolution: nest`, emitting a normative
diagnostic that names the `conflict_resolution: nest` key as
reserved for v0.2 and cites
[Section 7.2](#72-resolution-algorithm) clause (3).

<a id="req-rs-010"></a>
**[req-rs-010]** A conforming **consumer** implementation
producing an empty-intersection diagnostic per
[req-rs-001](#req-rs-001) clause (2) MUST format the diagnostic
so that it lists, for each chain, the ordered sequence of
`<owner>/<repo>@<constraint>` entries from the root manifest to
the conflicting entry, separated by `->`. Both chains MUST be
named; the diagnostic MUST be deterministic for a given install
plan.

<a id="req-rs-006"></a>
**[req-rs-006]** A conforming **consumer** implementation MUST stop
transitive resolution at a configurable depth cap whose default value
is **50**. The Governance class MAY tighten this cap via
`policy.dependencies.max_depth` (see [Section 6.3.1](#631-dependencies)).
Exceeding the cap MUST cause the install to fail with a diagnostic
naming the chain at which the cap was reached.

### 7.3 git-semver resolution

<a id="req-rs-003"></a>
**[req-rs-003]** A conforming **consumer** implementation MUST
classify the `ref:` of any git dependency into one of three kinds:
(a) `semver` -- the ref value parses as a semver range per
[Section 7.3.1](#731-semver-dialect-normative); (b)
`literal` -- the ref value is a commit SHA, a literal tag (matching
`v?\d+\.\d+\.\d+` or a non-semver tag), or a branch name; (c)
`none` -- the entry has no `ref:` at all.

<a id="req-rs-002"></a>
**[req-rs-002]** A conforming **consumer** implementation MUST,
when resolving a git-semver dependency, list the remote git tags of
the repository, dereference annotated tags to their peeled commit
object (lightweight and annotated tags are treated equivalently
thereafter), discard any tag whose name fails to parse under the
semver dialect of [Section 7.3.1](#731-semver-dialect-normative)
without diagnostic, filter the remainder to those matching the
manifest's semver range under the same dialect, and pin the
**highest** matching tag in the lockfile. Pre-release tags MUST be
excluded from selection unless explicit opt-in is signalled per
[Section 7.3.1](#731-semver-dialect-normative). The selected tag,
the original constraint, and the resolution timestamp MUST be
written per [req-lk-008](#req-lk-008).

<a id="req-rs-007"></a>
**[req-rs-007]** A conforming **consumer** implementation MUST
evaluate every semver range expression in a manifest or lockfile
under the **node-semver** dialect as pinned in
[Section 7.3.1](#731-semver-dialect-normative). No
implementation-defined hedging is permitted.

#### 7.3.1 Semver dialect (normative)

OpenAPM v0.1 pins the semver-range dialect to **node-semver**
([https://github.com/npm/node-semver](https://github.com/npm/node-semver))
as its normative reference, with version precedence and pre-release
ordering inherited from **Semantic Versioning 2.0.0** Section 11
([https://semver.org/spec/v2.0.0.html](https://semver.org/spec/v2.0.0.html)).
The conformance oracle for this section is
`tests/fixtures/spec-conformance/resolution/semver-dialect.json`
(see [Section 12.4](#124-fixture-layout-informative)).

**Range operators (normative).**

| Operator   | Semantics                                                                                              |
|------------|--------------------------------------------------------------------------------------------------------|
| `^x.y.z`   | Compatible-with-X: matches `>= x.y.z, < (x+1).0.0` when `x > 0`; `>= 0.y.z, < 0.(y+1).0` when `x == 0` and `y > 0`; `>= 0.0.z, < 0.0.(z+1)` when `x == 0` and `y == 0`. |
| `~x.y.z`   | Approximately equivalent: `>= x.y.z, < x.(y+1).0`. `~x.y` (no patch) is equivalent to `>= x.y.0, < x.(y+1).0`. |
| `>=`, `>`, `<=`, `<`, `=` | Comparator-form: standard inequality on semver precedence.                               |
| `x.y.z`, `*` | Wildcard: any version (subject to pre-release exclusion).                                  |
| Range list (comma or whitespace) | Logical AND: `>=1.0.0, <2.0.0` matches versions satisfying both comparators.     |
| `\|\|`     | Logical OR: `^1 \|\| ^2` matches versions satisfying either range list.                                 |
| `x.y.z - a.b.c` | Hyphen range: equivalent to `>= x.y.z, <= a.b.c`.                                                 |

**Build metadata.** Build metadata (anything after `+` in a
version) MUST be ignored for precedence comparisons per semver
2.0.0 Section 10. Two versions differing only in build metadata
compare as equal.

<a id="req-rs-014"></a>
**[req-rs-014]** When two candidate tags have equal precedence
under semver 2.0.0 Section 11 (i.e. they differ only in
build-metadata identifier), a conforming **consumer** MUST select
the tag whose name compares highest under bytewise ASCII ordering
of the full tag string. This rule eliminates non-determinism in
build-metadata ties.

**Pre-release ordering.** Pre-release ordering follows semver
2.0.0 Section 11: numeric identifiers compare numerically, ASCII
alphanumeric identifiers compare lexicographically in ASCII order,
numeric identifiers always have lower precedence than alphanumeric
identifiers, and a larger set of pre-release fields has higher
precedence than a smaller set when all preceding fields are equal.

**Pre-release opt-in (normative).** A pre-release tag (per semver
2.0.0 Section 9) MAY be selected only when **at least one** of the
following is true:

1. The manifest range expression itself contains a pre-release
   identifier on the same `[major, minor, patch]` tuple as the
   candidate tag (node-semver "include-prerelease in same range"
   semantics). For example, `>=1.2.0-alpha <1.3.0` permits
   `1.2.0-beta` and `1.2.0`, but does NOT permit `1.3.0-alpha`.
2. The manifest dependency entry declares `prerelease: true`
   (explicit opt-in across the whole range).

When neither (1) nor (2) holds, every candidate tag with a
non-empty pre-release identifier MUST be discarded from the
candidate set before highest-match selection.

**`0.x` quirk.** Per semver 2.0.0 Section 4, anything `0.x.y` is
considered unstable; the caret operator narrows accordingly as
defined above (`^0.2.3` matches `>= 0.2.3, < 0.3.0`, NOT
`>= 0.2.3, < 1.0.0`). This is the node-semver convention and is
adopted normatively.

**Determinism.** The selection function is a deterministic
function of (range expression, candidate-tag set, opt-in
signal). Two conforming consumers presented with the same inputs
MUST select the same tag.

### 7.4 Transitive resolution and conflict policy

Conflicts at the **primitive** level (multiple sources providing a
primitive with the same name) are governed by
[Section 8.3](#83-priority-and-conflict-resolution). Conflicts at
the **package** level (multiple resolution paths reaching the same
package identity at different constraints) are governed by
[req-rs-001](#req-rs-001)'s tri-modal policy.

> **Design rationale (non-normative).** The tri-modal policy
> replaces v0's silent first-wins behaviour. Empty-intersection
> fail-closed is the correctness default: a consumer that
> downgrades a dep silently to satisfy a transitive constraint
> produces audit drift the workspace owner did not author. The
> intersection-pick default is conservative; nest-mode is the
> escape hatch for ecosystems that cannot live without parallel
> versions. A future v0.2 may introduce
> `policy.dependencies.resolver:` to let Governance pick the mode
> centrally.

A worked example showing primitive- and package-level conflicts
composing:

```yaml
# Manifest:
dependencies:
  apm:
    - acme/foo#^1.2.0     # direct: depth 1, constraint ^1.2.0
    - acme/bar#^2.0.0     # direct: depth 1, constraint ^2.0.0
# acme/bar transitively pulls acme/foo#^1.5.0 (depth 2).
# Intersection of ^1.2.0 and ^1.5.0 is [>=1.5.0, <2.0.0]: pick highest
# tag in [1.5.0, 2.0.0) per req-rs-001 clause (1).
# If acme/bar instead pulled acme/foo#^2.0.0, intersection is empty
# and install fails closed per req-rs-001 clause (2).
```

Lockfile fragment for the 3-chain intersection case
(per [req-rs-010](#req-rs-010), `resolved_by` records the chain
contributing the binding tightest constraint):

```yaml
# Three chains reach acme/foo:
#   root -> acme/foo#^1.2.0                       (depth 1, lo=1.2.0)
#   root -> acme/bar#^2.0.0 -> acme/foo#^1.5.0    (depth 2, lo=1.5.0)
#   root -> acme/baz#^3.0.0 -> acme/qux#^1.0.0 -> acme/foo#~1.7.0
#                                                  (depth 3, lo=1.7.0)
# Intersection: [>=1.7.0, <2.0.0]. Pick highest tag in that range.
# The tightest lower bound is contributed by acme/baz -> acme/qux,
# so `resolved_by` records that chain.
dependencies:
  - repo_url: github.com/acme/foo
    resolved_tag: v1.7.4
    constraint: "~1.7.0"
    resolved_by: "acme/baz#^3.0.0 -> acme/qux#^1.0.0 -> acme/foo#~1.7.0"
    depth: 3
```

### 7.5 Lockfile replay semantics

<a id="req-rs-004"></a>
**[req-rs-004]** A conforming **consumer** implementation MUST treat
a manifest entry whose `ref:` is a semver range as equivalent to its
locked counterpart (no drift) when, and only when, the locked
`constraint` value is character-equal to the manifest's current
range. Any difference, including whitespace, MUST trigger
re-resolution.

#### 7.5.1 Mirror resolution

OpenAPM v0.1 anchors trust on the recorded `resolved_hash`, not on
the recorded `resolved_url`. This permits enterprise mirrors,
content-addressable proxies, and offline caches to substitute for
the origin URL without lockfile churn.

Registry-class implementations participating in mirror resolution
MUST satisfy [req-rg-001](#req-rg-001) (Registry-class trust
anchor).

<a id="req-rs-009"></a>
**[req-rs-009]** A conforming **consumer** implementation MUST
permit the fetch of a registry-sourced dependency to be satisfied
by **any** registry declared in the project's `apm.yml`
`registries:` block, or by any policy-declared mirror, **provided
that** the bytes returned by the mirror hash to the lockfile's
recorded `resolved_hash`. The `resolved_url` field is advisory in
v0.1: a mismatch between the mirror URL and `resolved_url` MUST
NOT fail the install when the hash matches. A hash mismatch MUST
fail closed per [req-lk-013](#req-lk-013), regardless of which
registry served the bytes.

> **Editorial note (non-normative).** The mirror-tolerance
> property of [req-rs-009](#req-rs-009) holds against the recorded
> `resolved_hash`, not against bytes reconstructed from the
> upstream source. Mirror operators MUST replicate the original
> archive bytes verbatim; rebuilding the archive on the mirror
> (even from the same source revision) will produce a different
> `resolved_hash` and break the mirror-tolerance guarantee until
> v0.2 introduces reproducible-build determinism (see
> [Section 1.1](#11-goals-and-non-goals) non-goals).

### 7.6 Diagnostic surface (`deps why`)

<a id="req-rs-005"></a>
**[req-rs-005]** A conforming **consumer** implementation that
exposes a "why is this dependency present" diagnostic command MUST
compute the answer by walking the lockfile **bottom-up** from the
target entry to the root, returning the set of root-to-target chains
that include the target. Chains MUST be returned in lexicographic
order of the root-to-target path tuple. The walker MUST operate
offline against the lockfile alone, MUST be safe against cycles (no
infinite recursion), and MUST produce deterministic output for a
given lockfile.

### 7.7 Update operation

OpenAPM v0.1 defines the semantics of an explicit "update"
operation so that two conforming consumers produce the same lockfile
delta from the same inputs.

<a id="req-rs-011"></a>
**[req-rs-011]** A conforming **consumer** implementation that
exposes an `apm update` (or equivalent) command MUST, when invoked
without a package argument, re-resolve every direct dependency
against its **current** manifest constraint (holding the manifest
unchanged), MUST rewrite the lockfile pins to the new highest
matching version for each direct dep, MUST re-resolve all
transitive dependencies as a side-effect, and MUST honour the
active Governance policy's `require_pinned_constraint` rule
([req-pl-007](#req-pl-007)).

<a id="req-rs-012"></a>
**[req-rs-012]** A conforming **consumer** implementation that
exposes `apm update <name>` MUST scope re-resolution to the named
package and its subtree only, MUST hold every other resolved entry
at its prior pin, and MUST refuse to operate on a frozen install
(see [req-lk-006](#req-lk-006)) without an explicit override
flag.

Range-widening update modes (for example `apm update --aggressive`,
which would mutate the manifest's range upper bounds) are
**reserved for v0.2**.

### 7.8 Producer release contract

OpenAPM v0.1 defines a minimal producer release contract so that
git-semver resolvers ([req-rs-002](#req-rs-002)) bind to the same
artifact every consumer sees.

<a id="req-pr-004"></a>
**[req-pr-004]** A conforming **producer** publishing a git tag
intended for consumption via git-semver MUST ensure that the tag
points at a commit whose `apm.yml` `version` field is equal to the
tag (modulo an OPTIONAL leading `v` prefix). For example, the tag
`v2.3.1` MUST point at a commit whose `apm.yml` contains
`version: "2.3.1"`. The tag name MUST match
`^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-((0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(\.(0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(\+([0-9a-zA-Z-]+(\.[0-9a-zA-Z-]+)*))?$`
(this matches the semver.org 2.0.0 regex modulo the optional
leading `v`). A conforming **consumer** SHOULD verify this
alignment after `git checkout` of the resolved tag and SHOULD emit
a non-blocking diagnostic on mismatch.

<a id="req-pr-005"></a>
**[req-pr-005]** A conforming **producer** publishing release tags
SHOULD sign tags via a publicly verifiable mechanism (for example
sigstore, GPG, or SSH-signed git tags). Signature verification is
not enforced by v0.1 consumers; the SHOULD is advisory and feeds
the v0.2 provenance work (see
[Section 10.12](#1012-publisher-provenance-and-attestations-reserved-for-v02)).

Release publication itself is **out of scope** for the v0.1 CLI
surface. The canonical publication flow is whichever tag-and-release
mechanism the producer's git host provides; on GitHub, that is
`gh release create` or the `microsoft/apm-action mode: release`
workflow. No `apm pack --create-tag` or `apm pack --push` surface
is defined by v0.1; producers MUST NOT depend on such a surface.

### 7.9 Version withdrawal (reserved for v0.2)

Version withdrawal (yank, deprecate, supersede) for published
versions is **out of scope for OpenAPM v0.1 and reserved for v0.2**.
A future surface will define: `yanked: true` (consumers MUST NOT
select for fresh resolution, MAY honour for existing locks with
SHOULD-warn), `superseded_by: <version>`, and Governance
`refuse_yanked: block | warn | off`. Producers needing withdrawal
semantics in v0.1 MUST rely on out-of-band advisories.

### 7.10 Worked example (informative)

A consumer with the manifest:

```yaml
name: web-app
version: "1.0.0"
default_host: github.com
dependencies:
  apm:
    - contoso/security-baseline#^2.0
    - git: https://gitlab.example.com/acme/coding-standards.git
      ref: main
```

resolves to the lockfile:

```yaml
lockfile_version: "2"
generated_at: "2026-05-10T20:14:00+00:00"
apm_version: "0.7.0"
dependencies:
  - repo_url: github.com/contoso/security-baseline
    resolved_commit: "a1b2c3d4e5f6789012345678901234567890abcd"
    resolved_ref: "^2.0"
    constraint: "^2.0"
    resolved_tag: v2.3.1
    resolved_at: "2026-05-10T20:14:00+00:00"
    tree_sha256: "sha256:0102030405060708091011121314151617181920212223242526272829303132"
    depth: 1
  - repo_url: gitlab.example.com/acme/coding-standards
    resolved_commit: "f6e5d4c3b2a1098765432109876543210fedcba9"
    resolved_ref: main
    tree_sha256: "sha256:abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd"
    depth: 1
```

The example uses `lockfile_version: "2"` even though no
registry-sourced entry is present; per [req-lk-002](#req-lk-002),
this is permitted. Once written as `"2"`, this lockfile MUST NOT
be demoted to `"1"` on subsequent rewrites.

### 7.11 Conformance requirements (resolution)

This section's normative statements are:

- Consumer: [req-rs-001](#req-rs-001), [req-rs-002](#req-rs-002),
  [req-rs-003](#req-rs-003), [req-rs-004](#req-rs-004),
  [req-rs-005](#req-rs-005), [req-rs-006](#req-rs-006),
  [req-rs-007](#req-rs-007), [req-rs-008](#req-rs-008),
  [req-rs-009](#req-rs-009), [req-rs-010](#req-rs-010),
  [req-rs-011](#req-rs-011), [req-rs-012](#req-rs-012),
  [req-rs-013](#req-rs-013), [req-rs-014](#req-rs-014).
- Producer: [req-pr-004](#req-pr-004).
- Producer (SHOULD): [req-pr-005](#req-pr-005).

---

## 8. Primitive type system and target matrix

### 8.1 Primitive types

This specification recognises **seven** primitive types: `instructions`,
`prompts`, `agents`, `skills`, `commands`, `hooks`, and `mcp`. A
primitive is a typed unit of agent configuration sourced from one of
the recognised package layouts:

- **APM package** (`.apm/` directory). Primitives live under typed
  subdirectories (`.apm/skills/`, `.apm/agents/`, ...) and are
  hoisted individually into deploy directories.
- **Skill bundle** (`SKILL.md` at root, optionally with `apm.yml` =
  hybrid). The whole directory is copied to
  `<deploy>/skills/<name>/`.
- **Skill collection** (`skills/<name>/SKILL.md` nested). Each
  nested skill is promoted to `<deploy>/skills/<name>/`.
- **Plugin collection** (`plugin.json` / `.claude-plugin/`).
  Artifacts are mapped into deploy directories per the plugin
  manifest.

### 8.2 Discovery and source tracking

<a id="req-pr-001"></a>
**[req-pr-001]** A conforming **consumer** implementation MUST
attach a source attribution to every discovered primitive. The
attribution MUST be `local` for primitives sourced from the
project's own `.apm/` directory and MUST be of the form
`dependency:<name>` for primitives sourced from a resolved
dependency.

### 8.3 Priority and conflict resolution

<a id="req-pr-002"></a>
**[req-pr-002]** A conforming **consumer** implementation MUST cause
local primitives to override dependency primitives of the same name
and same primitive type. The conflict MUST be recorded in the
consumer's diagnostic surface and MUST be inspectable by the user.

<a id="req-pr-003"></a>
**[req-pr-003]** A conforming **consumer** implementation MUST
process dependencies in the order they are declared in the manifest
(direct deps first, transitive deps appended in lockfile order).
When two dependencies provide primitives with the same name and
same type, the **first declared** dependency wins; later
dependencies' versions MUST NOT replace the resolved primitive.

### 8.4 Target detection signals (normative)

When the user has not specified a target via `--target` or in the
manifest's `target:` field, the consumer auto-detects from
filesystem signals. The concrete table of per-target detection
signals and deploy roots is published in the non-normative
**"OpenAPM Target Registry v0.1"** companion document (see
[targets-matrix.md](../../reference/targets-matrix/)). Vendors MAY
register new targets without a spec amendment via the
`x-<vendor>-<name>` extension namespace (see
[req-tg-004](#req-tg-004)).

<a id="req-tg-001"></a>
**[req-tg-001]** A conforming **consumer** implementation MUST
honour the per-target detection predicate published in the
registered OpenAPM Target Registry for every spec-registered target
identifier and for every vendor-registered identifier
([req-tg-004](#req-tg-004)). Auto-detection MUST activate a target
**only** when its registered predicate fires; no other filesystem
signal MAY substitute for, or augment, the registered predicate.
`agent-skills` MUST NOT be auto-detected; it MUST be selected
explicitly via `--target agent-skills` or via the manifest's
`target:` field. When no detection signal fires, the consumer MAY
fall back to a `minimal` profile that emits `AGENTS.md` only.

### 8.5 Deploy directory contract (normative)

OpenAPM v0.1 establishes `.agents/` as an **ecosystem convention**:
the cross-tool deploy root for primitives shared between targets
that opt into convergence. Per-target deploy roots are published in
the non-normative OpenAPM Target Registry v0.1 companion.

<a id="req-tg-002"></a>
**[req-tg-002]** A conforming **consumer** implementation MUST
deploy primitives only under the deploy root(s) registered for the
active target in the OpenAPM Target Registry. No target's
installer MAY write files outside its registered root(s); writing
outside the registered root MUST be treated as an implementation
defect, not a runtime warning. When two targets register the same
deploy root (for example two targets that both share `.agents/`),
each target OWNS only the file-name patterns documented for that
target in the Registry; `.agents/` is partitioned by subdirectory
(`.agents/skills/`, `.agents/commands/`, `.agents/prompts/`, ...)
so that distinct targets do not contend for the same on-disk
patterns.

<a id="req-tg-003"></a>
**[req-tg-003]** A conforming **consumer** implementation MUST
deploy skills to `.agents/skills/<name>/SKILL.md` for every target
that supports the `skills` primitive type, unless the user has
explicitly opted out of skill-convergence via the documented
opt-out switch. This cross-tool convergence ensures a single skill
bundle serves every harness without per-target duplication.

### 8.6 Per-target primitive support (informational)

The matrix of which primitive types each target supports is
informational and additive: new harness adapters MAY add support
without a spec revision. The current matrix is in the companion
[targets-matrix.md](../../reference/targets-matrix/).

### 8.7 Conformance requirements (primitives and targets)

- Consumer: [req-pr-001](#req-pr-001), [req-pr-002](#req-pr-002),
  [req-pr-003](#req-pr-003), [req-tg-001](#req-tg-001),
  [req-tg-002](#req-tg-002), [req-tg-003](#req-tg-003),
  [req-tg-004](#req-tg-004).

---

## 9. Versioning and amendment process

### 9.1 Spec versioning

OpenAPM follows the semver discipline at the document level:

- **0.x** -- editor's drafts. Each minor MAY introduce breaking
  changes with the migration window in [Section 9.5](#95-migration-windows-for-consumers).
- **1.0** -- first stable cut. Strictly additive within the 1.x
  major.
- **2.x and beyond** -- breaking changes require a major bump.

### 9.2 Breaking vs. non-breaking change definition

**Non-breaking** (allowed within a minor):

- Adding a new OPTIONAL field to manifest, lockfile, or policy.
- Adding a new enum value to a non-conformance-critical enum (for
  example a new target name registered in the Target Registry
  companion).
- Adding a new conformance test for behaviour already required.

**Breaking** (requires a minor bump with migration window):

- Removing or renaming a field.
- Changing the type or required-ness of a field.
- Tightening a SHOULD to a MUST. (Tightening introduces a new
  hard-conformance bar; it requires migration even when
  most implementations already meet the bar.)
- Loosening a MUST to a SHOULD.
- Promoting a parse-time warning to a parse-time error.
- Bumping `lockfile_version` for non-additive changes.

### 9.3 Amendment process

1. An issue is filed in `microsoft/apm` with a `spec/openapm-vN.x`
   label.
2. An editor's-draft PR amends
   `docs/src/content/docs/specs/openapm-vN.x.md`.
3. A reviewer panel of at minimum two non-author reviewers (one
   with implementation experience, one with
   consumer/integrator experience) approves.
4. A 14-day public comment period follows panel approval.
5. Merged amendments append to [Appendix D](#appendix-d-revision-history)
   with a `req-xxx`-level diff.

### 9.4 Errata vs. new revision

**Errata** are clarifications that do not change implementation
behaviour. They are merged inline with an `[Errata YYYY-MM-DD]`
footnote and summarised at the top of [Appendix D](#appendix-d-revision-history).
A new revision (minor bump) is required for any change that
satisfies the breaking-change definition in [Section 9.2](#92-breaking-vs-non-breaking-change-definition).

### 9.5 Migration windows for consumers

Breaking changes MUST be announced in the **previous** minor's
revision history (so integrators see the announcement while building
against the still-supported minor). A minimum of **90 days** MUST
elapse between announcement and removal. Consumers SHOULD emit
deprecation warnings during the migration window.

The marketplace input block (manifest [Section 4.7](#47-marketplace-authoring-block-normative-input))
is part of the manifest format. The shape of the emitted
`marketplace.json` artifact is governed externally; this
specification tracks upstream changes additively and does not bind
the emitted artifact.

---

## 10. Security considerations

This section enumerates the attack surfaces this specification
addresses, and maps each to the normative requirement(s) that
mitigate it. Attack surfaces marked **deferred to v0.2** identify a
mitigation that exists in v0.1 but whose normative wire-level
treatment lands with the registry HTTP API.

### 10.1 Dependency confusion

**Threat.** An adversary publishes a package with the same name as
an internal package on a public registry; the consumer's resolver
fetches the public copy instead of the internal one.

**v0.1 posture.** Absent an active Governance policy, OpenAPM v0.1
has **NO consumer-class mitigation** for dependency confusion: the
unprotected consumer install MUST be assumed vulnerable, and this
specification does not claim otherwise. Mitigations below are
Governance-class controls that an organisation MUST opt into.

**Mitigations (Governance-class only).** The Governance class's
allow/deny tri-state ([req-pl-005](#req-pl-005),
[req-pl-006](#req-pl-006)) lets an organisation pin acceptable
sources. The `require_pinned_constraint` rule
([req-pl-007](#req-pl-007)) forces the consumer to declare intent
explicitly, surfacing the dependency for review. A v0.2
`registry_source.allow_non_registry: false` toggle closes the
bypass in-band; v0.1 relies on policy review.

### 10.2 Typosquatting

**Threat.** A lookalike package name (`acm/security-baseline` instead
of `acme/security-baseline`) lures the consumer into installing a
hostile package.

**v0.1 posture.** Absent an active Governance policy, OpenAPM v0.1
has **NO consumer-class mitigation** for typosquatting. Lookalike
detection, vendor-distance scoring, and registry-side
disambiguation are reserved for v0.2 and are explicitly out of
scope for the v0.1 consumer.

**Mitigations (Governance-class only).** Canonical normalisation
([req-mf-009](#req-mf-009)) collapses cosmetic differences and
makes name comparisons stable for policy authors.
`require_pinned_constraint` ([req-pl-007](#req-pl-007)) forces
explicit refs, raising review value. Allow/deny lists
([req-pl-005](#req-pl-005), [req-pl-006](#req-pl-006)) gate the
acceptable name space.

### 10.3 Token leakage across hosts

**Threat.** A credential issued for `github.com` is forwarded to
`evil.example.com` during a cross-host clone, leaking the token.

**Mitigations.**

<a id="req-sc-003"></a>
**[req-sc-003]** A conforming **consumer** implementation MUST
resolve credentials per host class (as defined in
[Section 3](#3-terminology) and as alias-extended via
[req-sc-006](#req-sc-006)), and MUST NOT forward a credential
resolved for one host class to a request targeting another host
class. Credential scope MUST be observable in the consumer's
diagnostic surface. When a fetch follows an HTTP redirect (3xx)
whose target hostname classifies into a different host class than
the originating request per [req-sc-005](#req-sc-005), the consumer
MUST drop the originating Authorization header (and any other
credential material attached for the originating host class) before
issuing the redirected request. Credentials for the destination
host class MAY be re-resolved per this requirement.

<a id="req-sc-005"></a>
**[req-sc-005]** A conforming **consumer** implementation that
classifies two distinct hostnames as the same host class for the
purposes of credential reuse MUST do so on the basis of (a)
identical eTLD+1 per the Public Suffix List
([https://publicsuffix.org](https://publicsuffix.org)), or (b) an
explicit `aliases:` entry in the project's `apm.yml`
`registries:` block (see [Section 4.2.3](#423-registries) and
[req-sc-006](#req-sc-006)). Implementations MUST NOT collapse two
hostnames onto the same host class on any other basis (such as
DNS CNAME chains, TLS SAN entries, or shared HTTP redirects).

<a id="req-sc-007"></a>
**[req-sc-007]** A conforming **consumer** implementation MUST
redact credential material (tokens, basic-auth passwords, bearer
strings) so that such material MUST NOT appear in any user-facing
diagnostic, log, error message, packed bundle, lockfile, or
persisted audit record. The diagnostic that reveals "credential X
was used for host Y" MUST identify the credential by source
descriptor (for example `GITHUB_APM_PAT environment variable`),
not by literal value. The Producer toolchain MUST refuse to pack
any file whose path matches the configurable secret-pattern set
(default patterns: `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`,
`id_ed25519`); the pattern set MAY be extended via policy. See
also the broader token-leakage mitigation row in
[Section 10.11](#1011-summary-table).

<a id="req-sc-008"></a>
**[req-sc-008]** A conforming **consumer** implementation SHOULD
refuse to attach a credential to a git-over-HTTP fetch whose URL
scheme is not `https://`, unless the target host is the loopback
address (`127.0.0.0/8`, `::1`) or the target registry is declared
with `insecure: true` per [req-sc-006](#req-sc-006).

### 10.4 Lockfile tampering

**Threat.** An adversary edits `apm.lock.yaml` to swap a commit SHA
or a deployed-file hash and ship a malicious payload that still
"passes" a naive integrity check.

**Mitigations.**

<a id="req-sc-001"></a>
**[req-sc-001]** A conforming **consumer** implementation MUST
compute and record a SHA-256 content hash for every deployed file
(per [req-lk-012](#req-lk-012)) and MUST re-verify those hashes on
audit. Files present in `deployed_files` whose on-disk hash does
not match the recorded hash MUST be reported as a content-integrity
violation.

In addition, [req-lk-013](#req-lk-013) verifies registry archive
bytes before extraction, and [req-lk-017](#req-lk-017) re-verifies
deployed-file hashes on every frozen install. The combination
prevents an attacker from tampering with installed files without
detection.

### 10.5 Registry impersonation

**Threat.** DNS or MITM redirection points a registry URL at an
attacker-controlled host serving a manipulated archive.

**Mitigation.** [req-lk-013](#req-lk-013) anchors trust in the
archive's SHA-256 (per the hash envelope at
[req-lk-016](#req-lk-016)), not the URL: a tampered archive fails
closed before extraction. The mirror-tolerance rule
([req-rs-009](#req-rs-009)) preserves this property: a mirror MAY
serve the bytes, but the bytes MUST still hash to the lockfile's
recorded `resolved_hash`. Registry-class implementations
participating in this trust chain MUST satisfy
[req-rg-001](#req-rg-001). A v0.2 normative TLS-only requirement
on the registry HTTP wire (reserved in
[Appendix B](#appendix-b-registry-http-api-reserved-for-v02))
augments this in-band.

In addition:

<a id="req-sc-004"></a>
**[req-sc-004]** A conforming **consumer** implementation MUST
constrain registry archive extraction so that (a) the archive
content-type is `application/gzip` over a tar payload (`tar.gz`);
implementations MUST reject `application/zip` and any other
archive container in v0.1; (b) the uncompressed archive size MUST
NOT exceed a configurable cap whose default value is **100 MB**;
and (c) the number of entries in the archive MUST NOT exceed a
configurable cap whose default value is **10,000**. Violations MUST
fail closed before extraction proceeds.

### 10.6 Malicious package execution at install time

**Threat.** A hostile package's `scripts:` block executes during
install.

**Mitigation.** This specification does **not** authorise
`apm install` to execute any `scripts:` entry. The producer-side
`scripts:` block is a named-entry registry consumed by an explicit
user invocation (such as `apm run <name>`). Governance MAY further
forbid `scripts:` declarations via `manifest.scripts: deny`. The
absence of an install-time execution path is the load-bearing
mitigation; the policy block is defence in depth.

### 10.7 Unverified content cleanup (file integrators)

**Threat.** A hostile transitive dependency claims authority over
files outside its real deployment footprint, causing the cleanup
logic to remove files belonging to another dependency or to the
project itself.

**Mitigations.** [req-tg-002](#req-tg-002) constrains each target
to its registered deploy root(s). The self-entry isolation in
[Section 5.3](#53-self-entry-semantics) prevents the cleanup logic
of one dependency from claiming the project's own files. Orphan
detection MUST scope per-dependency, not globally.

### 10.8 Policy bypass via crafted manifest

**Threat.** A consumer manifest exploits parser ambiguity (an
unknown key, a malformed `extends:` chain) to silently skip the
governance gate.

**Mitigations.** [req-pl-009](#req-pl-009) makes unknown policy
keys a warning, not a silent acceptance, and preserves them as
`x-*` extensions. [req-pl-010](#req-pl-010) fails closed on fetch
failure when configured. [req-pl-002](#req-pl-002) blocks before
disk write. [req-pl-003](#req-pl-003) caps `extends:` depth to
thwart amplification attacks.

### 10.9 Archive path-traversal (zip-slip / symlink escape)

**Threat.** A crafted tarball or zip with `..` segments, absolute
paths, or symlinks writes files outside the extraction root.

**Mitigation.**

<a id="req-sc-002"></a>
**[req-sc-002]** A conforming **consumer** implementation MUST
reject any archive entry whose extracted path would contain `..`
segments, would be absolute, or would be a symbolic or hard link.
Extraction MUST fail closed on the first such entry; partial
extractions MUST be cleaned up. The archive container, size, and
entry-count limits of [req-sc-004](#req-sc-004) MUST be enforced
in addition.

### 10.10 Hash-algorithm downgrade

**Threat.** A consumer's `policy.hash_algorithm` accepts a weak
digest (MD5, SHA-1) and an attacker exploits collision weakness to
serve a manipulated policy that matches the recorded digest.

**Mitigation.** [req-mf-018](#req-mf-018) restricts the allowed
algorithms to `sha256`, `sha384`, and `sha512`, rejecting weaker
choices at parse time. The lockfile hash envelope
([req-lk-016](#req-lk-016)) makes the digest algorithm explicit on
every stored hash, foreclosing algorithm-ambiguity attacks.

### 10.11 Summary table

| # | Attack surface                              | Mitigation requirement(s)                                          | Posture           |
|---|---------------------------------------------|--------------------------------------------------------------------|-------------------|
| 1 | Dependency confusion                        | [req-pl-005](#req-pl-005), [req-pl-006](#req-pl-006), [req-pl-007](#req-pl-007) | Governance-only   |
| 2 | Typosquatting                               | [req-mf-009](#req-mf-009), [req-pl-005](#req-pl-005), [req-pl-007](#req-pl-007) | Governance-only   |
| 3 | Token leakage across hosts                  | [req-sc-003](#req-sc-003), [req-sc-005](#req-sc-005), [req-sc-007](#req-sc-007), [req-sc-008](#req-sc-008) | Consumer-default  |
| 4 | Lockfile tampering                          | [req-lk-012](#req-lk-012), [req-lk-013](#req-lk-013), [req-lk-016](#req-lk-016), [req-lk-017](#req-lk-017), [req-sc-001](#req-sc-001) | Consumer-default  |
| 5 | Registry impersonation                      | [req-lk-013](#req-lk-013), [req-rs-009](#req-rs-009), [req-sc-004](#req-sc-004); v0.2 TLS-only deferred | Consumer-default  |
| 6 | Malicious package execution at install time | No install-time execution path; [req-pl-006](#req-pl-006) defence  | Consumer-default  |
| 7 | Unverified content cleanup                  | [req-tg-002](#req-tg-002); self-entry isolation                    | Consumer-default  |
| 8 | Policy bypass via crafted manifest          | [req-pl-002](#req-pl-002), [req-pl-009](#req-pl-009), [req-pl-010](#req-pl-010) | Governance-only   |
| 9 | Archive path-traversal                      | [req-sc-002](#req-sc-002), [req-sc-004](#req-sc-004)               | Consumer-default  |
| 10| Hash-algorithm downgrade                    | [req-mf-018](#req-mf-018), [req-lk-016](#req-lk-016)               | Consumer-default  |
| 11| Unauthorised executable primitive deployment | [req-sc-009](#req-sc-009)                                         | Consumer-default  |
| 12| Approval grant propagation via VCS           | [req-sc-010](#req-sc-010)                                         | Consumer-default  |
| 13| Org executable denial bypassed by project/user grant | [req-sc-011](#req-sc-011)                                 | Consumer-default  |
| 14| Required-package audit false-positive on withheld executable | [req-sc-012](#req-sc-012)                         | Consumer-default  |

### 10.12 Publisher provenance and attestations (reserved for v0.2)

Publisher provenance (cryptographic attestations binding a
specific package version to a specific publisher identity) is
**out of scope for OpenAPM v0.1 and reserved for v0.2**. The
lockfile's `attestations:` field (per
[req-lk-001](#req-lk-001)) and the producer-side tag-signing
SHOULD ([req-pr-005](#req-pr-005)) are reserved hooks for this
future surface. A future surface will define: in-toto / SLSA
provenance binding format; sigstore verification semantics;
Governance `policy.dependencies.require_attestation` enforcement
modes; and the registry HTTP wire envelope (alongside
[Appendix B](#appendix-b-registry-http-api-reserved-for-v02)).

### 10.13 Executable primitive approval gate

**Threat.** A dependency package deploys executable code --
hooks, bin executables, MCP server configurations, or canvas
extensions -- that runs on the developer's machine without
explicit consent.

**Mitigations.**

<a id="req-sc-009"></a>
**[req-sc-009]** A conforming **consumer** implementation MUST,
when the consuming project's `apm.yml` contains an `allowExecutables`
block, deny deployment of any executable primitive (hooks, bin
executables, MCP server configurations, and canvas extensions)
from a dependency package unless that package is explicitly listed
in the effective approval set for the corresponding executable type.
A consumer MUST fail closed when the `allowExecutables` block is
present but the package is absent from the approval set: the
primitive MUST NOT be deployed.

<a id="req-sc-010"></a>
**[req-sc-010]** A conforming **consumer** implementation that
provides an interactive approval mechanism for executable primitives
MUST persist per-user approval decisions in a location that is
isolated from the project manifest (`apm.yml`) and is not tracked
by version control alongside project files by default. The consumer
MUST NOT write interactive approval decisions into the project
`apm.yml`, so that one developer's approval cannot propagate
implicitly to other developers who clone or share the project.

### 10.14 Executable trust precedence and audit fidelity

**Threat.** Two failure modes defeat centralized executable
governance. First, a project-level or user-level approval re-enables
an executable primitive that an organization policy has denied, or
the install-time gate and the post-install audit reach different
trust conclusions for the same primitive (a split-brain that lets a
denied primitive read as trusted, or the reverse). Second, a
governance requirement mandating that a package be present is
reported as unmet merely because that package's executable primitives
were withheld from deployment, making it impossible to both mandate a
package and let a consumer withhold its executables.

**Mitigations.**

<a id="req-sc-011"></a>
**[req-sc-011]** A conforming **consumer** implementation MUST resolve
every executable-primitive trust decision through a single deny-wins
precedence relation in which an organization policy denial (an
`executables.deny` entry or `executables.deny_all` in an applicable
`apm-policy.yml` per [Section 6](#6-policy-format-apm-policyyml))
overrides any project-level or user-level grant for the same package
and executable type. A consumer MUST reach the identical allow-or-deny
outcome for identical inputs whether the decision gates deployment at
install time or classifies an already-installed primitive at audit
time. A consumer MUST NOT allow a project `apm.yml` grant or a
user-local approval to re-enable an executable primitive that an
applicable organization policy denies.

<a id="req-sc-012"></a>
**[req-sc-012]** A conforming **consumer** implementation that
evaluates a governance requirement mandating the presence of a package
(per [Section 6](#6-policy-format-apm-policyyml)) MUST determine
satisfaction of that requirement from the presence of the package in
the resolved lockfile, and MUST NOT condition it on whether that
package's executable primitives were deployed. When a required package
is present but one or more of its executable primitives are withheld
from deployment by the trust resolution of
[req-sc-011](#req-sc-011), a consumer MUST treat the presence
requirement as satisfied and MUST surface each withheld executable as
a diagnostic signal distinct from any missing-package violation.

---

## 11. Conformance

### 11.1 Conformance classes (normative)

This specification defines four conformance classes; this section
is the **sole normative home** for them. The forward pointer in
[Section 2](#2-conventions) is editorial.

| Class        | Role                                                                                  |
|--------------|---------------------------------------------------------------------------------------|
| Producer     | Emits a conforming `apm.yml`; optionally emits a conforming `apm.lock.yaml`. Conformance hooks: tag-release contract ([req-pr-004](#req-pr-004), [req-pr-005](#req-pr-005)). |
| Consumer     | Parses `apm.yml`, resolves dependencies per [Section 7](#7-dependency-resolution), writes `apm.lock.yaml`, deploys primitives per [Section 8](#8-primitive-type-system-and-target-matrix). |
| Registry     | **Reserved for v0.2.** One normative anchor in v0.1: see [req-rg-001](#req-rg-001) (trust-anchor expectation). |
| Governance   | Parses `apm-policy.yml`, evaluates per [Section 6](#6-policy-format-apm-policyyml), gates a Consumer install. |

An implementation MAY claim more than one class. A toolchain
component that packs a project (the "producer toolchain") typically
acts as both Producer (it writes the manifest) and single-tenant
Consumer (it validates the manifest and writes a lockfile for its
own pack).

Section-level conformance summaries
([Section 4.9](#49-conformance-requirements-manifest),
[Section 5.7](#57-conformance-requirements-lockfile),
[Section 6.9](#69-conformance-requirements-governance),
[Section 7.11](#711-conformance-requirements-resolution),
[Section 8.7](#87-conformance-requirements-primitives-and-targets))
are reader-aids that restate the Appendix C rows for the section's
class. Appendix C is the canonical source of truth; on any
conflict between a section summary and Appendix C, Appendix C
wins.

### 11.2 How to claim conformance

An implementation claiming OpenAPM v0.1 conformance MUST publish a
conformance statement identifying:

1. Which conformance class(es) it claims.
2. The version of the specification it conforms to (`v0.1`).
3. The list of OPTIONAL features it implements.
4. Any limitations or non-conformance points, with rationale.

### 11.3 Enumerated requirements by class

#### 11.3.1 Producer

[req-mf-001](#req-mf-001), [req-mf-002](#req-mf-002),
[req-mf-003](#req-mf-003), [req-mf-004](#req-mf-004),
[req-mf-005](#req-mf-005), [req-mf-014](#req-mf-014),
[req-mf-015](#req-mf-015), [req-mf-017](#req-mf-017),
[req-mf-021](#req-mf-021), [req-ext-002](#req-ext-002),
[req-pr-004](#req-pr-004), [req-pr-005](#req-pr-005) (SHOULD).

#### 11.3.2 Consumer

[req-mf-006](#req-mf-006), [req-mf-007](#req-mf-007),
[req-mf-008](#req-mf-008), [req-mf-009](#req-mf-009),
[req-mf-010](#req-mf-010), [req-mf-011](#req-mf-011),
[req-mf-012](#req-mf-012), [req-mf-013](#req-mf-013),
[req-mf-016](#req-mf-016), [req-mf-018](#req-mf-018),
[req-mf-019](#req-mf-019), [req-mf-020](#req-mf-020),
[req-mf-021](#req-mf-021), [req-ext-001](#req-ext-001),
[req-lk-001](#req-lk-001), [req-lk-002](#req-lk-002),
[req-lk-003](#req-lk-003), [req-lk-004](#req-lk-004),
[req-lk-005](#req-lk-005), [req-lk-006](#req-lk-006),
[req-lk-007](#req-lk-007) (SHOULD), [req-lk-008](#req-lk-008),
[req-lk-009](#req-lk-009), [req-lk-010](#req-lk-010),
[req-lk-011](#req-lk-011), [req-lk-012](#req-lk-012),
[req-lk-013](#req-lk-013), [req-lk-014](#req-lk-014),
[req-lk-015](#req-lk-015), [req-lk-016](#req-lk-016),
[req-lk-017](#req-lk-017), [req-lk-018](#req-lk-018) (SHOULD),
[req-lk-019](#req-lk-019),
[req-rs-001](#req-rs-001), [req-rs-002](#req-rs-002),
[req-rs-003](#req-rs-003), [req-rs-004](#req-rs-004),
[req-rs-005](#req-rs-005), [req-rs-006](#req-rs-006),
[req-rs-007](#req-rs-007), [req-rs-008](#req-rs-008),
[req-rs-009](#req-rs-009), [req-rs-010](#req-rs-010),
[req-rs-011](#req-rs-011), [req-rs-012](#req-rs-012),
[req-rs-013](#req-rs-013), [req-rs-014](#req-rs-014),
[req-pr-001](#req-pr-001), [req-pr-002](#req-pr-002),
[req-pr-003](#req-pr-003), [req-tg-001](#req-tg-001),
[req-tg-002](#req-tg-002), [req-tg-003](#req-tg-003),
[req-tg-004](#req-tg-004), [req-sc-001](#req-sc-001),
[req-sc-002](#req-sc-002), [req-sc-003](#req-sc-003),
[req-sc-004](#req-sc-004), [req-sc-005](#req-sc-005),
[req-sc-006](#req-sc-006), [req-sc-007](#req-sc-007),
[req-sc-008](#req-sc-008) (SHOULD), [req-sc-009](#req-sc-009),
[req-sc-010](#req-sc-010), [req-sc-011](#req-sc-011),
[req-sc-012](#req-sc-012), [req-cf-001](#req-cf-001),
[req-cf-002](#req-cf-002).

#### 11.3.3 Registry

<a id="req-rg-001"></a>
**[req-rg-001]** A conforming **Registry** implementation
(reserved for v0.2 wire normativity) MUST serve archive bytes
such that the SHA-256 of those bytes equals the digest the
Registry advertises for the version, and MUST NOT mutate previously
published `(name, version)` bytes. When a Registry receives a
publish request for an `(name, version)` it has previously served,
the Registry MUST either (a) reject the publish request with a
diagnostic identifying the existing version, or (b) accept it
ONLY if the submitted archive bytes are byte-identical to the
previously-served bytes (idempotent republish). A Registry MUST
NOT replace the bytes of a previously-served `(name, version)`
under any circumstance. This is the trust anchor on which
[req-lk-013](#req-lk-013) and [req-rs-009](#req-rs-009) depend;
v0.2 will formalise the surrounding HTTP wire envelope.

#### 11.3.4 Governance

[req-pl-001](#req-pl-001), [req-pl-002](#req-pl-002),
[req-pl-003](#req-pl-003), [req-pl-004](#req-pl-004),
[req-pl-005](#req-pl-005), [req-pl-006](#req-pl-006),
[req-pl-007](#req-pl-007), [req-pl-008](#req-pl-008),
[req-pl-009](#req-pl-009), [req-pl-010](#req-pl-010),
[req-pl-011](#req-pl-011), [req-pl-012](#req-pl-012),
[req-pl-013](#req-pl-013), [req-pl-014](#req-pl-014).

### 11.4 Worked conformance examples (informative)

#### 11.4.1 Producer example

A minimal artifact a Producer emits:

```yaml
# apm.yml
name: contoso/security-baseline
version: "2.3.1"
description: Security baseline skills and instructions for contoso projects.
license: MIT
target: [copilot, claude]

dependencies:
  apm:
    - contoso/common-prompts#^1.0.0
```

This artifact satisfies [req-mf-001](#req-mf-001),
[req-mf-002](#req-mf-002), [req-mf-003](#req-mf-003),
[req-mf-004](#req-mf-004), [req-mf-005](#req-mf-005).

#### 11.4.2 Consumer example

A Consumer reading the manifest above produces the lockfile:

```yaml
lockfile_version: "2"
generated_at: "2026-05-10T20:14:00+00:00"
apm_version: "0.7.0"
dependencies:
  - repo_url: github.com/contoso/common-prompts
    resolved_commit: "a1b2c3d4e5f6789012345678901234567890abcd"
    resolved_ref: "^1.0.0"
    constraint: "^1.0.0"
    resolved_tag: v1.4.2
    resolved_at: "2026-05-10T20:14:00+00:00"
    tree_sha256: "sha256:0102030405060708091011121314151617181920212223242526272829303132"
    depth: 1
    deployed_files:
      - .github/prompts/review.prompt.md
    deployed_file_hashes:
      .github/prompts/review.prompt.md: "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
```

This artifact satisfies [req-lk-001](#req-lk-001),
[req-lk-002](#req-lk-002), [req-lk-003](#req-lk-003),
[req-lk-008](#req-lk-008), [req-lk-011](#req-lk-011),
[req-lk-012](#req-lk-012), [req-lk-015](#req-lk-015),
[req-lk-016](#req-lk-016), and (via the resolution that produced
it) [req-rs-001](#req-rs-001), [req-rs-002](#req-rs-002).

#### 11.4.3 Governance example

The following policy chain is evaluated by a Governance
implementation; an install that resolves to the lockfile in
[Section 11.4.2](#1142-consumer-example) is accepted (no
unbounded direct deps, dependency in `contoso/*` allow-list) and is
written to disk:

```yaml
name: contoso-baseline
enforcement: block
fetch_failure: block
dependencies:
  allow:
    - contoso/*
  require_pinned_constraint: true
  max_depth: 25
```

This evaluation exercises [req-pl-002](#req-pl-002),
[req-pl-005](#req-pl-005), [req-pl-007](#req-pl-007),
[req-pl-008](#req-pl-008).

#### 11.4.4 Registry example

Reserved for v0.2 wire-format normativity. A Registry claiming
conformance against the v0.1 trust-anchor requirement
([req-rg-001](#req-rg-001)) MUST publish an addendum statement
enumerating the immutability guarantees and the digest algorithm
served.

---

## 12. Conformance test methodology

### 12.1 Statement IDs

Every normative MUST, SHOULD, or MAY statement in this document
carries a stable `id="req-xxx"` anchor immediately preceding the
statement. These IDs are the contract between the specification and
its conformance suite: the test suite cites IDs, the specification
publishes them.

### 12.2 Hybrid binding (statement-anchored + fixture-anchored)

This specification adopts a **hybrid** conformance binding:

- Statement-anchored: every MUST/SHOULD/MAY carries a `req-xxx` ID.
  Conformance-suite tests reference the ID either in a docstring or
  via a pytest marker (`@pytest.mark.req("req-mf-005")`).
- Fixture-anchored: a subset of requirements that lend themselves to
  black-box file-in / file-out testing (round-trip, malicious
  archive, merge-table, semver dialect) MUST additionally carry a
  fixture directory under `tests/fixtures/spec-conformance/`. The
  seed fixture tree shipped at spec publication is enumerated in
  [Section 12.4](#124-fixture-layout-informative).

### 12.3 CI binding

A single CI job named `Spec conformance` is RECOMMENDED. The job:

1. Treats the HTML requirement anchors (`<a id="req-XXX"></a>`) in the
   spec body as the canonical statement list, and treats both the
   informative machine-readable manifest at
   [`docs/public/specs/manifests/openapm-v0.1.requirements.yml`](/apm/specs/manifests/openapm-v0.1.requirements.yml)
   (see [Section 12.6](#126-machine-readable-conformance-manifest-reserved-for-v02))
   and the [Appendix C](#appendix-c-index-of-normative-statements)
   table as derived projections that MUST agree with the canonical
   anchors. The v0.2 promotion will move the manifest from informative
   to normative.
2. Walks the conformance suite for ID references (docstrings,
   markers, or fixture directory names) and builds the set of
   referenced IDs.
3. Fails if any declared ID has zero references (orphan in the
   spec) or any referenced ID is not declared (orphan in the
   tests).
4. As a guard against drift in the non-normative companions, the
   same job MAY fail when any `MUST`, `SHOULD`, or `MAY` token
   appears in `docs/src/content/docs/reference/**` (the companion
   pages SHOULD NOT carry normative keywords).

<a id="req-cf-002"></a>
**[req-cf-002]** A **Consumer** or **Producer** claiming OpenAPM
v0.1 conformance MUST publish a conformance statement (see
[Section 11.2](#112-how-to-claim-conformance)) that cites the test
invocation exercising every `req-XXX` statement in its declared
class against the seed fixture tree under
`tests/fixtures/spec-conformance/`. The conformance statement MUST
list, for each `req-XXX` in scope, the fixture path and the
assertion that exercises it.

### 12.4 Fixture layout (informative)

The conformance seed fixture tree shipped with this specification
lives at `tests/fixtures/spec-conformance/`. Concrete on-disk
files:

```
tests/fixtures/spec-conformance/
  README.md
  manifest/
    valid-minimal.yml
    invalid-missing-name.yml
    invalid-no-source-key.yml
    x-extension-roundtrip.yml
  lockfile/
    v1-git-only.yml
    v2-with-registry.yml
    round-trip-unknown-fields.yml
  policy/
    valid-extends.yml
    invalid-extends-cycle.yml
  resolution/
    semver-dialect.json
```

Conformance-suite expansion (additional fixtures for archive
path-traversal, merge-table cases, etc.) tracks here in subsequent
revisions; the seed set above is the v0.1 minimum that
implementations can run against immediately.

### 12.5 Round-trip conformance (normative)

<a id="req-cf-001"></a>
**[req-cf-001]** A conforming **Consumer** implementation MUST
satisfy an **idempotent round-trip** on any conforming manifest
and lockfile: re-parsing and re-serialising either artifact MUST
produce a byte-equivalent file (modulo trailing newline and YAML
flow-style cosmetics that the implementation is permitted to
canonicalise per [Section 4.3.4](#434-canonical-normalisation-writer-requirements) and
[Section 5.2](#52-per-entry-fields)). Unknown top-level keys, `x-*`
extension entries (per [req-ext-001](#req-ext-001),
[req-lk-014](#req-lk-014)), and fields the implementation does
not understand MUST be preserved verbatim across round-trip.

### 12.6 Machine-readable conformance manifest (reserved for v0.2)

A machine-readable manifest enumerating every normative
requirement, its keyword (MUST/SHOULD/MAY), its class, and its
associated fixture path is **reserved for v0.2**. The shape will
permit a conformance-suite runner to enumerate requirements
without parsing the prose, and will permit cross-implementation
result aggregation. v0.1 implementations satisfy the
"enumerable requirements" property via the prose anchors and the
[Appendix C](#appendix-c-index-of-normative-statements) index
table.

As of v0.1.1 an **informative** companion manifest ships at
[`docs/public/specs/manifests/openapm-v0.1.requirements.yml`](/apm/specs/manifests/openapm-v0.1.requirements.yml)
with the shape sketched above (id, keyword, section,
conformance_class, plus optional fixture/oracle paths and
round-trip carve-outs). The companion is informative and exists
to seed the v0.2 normative promotion; it is also the trip wire the
spec-conformance CI job uses to detect silent drift between the
canonical spec anchors, the Appendix C reader-aid table, and the
test marker coverage. Implementations MAY consume it in v0.1 but
MUST NOT depend on its presence for normative conformance until
v0.2 lifts the reservation.

---

## Citing this specification

External documents, tooling, and conformance statements MUST cite
this specification using a stable URL. Three shortlinks are
provided under the published docs site:

| URL                                                  | Resolves to                  | Use when |
|------------------------------------------------------|------------------------------|----------|
| `https://microsoft.github.io/apm/spec/v0.1`         | This document (v0.1)         | Toolchain or test fixture pin. Versioned URLs are immortal: a versioned URL never moves, never 404s, never redirects to a different version. |
| `https://microsoft.github.io/apm/spec/latest`       | Newest ratified version      | Human citation in prose. Toolchains MUST NOT pin to `latest`; pin to a versioned URL. |
| `https://microsoft.github.io/apm/spec`              | Alias of `latest`            | Short prose citation. Same restriction as `latest` -- do not pin tooling. |

The JSON Schemas published alongside this specification (Appendix
A) are themselves identified by the `$id` URL embedded in each
schema. Toolchains MUST pin to the `$id` URL verbatim; the schema
files are byte-immortal at those URLs for the lifetime of this
version.

## Appendix A. Normative JSON Schemas (inline)

The machine-readable schemas backing this specification are
published alongside this document and are normative.

| Schema                | Authoritative source (in-tree)                                                                       |
|-----------------------|------------------------------------------------------------------------------------------------------|
| Manifest (`apm.yml`)  | [`schemas/manifest-v0.1.schema.json`](/apm/specs/schemas/manifest-v0.1.schema.json) (JSON Schema 2020-12).    |
| Lockfile (`apm.lock.yaml`) | [`schemas/lockfile-v0.1.schema.json`](/apm/specs/schemas/lockfile-v0.1.schema.json) (JSON Schema 2020-12). |
| Policy (`apm-policy.yml`) | [`schemas/policy-v0.1.schema.json`](/apm/specs/schemas/policy-v0.1.schema.json) (JSON Schema 2020-12).   |
| Claude-Code marketplace (informational, emitted output) | `tests/fixtures/schemas/claude-code-marketplace.schema.json`         |
| Claude-Code plugin (informational, emitted output)      | `tests/fixtures/schemas/claude-code-plugin.schema.json`              |

The reference Python validator `src/apm_cli/policy/schema.py`
remains in-tree as a **non-normative cross-reference** for
implementers; the JSON Schema is authoritative. Schemas for
manifest and lockfile validation are JSON-Schema-only in v0.1; a
reference Python validator MAY be added in a future minor revision
without normative effect.

Where a JSON Schema and the prose of this specification disagree,
the **prose** is authoritative and the schema is treated as an
errata candidate.

---

## Appendix B. Registry HTTP API (reserved for v0.2)

The registry HTTP API is **non-normative** in v0.1.

The companion page
[`registry-http-api.md`](../../reference/registry-http-api/) is
informational. v0.2 of this specification will define the wire
contract normatively once independent server implementations exist.
Until then, conforming Consumers MAY implement the wire contract
described in the companion page but MUST NOT claim normative
Registry conformance against v0.1, with the single exception of
the trust-anchor expectation in [req-rg-001](#req-rg-001).

Reserved for inclusion in v0.2 (sketch only, non-normative in
v0.1):

- Archive container binding (`application/gzip` over `tar`; reject
  `application/zip`; see also [req-sc-004](#req-sc-004)).
- Publisher attestation envelope (in-toto / SLSA; binds version to
  publisher identity; see
  [Section 10.12](#1012-publisher-provenance-and-attestations-reserved-for-v02)).
- Yank / withdrawal semantics (see
  [Section 7.9](#79-version-withdrawal-reserved-for-v02)).

The class slot is reserved so that v0.2 does not require
renumbering of conformance classes.

---

## Appendix C. Index of normative statements

| ID                                       | Keyword | Section | Class       |
|------------------------------------------|---------|---------|-------------|
| [req-mf-001](#req-mf-001)                | MUST    | 4.1     | producer    |
| [req-mf-002](#req-mf-002)                | MUST    | 4.1     | producer    |
| [req-mf-003](#req-mf-003)                | MUST    | 4.1     | producer    |
| [req-mf-004](#req-mf-004)                | SHOULD  | 4.1     | producer    |
| [req-mf-005](#req-mf-005)                | MUST    | 4.2.1   | producer    |
| [req-mf-006](#req-mf-006)                | MUST    | 4.1     | consumer    |
| [req-mf-007](#req-mf-007)                | MUST    | 4.3.1   | consumer    |
| [req-mf-008](#req-mf-008)                | MUST    | 4.3.3   | consumer    |
| [req-mf-009](#req-mf-009)                | MUST    | 4.3.4   | consumer    |
| [req-mf-010](#req-mf-010)                | MUST    | 4.3.2   | consumer    |
| [req-mf-011](#req-mf-011)                | MUST    | 4.3.2   | consumer    |
| [req-mf-012](#req-mf-012)                | MUST    | 4.3.6   | consumer    |
| [req-mf-013](#req-mf-013)                | MUST    | 4.5     | consumer    |
| [req-mf-014](#req-mf-014)                | MUST    | 4.2.3   | producer    |
| [req-mf-015](#req-mf-015)                | MUST    | 4.2.3   | producer    |
| [req-mf-016](#req-mf-016)                | MUST    | 4.3.5   | consumer    |
| [req-mf-017](#req-mf-017)                | MUST    | 4.7     | producer    |
| [req-mf-018](#req-mf-018)                | MUST    | 4.6.1   | consumer    |
| [req-mf-019](#req-mf-019)                | MUST    | 4.2.4   | consumer    |
| [req-mf-020](#req-mf-020)                | MUST    | 4.1     | consumer    |
| [req-mf-021](#req-mf-021)                | MUST    | 4.8     | producer    |
| [req-ext-001](#req-ext-001)              | MUST    | 4.1     | consumer    |
| [req-ext-002](#req-ext-002)              | MUST    | 4.1     | producer    |
| [req-lk-001](#req-lk-001)                | MUST    | 5.1     | consumer    |
| [req-lk-002](#req-lk-002)                | MUST    | 5.4     | consumer    |
| [req-lk-003](#req-lk-003)                | MUST    | 5.2     | consumer    |
| [req-lk-004](#req-lk-004)                | MUST    | 5.4     | consumer    |
| [req-lk-005](#req-lk-005)                | MUST    | 5.5     | consumer    |
| [req-lk-006](#req-lk-006)                | MUST    | 5.5     | consumer    |
| [req-lk-007](#req-lk-007)                | SHOULD  | 5.5     | consumer    |
| [req-lk-008](#req-lk-008)                | MUST    | 5.6     | consumer    |
| [req-lk-009](#req-lk-009)                | MUST    | 5.6     | consumer    |
| [req-lk-010](#req-lk-010)                | MUST    | 5.6     | consumer    |
| [req-lk-011](#req-lk-011)                | MUST    | 5.2     | consumer    |
| [req-lk-012](#req-lk-012)                | MUST    | 5.2     | consumer    |
| [req-lk-013](#req-lk-013)                | MUST    | 5.2     | consumer    |
| [req-lk-014](#req-lk-014)                | MUST    | 5.2     | consumer    |
| [req-lk-015](#req-lk-015)                | MUST    | 5.6.4   | consumer    |
| [req-lk-016](#req-lk-016)                | MUST    | 5.2     | consumer    |
| [req-lk-017](#req-lk-017)                | MUST    | 5.2     | consumer    |
| [req-lk-018](#req-lk-018)                | SHOULD  | 5.5     | consumer    |
| [req-lk-019](#req-lk-019)                | MUST    | 5.2     | consumer    |
| [req-pl-001](#req-pl-001)                | MUST    | 6.1     | governance  |
| [req-pl-002](#req-pl-002)                | MUST    | 6.2     | governance  |
| [req-pl-003](#req-pl-003)                | MUST    | 6.4     | governance  |
| [req-pl-004](#req-pl-004)                | MUST    | 6.4     | governance  |
| [req-pl-005](#req-pl-005)                | MUST    | 6.5     | governance  |
| [req-pl-006](#req-pl-006)                | MUST    | 6.4     | governance  |
| [req-pl-007](#req-pl-007)                | MUST    | 6.3.1   | governance  |
| [req-pl-008](#req-pl-008)                | MUST    | 6.3.1   | governance  |
| [req-pl-009](#req-pl-009)                | MUST    | 6.6     | governance  |
| [req-pl-010](#req-pl-010)                | MUST    | 6.2     | governance  |
| [req-pl-011](#req-pl-011)                | MUST    | 6.1.1   | governance  |
| [req-pl-012](#req-pl-012)                | MUST    | 6.1.1   | governance  |
| [req-pl-013](#req-pl-013)                | MUST    | 6.8     | governance  |
| [req-pl-014](#req-pl-014)                | MUST    | 6.8     | governance  |
| [req-pl-015](#req-pl-015)                | MUST    | 6.3.5   | governance  |
| [req-rs-001](#req-rs-001)                | MUST    | 7.2     | consumer    |
| [req-rs-002](#req-rs-002)                | MUST    | 7.3     | consumer    |
| [req-rs-003](#req-rs-003)                | MUST    | 7.3     | consumer    |
| [req-rs-004](#req-rs-004)                | MUST    | 7.5     | consumer    |
| [req-rs-005](#req-rs-005)                | MUST    | 7.6     | consumer    |
| [req-rs-006](#req-rs-006)                | MUST    | 7.2     | consumer    |
| [req-rs-007](#req-rs-007)                | MUST    | 7.3     | consumer    |
| [req-rs-008](#req-rs-008)                | MUST    | 7.1     | consumer    |
| [req-rs-009](#req-rs-009)                | MUST    | 7.5.1   | consumer    |
| [req-rs-010](#req-rs-010)                | MUST    | 7.2     | consumer    |
| [req-rs-011](#req-rs-011)                | MUST    | 7.7     | consumer    |
| [req-rs-012](#req-rs-012)                | MUST    | 7.7     | consumer    |
| [req-rs-013](#req-rs-013)                | MUST    | 7.2     | consumer    |
| [req-rs-014](#req-rs-014)                | MUST    | 7.3.1   | consumer    |
| [req-pr-001](#req-pr-001)                | MUST    | 8.2     | consumer    |
| [req-pr-002](#req-pr-002)                | MUST    | 8.3     | consumer    |
| [req-pr-003](#req-pr-003)                | MUST    | 8.3     | consumer    |
| [req-pr-004](#req-pr-004)                | MUST    | 7.8     | producer    |
| [req-pr-005](#req-pr-005)                | SHOULD  | 7.8     | producer    |
| [req-tg-001](#req-tg-001)                | MUST    | 8.4     | consumer    |
| [req-tg-002](#req-tg-002)                | MUST    | 8.5     | consumer    |
| [req-tg-003](#req-tg-003)                | MUST    | 8.5     | consumer    |
| [req-tg-004](#req-tg-004)                | MUST    | 4.2.1   | consumer    |
| [req-sc-001](#req-sc-001)                | MUST    | 10.4    | consumer    |
| [req-sc-002](#req-sc-002)                | MUST    | 10.9    | consumer    |
| [req-sc-003](#req-sc-003)                | MUST    | 10.3    | consumer    |
| [req-sc-004](#req-sc-004)                | MUST    | 10.5    | consumer    |
| [req-sc-005](#req-sc-005)                | MUST    | 10.3    | consumer    |
| [req-sc-006](#req-sc-006)                | MUST    | 4.2.3   | consumer    |
| [req-sc-007](#req-sc-007)                | MUST    | 10.3    | consumer    |
| [req-sc-008](#req-sc-008)                | SHOULD  | 10.3    | consumer    |
| [req-sc-009](#req-sc-009)                | MUST    | 10.13   | consumer    |
| [req-sc-010](#req-sc-010)                | MUST    | 10.13   | consumer    |
| [req-sc-011](#req-sc-011)                | MUST    | 10.14   | consumer    |
| [req-sc-012](#req-sc-012)                | MUST    | 10.14   | consumer    |
| [req-rg-001](#req-rg-001)                | MUST    | 11.3.3  | registry    |
| [req-cf-001](#req-cf-001)                | MUST    | 12.5    | consumer    |
| [req-cf-002](#req-cf-002)                | MUST    | 12.3    | consumer    |

**Total normative statements: 95** (90 MUST, 5 SHOULD).

---

## Appendix D. Revision history

| Version | Date       | Changes                                                  |
|---------|------------|----------------------------------------------------------|
| 0.1     | 2026-05-10 | Initial editor's Working Draft.                          |
| 0.1-r2  | 2026-05-17 | Round-2 adversarial revision. Closed mandatory FOLDs F1-F10: pinned semver dialect (node-semver + semver 2.0.0); tri-modal transitive conflict resolution; vendor-host neutrality (default_host, pluggable policy discovery, host class via PSL+aliases); hash envelope on every stored hash; canonical git tree-hash definition; mirror-tolerant fetch; producer release contract (tag-version alignment, SHOULD-sign); update operation semantics; lockfile_version monotonicity; reserved-slot prose for 11 deferrals (workspaces, x-* extensions, machine-readable conformance, update --aggressive, frozen-default flip, target registry companion, version yank, attestations, registry HTTP, mirror-tolerance, .agents/ partition); inline JSON Schemas in Appendix A; YAML safe subset; archive container binding; credential redaction. Conformance-statement count: 56 -> 83. Companion seed fixture tree shipped under `tests/fixtures/spec-conformance/`. |
| 0.1.1   | 2026-05-24 | v1.1 editorial+defensive fold. Closed convergent round-2 followups: Section 12.3 CI-binding MUST-for-claim (req-cf-002); req-mf-019 class reclassification (producer -> consumer); three stale heading labels in req-cf-001 and Appendix E.4; depEntry oneOf source-key requirement plus new fixture `manifest/invalid-no-source-key.yml`; normative-count reconciliation across Section 1.3, Appendix C trailer, and this row; bare-hex pattern anchored to exactly 64 hex characters; req-sc-007 redaction scope extended to packed bundles, lockfiles, and audit records, plus producer secret-pattern refuse-to-pack rule; workspaces MUST-NOT-use in v0.1 (req-mf-021); nest-mode reject-in-v0.1 (req-rs-013); tag-name regex tightened to the semver.org 2.0.0 reference grammar; build-metadata tie-break rule (req-rs-014); mirror-tolerance editorial note (replicate-verbatim); req-rg-001 cross-references added in Section 7.5.1 and Section 10.5; bare-hex reader-tolerance deprecation horizon; interoperability informative note Section 6.1.2; conformance-summary precedence rule in Section 11.1; wildcard typo `x.y.x` -> `x.y.z`; resolved_by worked-example fragment in Section 7.4. Statement count: 83 -> 87. Drift-detection scaffolding lands in-spec and in-tree (informative machine-readable manifest at `docs/public/specs/manifests/openapm-v0.1.requirements.yml`; 4-way orphan_check + spec-conformance pytest suite + generated `CONFORMANCE.{md,json}` at repo root); Section 12.3 language updated to identify HTML anchors as the canonical source. No normative count change. |
| 0.1.2   | 2026-05-28 | Round-3 spec-guardian editorial fold (no new normative statements; statement count remains 87). Section 11.3.2 Consumer enumeration appended `[req-rs-014]` and `[req-cf-002]` (closing drift vs Appendix C). req-lk-005 extended: writers MUST canonicalise the `dependencies` list in ascending lexicographic order of (`repo_url`, `virtual_path`) so frozen-install diffs are stable across implementations. req-sc-003 extended: consumers MUST drop the originating Authorization header before issuing a cross-host-class redirect (closes the mirror-redirect token-leak surface in Section 10.3). req-rg-001 extended with publish-side idempotency clause: a Registry MUST either reject a republish or accept ONLY if bytes are byte-identical to the previously-served bytes. Section 6.2 + Section 6.3.1 defaults pinned: `fetch_failure` defaults to `warn` and `dependencies.require_resolution` defaults to `project-wins` (mirrored as advisory `"default"` annotations in `policy-v0.1.schema.json`). Manifest schema `conflict_resolution` enum aligned to prose: renamed `intersect` -> `intersection-pick`, dropped `nest` from the v0.1 enum (`nest` remains reserved-for-v0.2 per req-rs-013, now noted via schema `$comment`). Mode B silent-extension detector landed in `.github/workflows/spec-conformance.yml` and `tests/spec_conformance/mode_b_detector.sh`; closes the named sole-implementer rot risk by gating PRs that add substantive code under critical paths (`primitives/`, `deps/`, `policy/`, `registry/`, `runtime/`, `install/`, `integration/`) without a spec citation, with auditable `apm-spec-waiver:` opt-out. |
| 0.1.3   | 2026-06-16 | Spec-citation fold for the declarable integrity policy keys. Added two governance MUSTs under a new Section 6.8 "Integrity controls": [req-pl-013] (`security.integrity.require_hashes` -- fail-closed install when a resolved non-local dependency lacks a recorded hash in `apm.lock.yaml`, or the lockfile is absent/unreadable) and [req-pl-014] (`security.audit.fail_on_drift` -- audit exits non-zero on detected or indeterminate drift). Both keys are default-off and merge by logical OR (tighten-not-relax). Added the non-normative Section 6.3.6 `security` field reference and two merge-table rows; renumbered the governance conformance trailer 6.8 -> 6.9. Statement count: 87 -> 89 (84 MUST, 5 SHOULD). NOTE: a sibling spec-citation amendment also edits the shared count sites (Section 1.3, Appendix C trailer, this revision history); whichever lands second reconciles the cumulative total and takes the union of the added Appendix C rows. |
| 0.1.4   | 2026-06-16 | Normative addition (semver-zero `0.x` minor): added `[req-pl-015]` (Section 6.3.5, governance MUST) codifying unmanaged-artifact surfacing completeness -- a governance implementation evaluating policy over a populated primitive target tree MUST surface every file under a managed primitive target directory that is neither recorded in `apm.lock.yaml` nor matched by a configured `unmanaged_files.exclude` glob, each with its unmanaged reason and a supplemental dependency/MCP deny-conflict note where applicable; the inferred primitive type is carried where determinable and omitted otherwise; an excluded path MUST NOT be surfaced even when it also matches a deny pattern. The requirement body is structured as sub-clauses (a)/(b)/(c) so each obligation is individually citable. Added the `unmanaged_files.exclude` row to the Section 6.4 merge table (additive union, deduplicated, parent order preserved). The requirement governs reporting COMPLETENESS only; enforcement stays governed by `unmanaged_files.action`. Reconciled with the sibling 0.1.3 amendment (req-pl-013/req-pl-014): cumulative statement count 89 -> 90 (85 MUST, 5 SHOULD); Appendix C carries the union of all three new governance rows. |
| 0.1.5   | 2026-06-20 | Spec-citation fold for the executable primitive approval gate. Added new Section 10.13 "Executable primitive approval gate" with two consumer MUSTs: [req-sc-009] (deny deployment of any hook, bin, MCP server, or canvas extension from a dependency not listed in the effective `allowExecutables` approval set when the block is present -- fail closed) and [req-sc-010] (persist interactive approval decisions user-locally, not in the project `apm.yml`, so one developer's approval cannot propagate via VCS to teammates). Added rows 11 and 12 to the Section 10.11 summary table. Section 11.3.2 Consumer enumeration and Appendix C updated. Statement count: 90 -> 92 (87 MUST, 5 SHOULD). |
| 0.1.6   | 2026-06-25 | Spec-citation fold for executable trust precedence and audit fidelity. Added Section 10.14 with two consumer MUSTs: [req-sc-011] (executable trust resolves through one deny-wins precedence; an org executables.deny/deny_all overrides any project or user grant; the install gate and the audit MUST reach the identical outcome via the shared resolver) and [req-sc-012] (a required package's audit asserts lockfile presence, not executable deployment; a present-but-withheld required package satisfies the presence requirement and surfaces a distinct withheld-executable signal). Added rows 13 and 14 to the Section 10.11 summary table. Section 11.3.2 and Appendix C updated. Statement count 92 -> 94 (89 MUST, 5 SHOULD). |
| 0.1.7   | 2026-06-27 | Spec-citation fold for lockfile inventory metadata (closes the #1888 Mode-B silent-extension gate). Added [req-lk-019] (Section 5.2, consumer MUST): the optional per-entry `name` and `version` fields are self-asserted inventory metadata only -- preserved on round-trip per [req-lk-011], never a trust anchor, and never an identity, deduplication, or frozen-replay key (identity/replay derive solely from `repo_url`, `resolved_commit`, `resolved_tag`/`constraint`, and the recorded hash envelopes); their presence is additive and MUST NOT change `lockfile_version`. Added the `name` row to the Section 5.2 per-entry field table and broadened the `version` row note to non-semver sources; added `name` to the `entry` `$defs` in `lockfile-v0.1.schema.json` (sibling of `declared_license`). Section 11.3.2 Consumer enumeration and Appendix C updated. Statement count: 94 -> 95 (90 MUST, 5 SHOULD). |

Errata (none at publication).

---

## Appendix E. Editorial reconciliation notes

This appendix collects editorial notes that were inlined in the
v0.1 first draft. They are non-normative; the section bodies they
reference remain authoritative.

**E.1 Manifest top-level `type` field.** [Section 4.2.2](#422-type-advisory)
defines `type` (with values `instructions`, `skill`, `hybrid`, or
`prompts`) as informational in v0.1; it exists so future minor
revisions can attach normative semantics (for example, packaging
filters per type) without a breaking schema change. v0.1 consumers
MUST ignore the value.

**E.2 Target identifier reservation.** The target identifiers
enumerated in the OpenAPM Target Registry companion are reserved
in the v0.1 namespace; vendor extensions MUST use the
`x-<vendor>-<name>` pattern of [req-tg-004](#req-tg-004) to avoid
collision.

**E.3 Lockfile `lockfile_version: "2"` adoption.** Once a
conforming consumer has written `"2"`, it MUST NOT demote to
`"1"` on subsequent rewrites (see
[req-lk-002](#req-lk-002)). The motivation is auditability: a
silently-demoted lockfile would lose the registry-binding
metadata that prompted the v2 upgrade.

**E.4 `resolved_at` non-stability.** The lockfile field
`resolved_at` is advisory and MAY vary across re-resolutions; it
is excluded from canonical-emission stability checks per
[Section 5.6](#56-git-semver-fields-constraint-resolved_tag-resolved_at). Round-
trip conformance ([req-cf-001](#req-cf-001)) treats this field as
permitted-to-vary.

