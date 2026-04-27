---
# APM (Agent Package Manager) - Shared Workflow
# Install Microsoft APM packages in your agentic workflow.
#
# This shared workflow creates a dedicated "apm" job (depending on activation) that
# packs packages using microsoft/apm-action and uploads the bundle as an artifact.
# The agent job then downloads and unpacks the bundle as pre-steps.
#
# Documentation: https://github.com/microsoft/APM
#
# Usage:
#   imports:
#     - uses: shared/apm.md
#       with:
#         packages:
#           - microsoft/apm-sample-package
#           - github/awesome-copilot/skills/review-and-refactor
#
# Cross-org private packages with GitHub App authentication:
#   imports:
#     - uses: shared/apm.md
#       with:
#         app-id: ${{ vars.APP_ID }}
#         private-key: ${{ secrets.APP_PRIVATE_KEY }}
#         owner: acme-platform-org
#         repositories: acme-skills
#         packages:
#           - acme-platform-org/acme-skills/skills/code-review

import-schema:
  packages:
    type: array
    items:
      type: string
    required: true
    description: >
      List of APM package references to install.
      Format: owner/repo or owner/repo/path/to/skill.
      Examples: microsoft/apm-sample-package, github/awesome-copilot/skills/review-and-refactor
  app-id:
    type: string
    required: false
    description: >
      GitHub App ID used to mint an installation access token for fetching
      cross-org private APM packages. When set, app-id and private-key together
      override the default GH_AW_PLUGINS_TOKEN / GH_AW_GITHUB_TOKEN / GITHUB_TOKEN
      cascading fallback.
  private-key:
    type: string
    required: false
    description: >
      GitHub App private key (PEM) corresponding to app-id. Required when app-id
      is set. Should be passed via a repository or organization secret.
  owner:
    type: string
    required: false
    description: >
      GitHub App installation owner. Defaults to the current repository owner
      when omitted. Only used when app-id is set.
  repositories:
    type: string
    required: false
    description: >
      Repositories the minted installation token should be scoped to. Accepts a
      single repo name, a comma-separated list, or a newline-separated block.
      When omitted, the token defaults to the calling repository (per
      actions/create-github-app-token). For org-wide access, configure the
      GitHub App installation with all-repositories access and leave this
      empty. Only used when app-id is set.

jobs:
  apm:
    runs-on: ubuntu-slim
    needs: [activation]
    permissions: {}
    steps:
      - name: Generate GitHub App token for APM packages
        id: apm_token
        if: ${{ github.aw.import-inputs.app-id != '' }}
        uses: actions/create-github-app-token@v3.1.1
        with:
          app-id: ${{ github.aw.import-inputs.app-id }}
          private-key: ${{ github.aw.import-inputs.private-key }}
          owner: ${{ github.aw.import-inputs.owner || github.repository_owner }}
          repositories: ${{ github.aw.import-inputs.repositories }}
      - name: Prepare APM package list
        id: apm_prep
        env:
          AW_APM_PACKAGES: '${{ github.aw.import-inputs.packages }}'
        run: |
          DEPS=$(echo "$AW_APM_PACKAGES" | jq -r '.[] | "- " + .')
          {
            echo "deps<<APMDEPS"
            printf '%s\n' "$DEPS"
            echo "APMDEPS"
          } >> "$GITHUB_OUTPUT"
      - name: Pack APM packages
        id: apm_pack
        uses: microsoft/apm-action@v1.4.2
        env:
          GITHUB_TOKEN: ${{ steps.apm_token.outputs.token || secrets.GH_AW_PLUGINS_TOKEN || secrets.GH_AW_GITHUB_TOKEN || secrets.GITHUB_TOKEN }}
        with:
          dependencies: ${{ steps.apm_prep.outputs.deps }}
          isolated: 'true'
          pack: 'true'
          archive: 'true'
          target: all
          working-directory: /tmp/gh-aw/apm-workspace
      - name: Upload APM bundle artifact
        if: success()
        uses: actions/upload-artifact@v7
        with:
          name: ${{ needs.activation.outputs.artifact_prefix }}apm
          path: ${{ steps.apm_pack.outputs.bundle-path }}
          retention-days: '1'

steps:
  - name: Download APM bundle artifact
    uses: actions/download-artifact@v8.0.1
    with:
      name: ${{ needs.activation.outputs.artifact_prefix }}apm
      path: /tmp/gh-aw/apm-bundle
  - name: Find APM bundle path
    id: apm_bundle
    run: echo "path=$(find /tmp/gh-aw/apm-bundle -name '*.tar.gz' | head -1)" >> "$GITHUB_OUTPUT"
  - name: Restore APM packages
    uses: microsoft/apm-action@v1.4.2
    with:
      bundle: ${{ steps.apm_bundle.outputs.path }}
---

<!--
## APM Packages

These packages are installed via a dedicated "apm" job that packs and uploads a bundle,
which the agent job then downloads and unpacks as pre-steps.

### How it works

1. **Pack** (`apm` job): `microsoft/apm-action` installs packages and creates a bundle archive,
   uploaded as a GitHub Actions artifact.
2. **Unpack** (agent job pre-steps): the bundle is downloaded and unpacked via
   `microsoft/apm-action` in restore mode, making all skills and tools available to the AI agent.

### Package format

Packages use the format `owner/repo` or `owner/repo/path/to/skill`:
- `microsoft/apm-sample-package` — organization/repository
- `github/awesome-copilot/skills/review-and-refactor` — organization/repository/path

### Authentication

By default, packages are fetched using the cascading token fallback:
`GH_AW_PLUGINS_TOKEN || GH_AW_GITHUB_TOKEN || GITHUB_TOKEN`.

For cross-org private packages, supply GitHub App credentials via the
`app-id`, `private-key`, `owner`, and `repositories` inputs. When `app-id`
is set, an installation access token is minted with
`actions/create-github-app-token` and used in place of the default cascade.
-->
