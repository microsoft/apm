---
title: Inventory and drift
description: Trace every agent file to the package and commit it came from, catch files that changed outside APM, and export an SBOM inventory from the lockfile.
---

This page is for the platform or security team that must answer three questions about
any repository: what do our coding agents load, where did each file come from, and has
anything changed since it was installed? APM answers from the lockfile, `apm.lock.yaml`,
which every repository commits.

## Read provenance from the lockfile

Provenance here means which package and commit a file came from. APM records it; it is
not cryptographically attested. Each locked package carries:

```yaml title="apm.lock.yaml"
- repo_url: microsoft/apm-sample-package
  host: github.com
  resolved_commit: fb2851683be0e0e7711421d518bd8dba23b0b1f6
  resolved_ref: v1.0.0
  version: 1.0.0
  ...
  deployed_file_hashes:
    .agents/skills/style-checker/SKILL.md: sha256:1142700284d253c15e561434362ae6203db08e41ef07e7082386df3651738829
    ...
  content_hash: sha256:744cca54cc8ff7ca90aa1dd621c2f98c6291cd793815afe8518001cc94b8aba9
```

`resolved_commit` is the exact commit installed. `deployed_file_hashes` lists every file
APM wrote and its SHA-256. `content_hash` covers the whole package. Packages installed
from a marketplace also record `discovered_via` (the marketplace name), the `constraint`
you declared and the `resolved_tag`. Field reference:
[Lockfile specification](../../reference/lockfile-spec/).

Because the lockfile is in git, `git log -p apm.lock.yaml` is the history of what every
agent in the repository could load, and who changed it.

## Trace a file to its package

```bash frame="terminal"
apm find --source .claude/skills/style-checker/SKILL.md
```

```text frame="none"
microsoft/apm-sample-package  microsoft/apm-sample-package@v1.0.0
```

`--path` prints the dependency chain instead, to show which declaration pulled a
transitive package in. A file APM did not write exits 1:

```text frame="none"
[x] 'nothing.md' is not tracked by any installed package in apm.lock.yaml.
```

## Detect drift

Drift means the files on disk, or the declarations in `apm.yml`, differ from what the
lockfile records. `apm audit` replays the lockfile into a scratch folder and compares
the result with your working tree. After someone edits a deployed skill by hand:

```bash frame="terminal"
apm audit --ci
```

```text frame="none"
...
| [x]      | content-integrity        | 1 file(s) with hash drift -- delete the edited skill       |
|          |                          | files, then run 'apm install' to restore the locked        |
|          |                          | content (a plain 'apm install' refuses to overwrite an     |
|          |                          | edited skill), then commit the result                      |
| [x]      | drift                    | drift detected: 1 file(s):                                 |
|          |                          | .claude/skills/style-checker/SKILL.md                      |
...
[x] 2 of 9 check(s) failed

[x] Drift detected: 1 file(s)

  modified (1):
    - .claude/skills/style-checker/SKILL.md  [microsoft/apm-sample-package]

  [i] Delete the edited skill files, then run 'apm install' to restore the locked content (a plain 'apm install' refuses to overwrite an edited skill), then commit the result.
```

What a plain `apm install` does to a hand-edited file depends on its kind. It refuses to overwrite an edited skill. It overwrites edited instructions, agents, prompts, commands and rules, and prints a `[!]` warning that names them. Deleting the file and rerunning `apm install` restores every kind; to keep an edit, run `apm export-patch` first. For files a local bundle deployed, delete them and reinstall that bundle with `apm install <bundle-path>`. `apm audit --ci` exits 1 on drift. Plain `apm audit` reports the same drift and exits 0,
unless the org policy sets `security.audit.fail_on_drift: true`. `apm install --frozen`
also fails, before writing anything, when a deployed file differs from its locked hash.

What the replay reports:

| Finding | Meaning |
|---|---|
| `modified` | A deployed file differs from what the lockfile would produce |
| `unintegrated` | The replay produces a file that is missing from disk |
| `orphaned` | A file the lockfile tracks is on disk, but the replay no longer produces it |
| `unrecorded` | A file matches the replay but no lockfile entry claims it |
| `unmanaged-conflict` | A file no lockfile entry claims sits where the replay writes; install skipped it as user-authored |

What it does not see: files APM never deployed (set `unmanaged_files` in the
[policy schema](../../reference/policy-schema/#unmanaged_files) to flag files in agent
folders that no package claims), and changes between runs. Run the check on every pull
request ([Gate pull requests](../enforce-in-ci/)) and on a schedule against the default
branch:

```yaml title=".github/workflows/apm-drift.yml"
on:
  schedule:
    - cron: "0 6 * * *"
jobs:
  drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: microsoft/apm-action@v1
        with:
          setup-only: true
      - run: apm audit --ci --no-fail-fast
```

`apm outdated` covers the other kind of drift: upstream tags that moved past what you
locked.

## Send local edits upstream

When a team has improved an installed skill in place, save the edit before
`apm install --force` restores the locked file:

```bash frame="terminal"
apm export-patch
```

```text frame="none"
[i] .claude/skills/style-checker/SKILL.md -> microsoft/apm-sample-package:.apm/skills/style-checker/SKILL.md
[i] wrote <repo>/patches/microsoft__apm-sample-package@v1.0.0.patch
[*] Exported 1 edit(s) into 1 patch file(s) under <repo>/patches. Apply each with 'git apply' from the package repository root at the base noted in the patch header.
```

Send the patch to the package owner, then run `apm install --force` to restore the locked file.

## Export an inventory

```bash frame="terminal"
apm lock export --format cyclonedx -o sbom.json
```

```text frame="none"
[+] SBOM written to sbom.json (format=cyclonedx)
```

`--format spdx` writes SPDX 2.3 instead of CycloneDX 1.5. The inventory lists one
component per locked package, with its name, version, the commit it resolved to and the
license its `apm.yml` declares. Each component carries a package URL
(`pkg:github/<owner>/<repo>@<commit>` for GitHub, typed for GitLab and Bitbucket,
`pkg:generic` for other hosts, with `#<subpath>` for a package inside a repository) and the
lockfile's SHA-256 content hash (CycloneDX `hashes`, SPDX `checksums`), labelled
`apm-package-tree-sha256`: APM's package tree hash, not an archive digest. The export reads `apm.lock.yaml` and nothing else: no
network, no re-hashing. Pass `--timestamp` or set `SOURCE_DATE_EPOCH` for byte-identical output in
release pipelines.

An inventory is a record of what the lockfile says, not an attestation: it does not
show that the files on disk match, so run `apm audit --ci` in the same pipeline. Attach the
SBOM to each release, or collect one per repository for an organization-wide view.

## Related

- [`apm audit`](../../reference/cli/audit/): flags and output formats.
- [`apm lock export`](../../reference/cli/lock/): SBOM options.
- [Agent supply chain security model](../security/): where drift and inventory fit.
