# PGS project board sync

Glue between the `microsoft/apm` repo and the [APM Roadmap project board](https://github.com/orgs/microsoft/projects/2304).

## What this does

`sync_item.py` reads an issue or PR's labels + milestone and projects them onto five board fields:

| Field | Source labels / signal | Possible values |
|---|---|---|
| Theme | `theme/portability` \| `theme/security` \| `theme/governance` | Portability / Security / Governance |
| Area | `area/*` | one of 14 product areas |
| Kind | `type/*` | Bug / Feature / Docs / Refactor / Architecture / Automation / Release / Performance |
| Priority | `priority/high` \| `priority/low` \| (none) | High / Low / Normal |
| Tier | issue state + milestone title | Now / Next / Later / Shipped |

Tier rules (see `derive_tier()`):
- Closed -> `Shipped`
- Open + open milestone titled `0.9.x`, `0.10.x`, or `0.11.x` -> `Now` (keep `NOW_MILESTONE_PREFIXES` aligned with the active release lines)
- Open + any other open milestone -> `Next`
- Open + no milestone -> `Later`

`backfill.sh` is the one-shot helper for re-baselining the board after a label-taxonomy change.

## How it runs

`.github/workflows/project-sync.yml` triggers on issue + PR `opened|labeled|unlabeled|milestoned|demilestoned|closed|reopened` and on `workflow_dispatch`. The job is gated on the presence of a `theme/*` label so unrelated activity is a no-op.

## One-time setup (org admin)

The workflow authenticates to the org-level project via repo secret `PROJECT_SYNC_PAT`:

1. Generate a fine-grained PAT
2. Scopes: `Projects: Read & Write` (org `microsoft`), `Issues: Read` + `Pull requests: Read` (`microsoft/apm`)
3. Save in repo settings as `PROJECT_SYNC_PAT`

Until the secret exists the workflow runs but the sync step fails (no other side effects).

## Manual ad-hoc sync

```sh
GITHUB_TOKEN=$(gh auth token) python3 scripts/project/sync_item.py --content-id <node_id>
```

To obtain a node ID:

```sh
gh api graphql -f query='query{repository(owner:"microsoft",name:"apm"){issue(number:916){id}}}' --jq '.data.repository.issue.id'
```

## Followups (manual, GraphQL has no view-creation mutation)

- 9-step UI conversion of the default view to a Now/Next/Later board sliced by Theme: see #920
- Secondary views (Triage queue / Good first issues / Shipped log): see #920
