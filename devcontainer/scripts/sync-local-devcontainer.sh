#!/usr/bin/env bash
# Refresh .devcontainer/ so VS Code Dev Containers can consume the local feature.
#
# Recent @devcontainers/cli versions (bundled in ms-vscode-remote.remote-containers
# >= 0.454.0) require local Feature paths to resolve INSIDE .devcontainer/, so we
# copy devcontainer/src/apm -> .devcontainer/apm-feature and write a
# devcontainer.json that references ./apm-feature.
#
# Run from the repo root: ./devcontainer/scripts/sync-local-devcontainer.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

mkdir -p .devcontainer
rm -rf .devcontainer/apm-feature
cp -R devcontainer/src/apm .devcontainer/apm-feature

cat > .devcontainer/devcontainer.json <<'EOF'
{
  "name": "APM Development",
  "image": "mcr.microsoft.com/devcontainers/python:3.12",
  "features": {
    "./apm-feature": {}
  }
}
EOF

echo "Synced .devcontainer/apm-feature from devcontainer/src/apm"
