#!/usr/bin/env python3
"""Sync one issue or PR to the APM PGS project board.

Reads labels and milestone from a GitHub issue/PR, then sets the
project's Theme/Area/Kind/Priority/Tier fields accordingly.

Idempotent: safe to call on every label change.

Usage:
    python sync_item.py --content-id <node_id> [--project-id <pvt_id>]

Requires GITHUB_TOKEN env var with project + repo scopes.
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error

PROJECT_ID = "PVT_kwDOAF3p4s4BVoGw"
GQL_URL = "https://api.github.com/graphql"

# Milestone-title prefixes that map to Tier=Now. Keep aligned with the
# active release lines (current development line + still-open patch lines).
# See pyproject.toml `version` and the open milestones in microsoft/apm.
# Anything outside this list with an open milestone falls into Tier=Next.
NOW_MILESTONE_PREFIXES = ("0.9.", "0.10.", "0.11.")

THEME_MAP = {
    "theme/portability": "Portability",
    "theme/security": "Security",
    "theme/governance": "Governance",
}
KIND_MAP = {
    "type/bug": "Bug",
    "type/feature": "Feature",
    "type/docs": "Docs",
    "type/refactor": "Refactor",
    "type/architecture": "Architecture",
    "type/automation": "Automation",
    "type/release": "Release",
    "type/performance": "Performance",
}
PRIORITY_MAP = {
    "priority/high": "High",
    "priority/low": "Low",
}
AREA_NAMES = {
    "area/multi-target", "area/marketplace", "area/package-authoring",
    "area/distribution", "area/mcp-config", "area/content-security",
    "area/lockfile", "area/mcp-trust", "area/audit-policy",
    "area/enterprise", "area/cli", "area/ci-cd", "area/testing",
    "area/docs-site",
}


def gql(query, variables=None):
    token = os.environ["GITHUB_TOKEN"]
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        GQL_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"HTTP {e.code}: {e.read().decode()}\n")
        raise
    if "errors" in data:
        sys.stderr.write(json.dumps(data["errors"], indent=2) + "\n")
        raise SystemExit(2)
    return data["data"]


def fetch_project_meta(project_id):
    q = """
    query($id: ID!) {
      node(id: $id) {
        ... on ProjectV2 {
          fields(first: 50) {
            nodes {
              ... on ProjectV2SingleSelectField {
                id name options { id name }
              }
            }
          }
        }
      }
    }"""
    data = gql(q, {"id": project_id})
    fields = {}
    for node in data["node"]["fields"]["nodes"]:
        if not node:
            continue
        if "options" in node:
            fields[node["name"]] = {
                "id": node["id"],
                "options": {opt["name"]: opt["id"] for opt in node["options"]},
            }
    return fields


def fetch_content(content_id):
    q = """
    query($id: ID!) {
      node(id: $id) {
        __typename
        ... on Issue {
          number title state url repository { nameWithOwner }
          labels(first: 50) { nodes { name } }
          milestone { title state }
        }
        ... on PullRequest {
          number title state url repository { nameWithOwner }
          labels(first: 50) { nodes { name } }
          milestone { title state }
        }
      }
    }"""
    return gql(q, {"id": content_id})["node"]


def add_to_project(project_id, content_id):
    q = """
    mutation($pid: ID!, $cid: ID!) {
      addProjectV2ItemById(input: {projectId: $pid, contentId: $cid}) {
        item { id }
      }
    }"""
    return gql(q, {"pid": project_id, "cid": content_id})["addProjectV2ItemById"]["item"]["id"]


def update_single_select(project_id, item_id, field_id, option_id):
    q = """
    mutation($pid: ID!, $iid: ID!, $fid: ID!, $oid: String!) {
      updateProjectV2ItemFieldValue(input: {
        projectId: $pid, itemId: $iid, fieldId: $fid,
        value: { singleSelectOptionId: $oid }
      }) { projectV2Item { id } }
    }"""
    gql(q, {"pid": project_id, "iid": item_id, "fid": field_id, "oid": option_id})


def clear_field(project_id, item_id, field_id):
    q = """
    mutation($pid: ID!, $iid: ID!, $fid: ID!) {
      clearProjectV2ItemFieldValue(input: {projectId: $pid, itemId: $iid, fieldId: $fid}) {
        projectV2Item { id }
      }
    }"""
    gql(q, {"pid": project_id, "iid": item_id, "fid": field_id})


def derive_tier(content):
    state = content.get("state", "OPEN")
    ms = content.get("milestone")
    if state == "CLOSED":
        return "Shipped"
    if ms and ms.get("state") == "OPEN":
        title = ms["title"]
        if title.startswith(NOW_MILESTONE_PREFIXES):
            return "Now"
        return "Next"
    return "Later"


def derive_field_value(labels, mapping):
    for lab in labels:
        if lab in mapping:
            return mapping[lab]
    return None


def derive_area(labels):
    for lab in labels:
        if lab in AREA_NAMES:
            return lab.split("/", 1)[1]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--content-id", required=True)
    ap.add_argument("--project-id", default=PROJECT_ID)
    args = ap.parse_args()

    fields = fetch_project_meta(args.project_id)
    content = fetch_content(args.content_id)
    if not content:
        sys.stderr.write(f"content not found: {args.content_id}\n")
        return 1

    labels = [n["name"] for n in content["labels"]["nodes"]]
    print(f"Syncing {content.get('repository', {}).get('nameWithOwner')}#{content['number']}: {content['title']}")
    print(f"  labels: {labels}")

    item_id = add_to_project(args.project_id, args.content_id)
    print(f"  item: {item_id}")

    theme = derive_field_value(labels, THEME_MAP)
    kind = derive_field_value(labels, KIND_MAP)
    priority = derive_field_value(labels, PRIORITY_MAP) or "Normal"
    area = derive_area(labels)
    tier = derive_tier(content)

    plan = {"Theme": theme, "Area": area, "Kind": kind, "Priority": priority, "Tier": tier}
    for field_name, value in plan.items():
        field = fields.get(field_name)
        if not field:
            print(f"  ! field {field_name} missing from project")
            continue
        if value is None:
            clear_field(args.project_id, item_id, field["id"])
            print(f"  - {field_name}: cleared")
            continue
        opt_id = field["options"].get(value)
        if not opt_id:
            print(f"  ! {field_name}: option '{value}' not found")
            continue
        update_single_select(args.project_id, item_id, field["id"], opt_id)
        print(f"  + {field_name}: {value}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
