---
# APM (Agent Package Manager) - Shared Workflow
# Install Microsoft APM packages in your agentic workflow.
#
# This shared workflow normalises packages, single-app inputs, and apps[] (multi-org
# GitHub App credential groups) into one canonical list of credential groups in an
# "apm-prep" job, then fans the "apm" job out one matrix replica per group. Each
# replica mints its own installation token (when an app-id is set), packs only its
# declared packages with microsoft/apm-action, and uploads a uniquely-named artifact.
# Pre-agent-steps then download all bundles and restore them in one apm-action call.
#
# Source of truth: https://github.com/microsoft/apm/blob/main/.github/workflows/shared/apm.md
# apm-action pin:  microsoft/apm-action@v1.7.2
# To check whether a vendored copy is current, compare these two lines.
#
# Documentation: https://microsoft.github.io/apm/integrations/gh-aw/
#
# Three user-facing forms (all valid, additive):
#
# 1. Public + default-token packages (no App credentials):
#
#    imports:
#      - uses: shared/apm.md
#        with:
#          packages:
#            - microsoft/apm-sample-package
#            - github/awesome-copilot/skills/review-and-refactor
#
# 2. Single GitHub App (one org) -- canonical shorthand:
#
#    imports:
#      - uses: shared/apm.md
#        with:
#          app-id: ${{ vars.APP_ID }}
#          private-key: ${{ secrets.APP_PRIVATE_KEY }}
#          owner: my-org
#          packages:
#            - my-org/my-private-skills
#
# 3. Multiple GitHub Apps (cross-org):
#
#    imports:
#      - uses: shared/apm.md
#        with:
#          packages:
#            - microsoft/apm-sample-package
#          apps:
#            - id: acme
#              app-id: ${{ vars.ACME_APP_ID }}
#              private-key: ${{ secrets.ACME_KEY }}
#              owner: acme-org
#              packages:
#                - acme-org/acme-skills/skills/code-review
#            - app-id: ${{ vars.BETA_APP_ID }}
#              private-key: ${{ secrets.BETA_KEY }}
#              owner: beta-org
#              packages:
#                - beta-org/beta-pkg
#
# 4. Slim bundle for a single harness (recommended when the workflow
#    targets one engine -- avoids packing every harness layout):
#
#    imports:
#      - uses: shared/apm.md
#        with:
#          target: copilot
#          packages:
#            - microsoft/apm-sample-package
#
# 5. Pin a specific apm CLI version (overrides the action's built-in default):
#
#    imports:
#      - uses: shared/apm.md
#        with:
#          apm-version: '0.20.0'
#          target: copilot
#          packages:
#            - microsoft/apm-sample-package

import-schema:
  packages:
    type: array
    items:
      type: string
    required: false
    description: >
      Public APM packages or packages reachable via the default token cascade
      (GH_AW_PLUGINS_TOKEN, GH_AW_GITHUB_TOKEN, GITHUB_TOKEN). Optional. At
      least one of `packages`, the single-app inputs, or `apps` must be provided.
      Format: owner/repo or owner/repo/path/to/skill.

  # Single-app convenience form (canonical shorthand for one-org users)
  app-id:
    type: string
    required: false
    description: >
      GitHub App ID. With `private-key`, mints an installation token for the
      packages listed in `packages:`. For multiple orgs, use `apps:` instead.
  private-key:
    type: string
    required: false
    description: >
      PEM private key matching `app-id`. Required when `app-id` is set. Pass via
      a repository or organization secret.
  owner:
    type: string
    required: false
    description: >
      App installation owner. Defaults to the current repository owner when
      omitted. Only used when `app-id` is set.
  repositories:
    type: string
    required: false
    description: >
      Repositories the minted token is scoped to. Comma- or newline-separated.
      Empty defaults to the calling repo or the App installation default scope.
      Note: literal "*" is NOT a wildcard for actions/create-github-app-token;
      leave empty for org-wide access via App installation config.

  # Multi-app form (cross-org)
  apps:
    type: array
    required: false
    description: >
      List of GitHub App credential groups. Each entry mints its own
      installation token and packs its own packages. Use when packages span
      multiple orgs requiring different App installations.
    items:
      type: object
      properties:
        id:
          type: string
          required: false
          description: >
            Stable identifier used for matrix-row and artifact naming.
            Auto-derived from `owner` (slugified) when omitted. Required when
            two entries share the same owner.
        app-id:
          type: string
          required: true
        private-key:
          type: string
          required: true
        owner:
          type: string
          required: false
        repositories:
          type: string
          required: false
        packages:
          type: array
          items:
            type: string
          required: true

  # APM compilation target (which agent harness layouts to deploy)
  target:
    type: string
    required: false
    default: all
    description: >
      Target harness(es) for APM compilation. Controls which agent config
      directories are generated in the bundle. Single token or comma-separated
      list. Valid tokens: copilot, claude, cursor, codex, opencode, gemini,
      windsurf, agent-skills, all. Default: all (every supported harness).
      Set this to match the engine your gh-aw workflow targets for smaller,
      faster bundles. The shared workflow runs apm-action in isolated mode,
      so any apm.yml in the consumer repo is intentionally ignored -- this
      input is the sole target signal.

  # apm CLI version (overrides apm-action's pinned default)
  apm-version:
    type: string
    required: false
    # MAINTENANCE: this default MUST mirror the apm-version default shipped
    # by the pinned microsoft/apm-action ref used in the Pack and Restore
    # steps below. gh-aw substitutes this value at compile time when a
    # consumer omits apm-version, so an empty string is never forwarded to
    # apm-action (an empty apm-version floats the action to 'latest', the
    # opposite of the pinned default). Bump this in lockstep with the action.
    default: '0.12.4'
    description: >
      apm CLI version for apm-action to install, as a bare semver tag (e.g.
      '0.12.4'); pass 'latest' to opt into floating to the newest release.
      Omit to use apm-action's pinned default. Applied to both the Pack and
      Restore apm-action steps so the CLI version cannot skew between packing
      and restoring.

jobs:
  apm-prep:
    runs-on: ubuntu-slim
    needs: [activation]
    permissions: {}
    outputs:
      matrix: ${{ steps.compute.outputs.matrix }}
    steps:
      # SECURITY (S3): the matrix written to $GITHUB_OUTPUT below carries
      # NO secret values -- only routing metadata (id, kind, index, owner,
      # repositories, packages, has-app). Credentials are resolved per-row
      # in the apm job via $GITHUB_ENV (see "Resolve credentials" step).
      # This avoids GitHub Actions' cross-job output redaction filter
      # (HostContext.SecretMasker.MaskSecrets in actions/runner), which
      # silently strips any job output whose value contains a registered
      # secret substring and emits "Skip output '<name>' since it may
      # contain secret." See: docs/.../security-guides/security-hardening.
      - name: Compute APM credential-group matrix
        id: compute
        env:
          AW_APM_PACKAGES: ${{ github.aw.import-inputs.packages }}
          AW_APM_APPS: ${{ github.aw.import-inputs.apps }}
          AW_APM_LEGACY_APP_ID: ${{ github.aw.import-inputs.app-id }}
          AW_APM_LEGACY_OWNER: ${{ github.aw.import-inputs.owner }}
          AW_APM_LEGACY_REPOS: ${{ github.aw.import-inputs.repositories }}
        run: |
          set -euo pipefail
          packages_json=${AW_APM_PACKAGES:-null}
          apps_json=${AW_APM_APPS:-null}
          legacy_id=${AW_APM_LEGACY_APP_ID:-}

          # gh-aw substitutes `${{ github.aw.import-inputs.packages }}` at
          # compile time using Go's default slice formatter, which emits
          # `[a b c]` (space-separated, no quotes) instead of valid JSON.
          # That breaks `jq --argjson` below. Repair string-array inputs
          # in place; leave already-valid JSON untouched. apps[] (objects)
          # is not repairable this way -- consumers must use the legacy
          # single-app inputs until upstream gh-aw exposes a JSON-encoding
          # helper for import-inputs.
          repair_string_array() {
            local raw="$1"
            if [ -z "$raw" ] || [ "$raw" = "null" ]; then
              echo "$raw"; return
            fi
            if printf '%s' "$raw" | jq -e 'type=="array"' >/dev/null 2>&1; then
              echo "$raw"; return
            fi
            python3 -c 'import json, re, sys; s=sys.argv[1].strip(); s=s[1:-1] if s.startswith("[") and s.endswith("]") else s; print(json.dumps([t for t in re.split(r"[\s,]+", s) if t]))' "$raw"
          }
          packages_json=$(repair_string_array "$packages_json")

          groups=$(jq -nc \
            --argjson packages "$packages_json" \
            --argjson apps "$apps_json" \
            --arg legacy_id "$legacy_id" \
            --arg legacy_owner "${AW_APM_LEGACY_OWNER:-}" \
            --arg legacy_repos "${AW_APM_LEGACY_REPOS:-}" \
            'def slug(s): s | gsub("[^a-zA-Z0-9-]"; "-") | ascii_downcase | .[0:32];
             def with_id(g):
               g + (if (g.id // "") == "" then {id: ("auto-" + slug(g.owner // "default"))} else {} end);
             [
               (if (($packages // []) | length) > 0 and $legacy_id == ""
                  then [{id:"default",kind:"default",index:0,owner:"",repositories:"",packages:$packages,("has-app"):"false"}]
                  else [] end),
               (if $legacy_id != ""
                  then [with_id({id:"legacy",kind:"legacy",index:0,owner:$legacy_owner,repositories:$legacy_repos,packages:($packages // []),("has-app"):"true"})]
                  else [] end),
               (($apps // []) | to_entries | map(
                  with_id({
                    id: (.value.id // ""),
                    kind: "apps",
                    index: .key,
                    owner: (.value.owner // ""),
                    repositories: (.value.repositories // ""),
                    packages: (.value.packages // []),
                    ("has-app"): "true"
                  })))
             ] | add // []')

          count=$(echo "$groups" | jq 'length')
          if [ "$count" = "0" ]; then
            echo "::error::shared/apm.md import provided no packages. Add packages: <list>, single-app inputs (app-id + private-key), or apps: <list> in the with: block."
            exit 1
          fi

          dups=$(echo "$groups" | jq -r '[.[].id] | group_by(.) | map(select(length > 1) | first) | join(", ")')
          if [ -n "$dups" ]; then
            echo "::error::duplicate apm group ids after auto-derivation: $dups. Set apps[].id explicitly when two entries share the same owner."
            exit 1
          fi

          while IFS= read -r id; do
            if ! echo "$id" | grep -Eq '^[a-z0-9-]{1,32}$'; then
              echo "::error::invalid apm group id: '$id' (lowercase alphanumeric and dashes, 1-32 chars). Set apps[].id explicitly."
              exit 1
            fi
          done < <(echo "$groups" | jq -r '.[].id')

          # Emit only id + package-count to logs to keep notice output tight.
          # The matrix itself is non-secret under the env-relay design, but a
          # condensed summary remains easier to scan in the run UI.
          {
            echo "matrix={\"group\":$groups}"
          } >> "$GITHUB_OUTPUT"
          printf "::notice::APM matrix: %d credential group(s)\n" "$count"
          echo "$groups" | jq -r '.[] | "  - " + .id + " (" + (.packages | length | tostring) + " package(s))"'

  apm:
    runs-on: ubuntu-slim
    needs: [activation, apm-prep]
    permissions: {}
    strategy:
      fail-fast: false
      matrix: ${{ fromJSON(needs.apm-prep.outputs.matrix) }}
    env:
      # gh-aw text-substitutes these at compile time. They land in the
      # apm job's per-replica env, which the runner masks in logs but
      # does NOT redact-strip the way job outputs are. From here the
      # "Resolve credentials" step picks the right row by matrix.group
      # routing metadata and relays only the resolved values into
      # $GITHUB_ENV. This is the workaround for GitHub Actions silently
      # dropping job outputs whose value contains a registered secret
      # substring (HostContext.SecretMasker output filter).
      AW_APM_LEGACY_APP_ID: ${{ github.aw.import-inputs.app-id }}
      AW_APM_LEGACY_PRIVATE_KEY: ${{ github.aw.import-inputs.private-key }}
      AW_APM_APPS: ${{ github.aw.import-inputs.apps }}
    steps:
      - name: Resolve credentials for this matrix row
        if: ${{ matrix.group.has-app == 'true' }}
        env:
          ROW_KIND: ${{ matrix.group.kind }}
          ROW_INDEX: ${{ matrix.group.index }}
        run: |
          # SECURITY: never `set -x` or `echo "$pk"` in this step. ::add-mask::
          # registers the PEM as a single multi-line substring; the masker will
          # not match individual PEM lines printed in isolation, so any future
          # debug echo of $pk line-by-line would leak the key body in clear text.
          set -euo pipefail
          case "$ROW_KIND" in
            legacy)
              app_id="${AW_APM_LEGACY_APP_ID:-}"
              pk="${AW_APM_LEGACY_PRIVATE_KEY:-}"
              ;;
            apps)
              apps_json="${AW_APM_APPS:-[]}"
              app_id=$(printf '%s' "$apps_json" | jq -r --argjson i "$ROW_INDEX" '.[$i]["app-id"] // ""')
              pk=$(printf '%s' "$apps_json" | jq -r --argjson i "$ROW_INDEX" '.[$i]["private-key"] // ""')
              ;;
            *)
              echo "::error::unexpected apm matrix kind '$ROW_KIND' for row with has-app=true"
              exit 1
              ;;
          esac
          if [ -z "$app_id" ] || [ -z "$pk" ]; then
            echo "::error::missing app-id or private-key for apm row kind=$ROW_KIND index=$ROW_INDEX"
            exit 1
          fi
          # Normalise trailing newline. Bash $(jq ...) strips ALL trailing
          # newlines from PEMs read out of the apps[] JSON, while a direct
          # env-var assignment preserves them. Stripping any tail and adding
          # exactly one makes the legacy and apps paths produce byte-identical
          # ROW_PRIVATE_KEY values so downstream tolerance is irrelevant.
          pk="${pk%$'\n'}"
          # Defence in depth: the PK is already masked because it came from
          # a secrets-context reference at compile time (gh-aw substitutes
          # the configured private-key secret into AW_APM_*), but
          # registering it again here makes the contract explicit and
          # survives any future gh-aw template churn that might lose the
          # secret tag. NOTE: do not write GitHub Actions expression syntax
          # (dollar-doublecurly ... doublecurly) inside this comment.
          # gh-aw v0.76+ harvests such tokens out of bash run-block bodies
          # (even inside `#` comments) and hoists them into the step env,
          # which fails workflow load when the inner expression resolves
          # to a sequence (e.g. wildcard secrets-context references).
          echo "::add-mask::$pk"
          # Use a random heredoc delimiter to eliminate any chance of a PEM
          # line collision terminating the value early. The official docs
          # explicitly warn against fixed delimiters for arbitrary multi-line
          # values: https://docs.github.com/en/actions/using-workflows/workflow-commands-for-github-actions#multiline-strings
          delim="APMPK_$(openssl rand -hex 16)"
          {
            echo "ROW_APP_ID=$app_id"
            printf 'ROW_PRIVATE_KEY<<%s\n' "$delim"
            printf '%s\n' "$pk"
            printf '%s\n' "$delim"
          } >> "$GITHUB_ENV"
      - name: Mint installation token
        id: token
        if: ${{ matrix.group.has-app == 'true' }}
        uses: actions/create-github-app-token@v3.1.1
        with:
          app-id: ${{ env.ROW_APP_ID }}
          private-key: ${{ env.ROW_PRIVATE_KEY }}
          owner: ${{ matrix.group.owner != '' && matrix.group.owner || github.repository_owner }}
          repositories: ${{ matrix.group.repositories }}
      - name: Render package list
        id: list
        env:
          AW_PKG: ${{ toJSON(matrix.group.packages) }}
        run: |
          DEPS=$(echo "$AW_PKG" | jq -r '.[] | "- " + .')
          {
            echo "deps<<APMDEPS"
            printf '%s\n' "$DEPS"
            echo "APMDEPS"
          } >> "$GITHUB_OUTPUT"
      - name: Pack APM packages
        id: pack
        uses: microsoft/apm-action@v1.7.2
        env:
          GITHUB_TOKEN: ${{ steps.token.outputs.token || secrets.GH_AW_PLUGINS_TOKEN || secrets.GH_AW_GITHUB_TOKEN || secrets.GITHUB_TOKEN }}
        with:
          apm-version: ${{ github.aw.import-inputs.apm-version }}
          dependencies: ${{ steps.list.outputs.deps }}
          isolated: 'true'
          pack: 'true'
          archive: 'true'
          target: ${{ github.aw.import-inputs.target }}
          working-directory: /tmp/gh-aw/apm-workspace
      - name: Upload APM bundle artifact
        if: success()
        uses: actions/upload-artifact@v7
        with:
          name: ${{ needs.activation.outputs.artifact_prefix }}apm-${{ matrix.group.id }}
          path: ${{ steps.pack.outputs.bundle-path }}
          retention-days: '1'

steps:
  - name: Download APM bundle artifacts (all groups)
    uses: actions/download-artifact@v8.0.1
    with:
      pattern: ${{ needs.activation.outputs.artifact_prefix }}apm-*
      path: /tmp/gh-aw/apm-bundles
      merge-multiple: false
  - name: Normalise bundle layout (single-artifact flatten workaround)
    env:
      EXPECTED_MATRIX: ${{ needs.apm-prep.outputs.matrix }}
      ARTIFACT_PREFIX: ${{ needs.activation.outputs.artifact_prefix }}
    run: |
      set -euo pipefail
      # actions/download-artifact (>=v5) flattens contents directly into `path/`
      # whenever exactly one artifact matches the pattern, ignoring
      # `merge-multiple: false`. Re-shape into the per-group subdir layout so
      # downstream validation sees a stable structure regardless of matrix size.
      # Upstream reference:
      # https://github.com/actions/download-artifact/blob/v8.0.1/src/download-artifact.ts
      # (see the `isSingleArtifactDownload || mergeMultiple || artifacts.length === 1`
      # branch). Remove this step once download-artifact stops flattening or
      # exposes an opt-out.
      expected_count=$(echo "$EXPECTED_MATRIX" | jq '.group // [] | length')
      if [ "$expected_count" -eq 1 ]; then
        group_id=$(echo "$EXPECTED_MATRIX" | jq -r '.group[0].id')
        # Defence-in-depth: group_id is interpolated into a shell path. apm-prep
        # produces a sanitised id today, but enforce a strict allowlist here so
        # any future schema drift cannot smuggle traversal sequences.
        if ! printf '%s' "$group_id" | grep -Eq '^[A-Za-z0-9_-]+$'; then
          echo "::error::unsafe group_id '$group_id' (must match ^[A-Za-z0-9_-]+$)"
          exit 1
        fi
        group_dir="/tmp/gh-aw/apm-bundles/${ARTIFACT_PREFIX}apm-${group_id}"
        if [ ! -d "$group_dir" ]; then
          mkdir -p "$group_dir"
          find /tmp/gh-aw/apm-bundles -mindepth 1 -maxdepth 1 ! -path "$group_dir" -exec mv {} "$group_dir/" \;
        fi
      fi
  - name: Validate downloaded bundles match matrix manifest
    env:
      EXPECTED_MATRIX: ${{ needs.apm-prep.outputs.matrix }}
      ARTIFACT_PREFIX: ${{ needs.activation.outputs.artifact_prefix }}
    run: |
      set -euo pipefail
      expected=$(echo "$EXPECTED_MATRIX" | jq -r --arg prefix "$ARTIFACT_PREFIX" '.group | map($prefix + "apm-" + .id) | sort | .[]')
      actual=$(ls /tmp/gh-aw/apm-bundles | sort)
      missing=$(comm -23 <(echo "$expected") <(echo "$actual") || true)
      unexpected=$(comm -13 <(echo "$expected") <(echo "$actual") || true)
      if [ -n "$missing" ]; then
        echo "::error::missing APM bundles (group did not pack successfully): $missing"
        exit 1
      fi
      if [ -n "$unexpected" ]; then
        echo "::error::unexpected artifact in apm bundle download (collision attack?): $unexpected"
        exit 1
      fi
  - name: Build bundle list
    id: bundles
    run: |
      set -euo pipefail
      mapfile -t list < <(find /tmp/gh-aw/apm-bundles -name '*.tar.gz' | sort)
      [ ${#list[@]} -gt 0 ] || { echo '::error::no apm bundles found'; exit 1; }
      printf '%s\n' "${list[@]}" > /tmp/gh-aw/apm-bundle-list.txt
  - name: Restore APM packages (all bundles)
    uses: microsoft/apm-action@v1.7.2
    with:
      apm-version: ${{ github.aw.import-inputs.apm-version }}
      bundles-file: /tmp/gh-aw/apm-bundle-list.txt
---

<!--
## APM Packages

This shared workflow installs APM packages in a dedicated `apm` job that runs
in parallel one matrix replica per credential group, packs each group's packages
with `microsoft/apm-action`, and uploads a per-group bundle artifact. The agent
job's pre-agent-steps then download all bundles and restore them in a single
`apm-action` invocation (using the `bundles-file:` input shipped in
`microsoft/apm-action@v1.7.2`).

### How it works

1. **Normalise** (`apm-prep` job): a small jq script merges `packages:`, the
   single-app top-level inputs, and `apps[]` into one canonical list of
   credential groups. Each group has an `id`, optional App credentials, and a
   `packages` list. The matrix size is the number of groups.
2. **Pack per group** (`apm` job, matrix fan-out): each replica conditionally
   mints an installation token (only if `app-id` is set), packs only its declared
   packages, and uploads `apm-<group-id>` as an artifact.
3. **Restore** (agent pre-agent-steps): all `apm-*` artifacts are downloaded,
   validated against the matrix manifest (defends against same-run artifact-name
   collision attacks), and restored in one call via the `bundles-file:` input
   on `microsoft/apm-action@v1.7.2`.

### Authentication

Three forms, additive:

- No App credentials: packages fetched via `GH_AW_PLUGINS_TOKEN || GH_AW_GITHUB_TOKEN || GITHUB_TOKEN`.
- Single App (top-level `app-id` + `private-key` + `owner` + `repositories`):
  one installation token mints for one credential group; canonical shorthand for
  one-org users.
- Multi App (`apps:` array): each entry mints its own installation token and
  packs only its declared packages, enabling cross-org scenarios where each org
  requires a different App installation.
-->
