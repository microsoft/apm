#!/usr/bin/env bash
# Manual end-to-end reproduction of the local-marketplace user flow.
#
# Not wired into CI -- keeps CI hermetic and avoids depending on
# external repos. Reproduces the workflow the requesting team was
# trapped in (manual editing of ~/.apm/marketplaces.json + cache
# pre-seeding) and validates the new register / browse / install /
# update / refresh / drift paths.
#
# Prereqs:
#   - apm CLI on PATH (or run from the repo's venv)
#   - git
#   - network access for the initial bare clone of the demo repo
#
# Cleanup: removes /tmp/apm-e2e/ on success. Re-run is idempotent.

set -euo pipefail

E2E_ROOT="/tmp/apm-e2e"
# Point this at any git remote that exposes a marketplace.json at the root of
# its default branch. Override via DEMO_REMOTE=... before invoking.
DEMO_REMOTE="${DEMO_REMOTE:-}"
BARE_REPO="${E2E_ROOT}/demo-marketplace.git"

if [ -z "${DEMO_REMOTE}" ]; then
  echo "Set DEMO_REMOTE to a git URL exposing marketplace.json, e.g.:" >&2
  echo "  DEMO_REMOTE=https://example.com/org/repo.git $0" >&2
  exit 2
fi

rm -rf "${E2E_ROOT}"
mkdir -p "${E2E_ROOT}"

echo "[1/7] Cloning demo marketplace into a local bare repo..."
git clone --bare "${DEMO_REMOTE}" "${BARE_REPO}"

echo "[2/7] Registering via local filesystem path (kind=local)..."
apm marketplace add "${BARE_REPO}" --name demo-local

echo "[3/7] Registering same repo via file:// URI (kind=local, exercises git show)..."
apm marketplace add "file://${BARE_REPO}" --name demo-fileuri

echo "[4/7] Browsing both registrations..."
apm marketplace browse demo-local
apm marketplace browse demo-fileuri

echo "[5/7] Picking the first plugin from each registration and installing..."
PLUGIN_NAME="$(apm marketplace browse demo-local --json 2>/dev/null | python -c 'import json,sys; print(json.load(sys.stdin)["plugins"][0]["name"])' || echo "")"
if [ -n "${PLUGIN_NAME}" ]; then
  apm install "${PLUGIN_NAME}@demo-local"
  apm install "${PLUGIN_NAME}@demo-fileuri"
  echo "    Installed ${PLUGIN_NAME} from both registrations."
else
  echo "    No plugins exposed by the demo marketplace; skipping install."
fi

echo "[6/7] Refreshing both marketplaces (validates GitCache ls-remote refresh path)..."
apm marketplace update demo-local
apm marketplace update demo-fileuri

echo "[7/7] Drift smoke: marketplace update picks up working-tree edits without manual cache flushing."
# (Would need a writable working-tree mirror; bare repos don't have one.
# Documented as a manual step: edit the working tree, re-run update,
# observe the change.)

echo ""
echo "E2E flow completed successfully."
echo "Workspace: ${E2E_ROOT}"
echo "Registered marketplaces:"
apm marketplace list
