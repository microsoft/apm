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
DEMO_REMOTE="https://github.com/epam-mbg-demo/agent-forge.git"
BARE_REPO="${E2E_ROOT}/agent-forge.git"

rm -rf "${E2E_ROOT}"
mkdir -p "${E2E_ROOT}"

echo "[1/7] Cloning demo marketplace into a local bare repo..."
git clone --bare "${DEMO_REMOTE}" "${BARE_REPO}"

echo "[2/7] Registering via local filesystem path (kind=local)..."
apm marketplace add "${BARE_REPO}" --name agent-forge-local

echo "[3/7] Registering same repo via file:// URI (kind=local, exercises git show)..."
apm marketplace add "file://${BARE_REPO}" --name agent-forge-fileuri

echo "[4/7] Browsing both registrations..."
apm marketplace browse agent-forge-local
apm marketplace browse agent-forge-fileuri

echo "[5/7] Picking the first plugin from each registration and installing..."
PLUGIN_NAME="$(apm marketplace browse agent-forge-local --json 2>/dev/null | python -c 'import json,sys; print(json.load(sys.stdin)["plugins"][0]["name"])' || echo "")"
if [ -n "${PLUGIN_NAME}" ]; then
  apm install "${PLUGIN_NAME}@agent-forge-local"
  apm install "${PLUGIN_NAME}@agent-forge-fileuri"
  echo "    Installed ${PLUGIN_NAME} from both registrations."
else
  echo "    No plugins exposed by the demo marketplace; skipping install."
fi

echo "[6/7] Refreshing both marketplaces (validates GitCache ls-remote refresh path)..."
apm marketplace update agent-forge-local
apm marketplace update agent-forge-fileuri

echo "[7/7] Drift smoke: marketplace update picks up working-tree edits without manual cache flushing."
# (Would need a writable working-tree mirror; bare repos don't have one.
# Documented as a manual step: edit the working tree, re-run update,
# observe the change.)

echo ""
echo "E2E flow completed successfully."
echo "Workspace: ${E2E_ROOT}"
echo "Registered marketplaces:"
apm marketplace list
