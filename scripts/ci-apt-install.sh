#!/usr/bin/env bash
# Install apt packages on GitHub-hosted Ubuntu runners.
#
# Runner images route apt through azure.archive.ubuntu.com via
# /etc/apt/apt-mirrors.txt. When that mirror does not resolve, apt's mirror
# fallback still fails after minutes of retries and the step dies
# (actions/runner-images#14594, #15040). Probe the mirror first and, when it
# does not resolve or the install fails, point apt straight at the public
# Ubuntu archive and retry once.
#
# Usage: scripts/ci-apt-install.sh <package>...
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <package>..." >&2
  exit 2
fi

SOURCES=/etc/apt/sources.list.d/ubuntu.sources
MIRRORS=/etc/apt/apt-mirrors.txt
APT_OPTS=(-o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30)

use_public_archive() {
  local archive
  case "$(dpkg --print-architecture)" in
    amd64 | i386) archive=http://archive.ubuntu.com/ubuntu/ ;;
    *) archive=http://ports.ubuntu.com/ubuntu-ports/ ;;
  esac
  if grep -qs "mirror+file:${MIRRORS}" "$SOURCES"; then
    echo "[!] Pointing apt at ${archive} instead of the runner mirror list"
    sudo sed -i "s#mirror+file:${MIRRORS}#${archive}#" "$SOURCES"
  fi
}

apt_install() {
  sudo apt-get "${APT_OPTS[@]}" update && sudo apt-get "${APT_OPTS[@]}" install -y "$@"
}

if grep -qs "azure.archive.ubuntu.com" "$MIRRORS" \
  && ! getent hosts azure.archive.ubuntu.com > /dev/null; then
  echo "[!] azure.archive.ubuntu.com does not resolve"
  use_public_archive
fi

if apt_install "$@"; then
  exit 0
fi
echo "[!] apt install failed; retrying once against the public Ubuntu archive"
use_public_archive
apt_install "$@"
