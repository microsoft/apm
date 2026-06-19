#!/usr/bin/env bash
# scan-cross-corpus-drift.sh -- deterministic cross-corpus drift scan
#
# Used by docs-corpus-audit's CROSS-CORPUS POST-PASS step (5) and the
# ALIGNMENT LOOP step (6). Surfaces drift patterns that span many
# pages and are invisible to per-scope wave agents (which only see
# their own ~9 pages).
#
# Non-interactive. Emits structured matches on stdout, diagnostics
# on stderr. Update the pattern lists below after each major IA
# reshuffle or after every minor release.
#
# Usage:
#   ./scan-cross-corpus-drift.sh           # run all patterns
#   ./scan-cross-corpus-drift.sh --help    # list pattern groups
#   ./scan-cross-corpus-drift.sh ia-links  # one group only

set -euo pipefail

print_help() {
  cat <<'EOF'
scan-cross-corpus-drift.sh -- cross-corpus drift scan for docs-corpus-audit

Pattern groups:
  ia-links            Dead nav links from past IA reshuffles
                      (slug paths that no longer route).
  stale-deprecation   Deprecation banners pinned to a past version
                      target (e.g., "removed in v0.14" once we ship
                      v0.15+).
  absolute-base       Absolute /apm/... links (starlight base-prefix
                      hostility -- validator rejects these).
  ascii-leak          Non-ASCII characters in apm-usage/ corpus
                      (cp1252 hostility; ships to Windows runners).

Examples:
  ./scan-cross-corpus-drift.sh
  ./scan-cross-corpus-drift.sh ia-links
EOF
}

# IA-reshuffle dead-link slugs. Update this list whenever a major IA
# change retires or merges a slug. Each entry is the OLD slug; the
# scan flags any prose that still references it.
IA_DEAD_SLUGS=(
  "guides/agent-workflows"
  "introduction/"
  "guides/install-and-use"
  "guides/pack-distribute"
  "guides/ci-policy-setup"
  "guides/compilation"
  "guides/prompts"
  "guides/dependencies"
  "guides/drift-detection"
)

scan_ia_links() {
  echo ">>> IA dead-link scan" >&2
  local pattern
  pattern=$(IFS='|'; echo "${IA_DEAD_SLUGS[*]}")
  grep -rnE "($pattern)" docs/src/content/docs/ || true
}

scan_stale_deprecation() {
  echo ">>> Stale deprecation target scan" >&2
  grep -rnE "removal in v0\.|will be removed in v0\.|removed in v0\." \
    src/apm_cli/ docs/src/content/docs/ \
    packages/apm-guide/.apm/skills/apm-usage/ 2>/dev/null || true
}

scan_absolute_base() {
  echo ">>> Absolute /apm/ link scan (starlight base-prefix hostile)" >&2
  grep -rnE '\]\(/apm/' docs/src/content/docs/ || true
}

scan_ascii_leak() {
  echo ">>> Non-ASCII leak in apm-usage corpus (cp1252 hostile)" >&2
  if [ -d packages/apm-guide/.apm/skills/apm-usage/ ]; then
    LC_ALL=C grep -rn '[^[:print:][:space:]]' \
      packages/apm-guide/.apm/skills/apm-usage/ || true
  else
    echo "(skipped: apm-usage corpus not present)" >&2
  fi
}

run_all() {
  scan_ia_links
  scan_stale_deprecation
  scan_absolute_base
  scan_ascii_leak
}

case "${1:-}" in
  -h|--help) print_help ;;
  "") run_all ;;
  ia-links) scan_ia_links ;;
  stale-deprecation) scan_stale_deprecation ;;
  absolute-base) scan_absolute_base ;;
  ascii-leak) scan_ascii_leak ;;
  *)
    echo "unknown pattern group: $1" >&2
    print_help >&2
    exit 2
    ;;
esac
