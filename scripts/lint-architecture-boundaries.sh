#!/usr/bin/env bash
set -euo pipefail

if (( $# != 0 )); then
    printf '%s\n' 'lint-architecture-boundaries.sh accepts no arguments' >&2
    exit 2
fi

script_path=${BASH_SOURCE[0]}
case "$script_path" in
    /*) script_dir=${script_path%/*} ;;
    */*) script_dir=$PWD/${script_path%/*} ;;
    *) script_dir=$PWD ;;
esac
cd -- "$script_dir/.."
root=$PWD

exec python3 "$root/scripts/lint_architecture_boundaries.py" --root "$root"
