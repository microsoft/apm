#!/usr/bin/env bash
# Install apt packages on GitHub-hosted Ubuntu runners.
#
# Runner images route apt through azure.archive.ubuntu.com, falling back to
# other *.ubuntu.com hosts. When ubuntu.com DNS is down (for example the
# Canonical outage of 2026-11-11) every one of those hosts fails to resolve
# and the install dies after minutes of retries. On amd64, retry once against
# a mirror outside the ubuntu.com zone. apt still verifies the archive with
# the Ubuntu keyring (Signed-By in ubuntu.sources), so the mirror is only a
# transport.
#
# Usage: scripts/ci-apt-install.sh <package>...
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <package>..." >&2
  exit 2
fi

SOURCES=/etc/apt/sources.list.d/ubuntu.sources
FALLBACK_MIRROR=http://mirrors.edge.kernel.org/ubuntu/
APT_OPTS=(-o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30)

apt_install() {
  sudo apt-get "${APT_OPTS[@]}" update && sudo apt-get "${APT_OPTS[@]}" install -y "$@"
}

can_fall_back() {
  [ "$(dpkg --print-architecture)" = amd64 ] && grep -qs '^URIs:' "$SOURCES"
}

use_fallback_mirror() {
  echo "[!] Pointing apt at ${FALLBACK_MIRROR}"
  sudo sed -i "s#^URIs:.*#URIs: ${FALLBACK_MIRROR}#" "$SOURCES"
}

if can_fall_back && ! getent hosts archive.ubuntu.com > /dev/null; then
  echo "[!] archive.ubuntu.com does not resolve"
  use_fallback_mirror
  apt_install "$@"
  exit 0
fi

if apt_install "$@"; then
  exit 0
fi
if ! can_fall_back; then
  exit 1
fi
echo "[!] apt install failed; retrying once against a mirror outside ubuntu.com"
use_fallback_mirror
apt_install "$@"
