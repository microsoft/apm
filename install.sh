#!/bin/bash
set -e

# APM CLI Installer Script
# Usage: curl -sSL https://aka.ms/apm-unix | sh
# Specific version:     curl -sSL https://aka.ms/apm-unix | sh -s -- @v1.2.3   (or VERSION=v1.2.3)
# Prefix install:       curl -sSL https://aka.ms/apm-unix | sh -s -- --prefix "$HOME/.local"
# Custom install dir:   curl -sSL https://aka.ms/apm-unix | APM_INSTALL_DIR=$HOME/tools/bin sh
# Custom repository:    APM_REPO=ghe-org/apm sh install.sh
# GitHub Enterprise:    GITHUB_URL=https://gh.corp.com sh install.sh
# Enterprise mirror:    APM_RELEASE_BASE_URL=https://mirror.example/apm VERSION=v1.2.3 sh install.sh
# PyPI mirror fallback: APM_PYPI_INDEX_URL=https://mirror.example/pypi/simple sh install.sh
# Fail closed:          APM_NO_DIRECT_FALLBACK=1 sh install.sh
# For private repositories, use with authentication:
#   curl -sSL -H "Authorization: token $GITHUB_APM_PAT" \
#     https://raw.githubusercontent.com/microsoft/apm/main/install.sh | \
#     GITHUB_APM_PAT=$GITHUB_APM_PAT sh

# Colors for output
RED=$(printf '\033[0;31m')
GREEN=$(printf '\033[0;32m')
BLUE=$(printf '\033[0;34m')
YELLOW=$(printf '\033[1;33m')
NC=$(printf '\033[0m') # No Color

apm_echo() {
    printf '%s\n' "$*"
}

# Configuration (all overridable via environment variables)
APM_REPO="${APM_REPO:-microsoft/apm}"
_APM_INSTALL_DIR_SET="${APM_INSTALL_DIR:+1}"
_APM_LIB_DIR_SET="${APM_LIB_DIR:+1}"
_APM_PREFIX_SET=""
APM_INSTALL_PREFIX=""
APM_INSTALL_DIR="${APM_INSTALL_DIR:-$HOME/.local/bin}"
APM_LIB_DIR="${APM_LIB_DIR:-$(dirname "$APM_INSTALL_DIR")/lib/apm}"
BINARY_NAME="apm"
GITHUB_URL="${GITHUB_URL:-https://github.com}"
APM_RELEASE_BASE_URL="${APM_RELEASE_BASE_URL:-}"
APM_RELEASE_METADATA_URL="${APM_RELEASE_METADATA_URL:-}"
APM_INSTALLER_BASE_URL="${APM_INSTALLER_BASE_URL:-}"
APM_PYPI_INDEX_URL="${APM_PYPI_INDEX_URL:-}"
APM_NO_DIRECT_FALLBACK="${APM_NO_DIRECT_FALLBACK:-}"
APM_NO_MODIFY_PATH="${APM_NO_MODIFY_PATH:-}"
_APM_MODIFY_PATH_REQUEST="inherit"
_APM_PREVIOUS_SHELL_RECEIPT_VALID=""
_APM_PREVIOUS_SHELL_SELECTED_BIN=""
_APM_PREVIOUS_SHELL_HOOK_DIR=""
_APM_PREVIOUS_SHELL_HOOK_KIND=""
_APM_PREVIOUS_SHELL_HOOK_DIGEST=""
_APM_PREVIOUS_SHELL_MODIFY_PATH=""
_APM_NEW_SHELL_HOOK_DIR=""
_APM_NEW_SHELL_HOOK_KIND="none"
_APM_NEW_SHELL_HOOK_DIGEST="none"
_APM_NATIVE_RECEIPT_WRITTEN=""

# INSTALL_OWNERSHIP_BEGIN
# Resolve symlink targets portably, including on macOS without readlink -f.
apm_real_path() (
    _path="$1"
    case "$_path" in /*) ;; *) _path="$PWD/$_path" ;; esac
    _links=0
    while [ -L "$_path" ]; do
        _links=$((_links + 1))
        [ "$_links" -le 40 ] || return 1
        _link="$(readlink "$_path")" || return 1
        case "$_link" in
            /*) _path="$_link" ;;
            *) _path="$(dirname "$_path")/$_link" ;;
        esac
    done
    _parent="$(dirname "$_path")"
    _leaf="${_path##*/}"
    while [ ! -d "$_parent" ]; do
        [ "$_parent" != "/" ] || return 1
        _leaf="${_parent##*/}/$_leaf"
        _parent="${_parent%/*}"
        [ -n "$_parent" ] || _parent="/"
    done
    _parent="$(cd -P "$_parent" && pwd)" || return 1
    printf '%s/%s\n' "${_parent%/}" "$_leaf"
)

apm_install_error() {
    printf '%s\n' "[x] $*" >&2
    exit 1
}

apm_print_usage() {
    printf '%s\n' \
        "Usage: sh install.sh [--prefix PATH] [@vVERSION]" \
        "" \
        "Options:" \
        "  --prefix PATH     Install launcher at PATH/bin and bundle at PATH/lib/apm." \
        "  --prefix=PATH     Same as --prefix PATH." \
        "  -h, --help        Show this help." \
        "" \
        "Environment:" \
        "  APM_INSTALL_DIR   Explicit launcher directory when --prefix is absent." \
        "  APM_LIB_DIR       Explicit Unix bundle directory when --prefix is absent." \
        "  APM_NO_MODIFY_PATH  Set 1 to skip native shell PATH setup, 0 to re-enable." \
        "  VERSION           Release tag to install, equivalent to @vVERSION." \
        "" \
        "The installer never runs sudo. Run the shell with the privileges needed for" \
        "the selected destination."
}

apm_set_cli_version() {
    [ -n "$1" ] || apm_install_error "Version argument is empty. Use @v1.2.3 or VERSION=v1.2.3."
    if [ -n "$VERSION" ] && [ "$VERSION" != "$1" ]; then
        apm_install_error "VERSION is already set to $VERSION. Remove the positional version argument or make it match."
    fi
    VERSION="$1"
}

apm_parse_installer_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                apm_print_usage
                exit 0
                ;;
            --prefix=*)
                [ -z "$_APM_PREFIX_SET" ] ||
                    apm_install_error "--prefix may be provided only once."
                APM_INSTALL_PREFIX="${1#--prefix=}"
                [ -n "$APM_INSTALL_PREFIX" ] ||
                    apm_install_error "Missing value for --prefix. Use --prefix PATH or --prefix=PATH."
                _APM_PREFIX_SET=1
                ;;
            --prefix)
                [ -z "$_APM_PREFIX_SET" ] ||
                    apm_install_error "--prefix may be provided only once."
                shift
                [ "$#" -gt 0 ] ||
                    apm_install_error "Missing value for --prefix. Use --prefix PATH or --prefix=PATH."
                case "$1" in
                    -*) apm_install_error "Missing value for --prefix. Use --prefix PATH or --prefix=PATH." ;;
                esac
                APM_INSTALL_PREFIX="$1"
                [ -n "$APM_INSTALL_PREFIX" ] ||
                    apm_install_error "Missing value for --prefix. Use --prefix PATH or --prefix=PATH."
                _APM_PREFIX_SET=1
                ;;
            --*)
                apm_install_error "Unknown option: $1. Run install.sh --help for usage."
                ;;
            @v[0-9]*|@[0-9]*)
                apm_set_cli_version "${1#@}"
                ;;
            @*)
                apm_install_error "Invalid version argument: $1. Use @v1.2.3."
                ;;
            v[0-9]*|[0-9]*)
                apm_set_cli_version "$1"
                ;;
            *)
                apm_install_error "Unknown argument: $1. Use @v1.2.3 for a version or --prefix PATH for destinations."
                ;;
        esac
        shift
    done
}

apm_trim_trailing_slashes() (
    _path="$1"
    while [ "$_path" != "/" ] && [ "${_path%/}" != "$_path" ]; do
        _path="${_path%/}"
    done
    printf '%s\n' "$_path"
)

# Produce one POSIX shell word without adding a Python dependency.
apm_shell_quote() (
    _rest="$1"
    printf "'"
    while :; do
        case "$_rest" in
            *"'"*)
                printf "%s'\\\\''" "${_rest%%"'"*}"
                _rest="${_rest#*"'"}"
                ;;
            *)
                printf "%s'" "$_rest"
                return
                ;;
        esac
    done
)

apm_print_path_guidance() {
    _apm_run_hint="apm"
    _apm_guidance_shell="$(apm_detect_current_shell)"
    case "$1" in
        *:*|*[[:cntrl:]]*)
            if [ "$_apm_guidance_shell" = "fish" ]; then
                _apm_run_hint="$(apm_fish_quote "$1/apm")"
            else
                _apm_run_hint="$(apm_shell_quote "$1/apm")"
            fi
            echo "Run APM using its absolute path:"
            printf '  %s --version\n' "$_apm_run_hint"
            echo "For PATH discovery, reinstall through its owner into a directory without ':' or control characters."
            ;;
        *)
            echo "For this shell, run the following for the current terminal:"
            if [ "$_apm_guidance_shell" = "fish" ]; then
                printf '  set -gx PATH %s $PATH\n' "$(apm_fish_quote "$1")"
                echo "To persist it manually, add that line to your Fish config."
            else
                printf '  export PATH=%s:"$PATH"\n' "$(apm_shell_quote "$1")"
                echo "To persist it manually, add that line to your shell profile."
            fi
            ;;
    esac
    if [ "${_APM_PROFILE_CHANGE_STATUS:-}" = "partial" ]; then
        echo "Some shell profile changes may remain. Inspect blocks marked 'apm shell setup' before retrying."
    else
        echo "No shell profiles were changed."
    fi
}

apm_parse_modify_path_env() {
    [ -n "$APM_NO_MODIFY_PATH" ] || {
        _APM_MODIFY_PATH_REQUEST="inherit"
        return 0
    }
    _apm_value="$(printf '%s' "$APM_NO_MODIFY_PATH" | tr '[:upper:]' '[:lower:]')"
    case "$_apm_value" in
        1|true|yes|on)
            _APM_MODIFY_PATH_REQUEST="disable"
            ;;
        0|false|no|off)
            _APM_MODIFY_PATH_REQUEST="enable"
            ;;
        *)
            apm_install_error "Invalid APM_NO_MODIFY_PATH value: $APM_NO_MODIFY_PATH. Use 1 to opt out, 0 to re-enable, or leave it unset."
            ;;
    esac
}

apm_path_is_safe_for_path() {
    case "$1" in
        *:*|*[[:cntrl:]]*) return 1 ;;
        *) return 0 ;;
    esac
}

apm_fish_quote() (
    _rest="$1"
    printf "'"
    while [ -n "$_rest" ]; do
        _char="${_rest%"${_rest#?}"}"
        _rest="${_rest#?}"
        case "$_char" in
            "'") printf "%s" "\\'" ;;
            "\\") printf "%s" "\\\\" ;;
            *) printf "%s" "$_char" ;;
        esac
    done
    printf "'"
)

apm_bool_env_active() {
    _apm_flag_value="$1"
    [ -n "$_apm_flag_value" ] || return 1
    _apm_flag_value="$(printf '%s' "$_apm_flag_value" | tr '[:upper:]' '[:lower:]')"
    case "$_apm_flag_value" in
        0|false|no|off) return 1 ;;
        *) return 0 ;;
    esac
}

apm_has_controlling_tty() {
    ( : < /dev/tty ) >/dev/null 2>&1
}

apm_file_digest() {
    [ -f "$1" ] || return 1
    set -- $(cksum < "$1") || return 1
    printf '%s:%s' "$1" "$2"
}

apm_reset_previous_shell_receipt() {
    _APM_PREVIOUS_SHELL_RECEIPT_VALID=""
    _APM_PREVIOUS_SHELL_RECEIPT_PRESENT=""
    _APM_PREVIOUS_SHELL_SELECTED_BIN=""
    _APM_PREVIOUS_SHELL_HOOK_DIR=""
    _APM_PREVIOUS_SHELL_HOOK_KIND=""
    _APM_PREVIOUS_SHELL_HOOK_DIGEST=""
    _APM_PREVIOUS_SHELL_MODIFY_PATH=""
}

apm_read_shell_receipt() {
    apm_reset_previous_shell_receipt
    _apm_receipt="$1"
    [ -e "$_apm_receipt" ] || [ -L "$_apm_receipt" ] || return 0
    _APM_PREVIOUS_SHELL_RECEIPT_PRESENT=1
    [ -f "$_apm_receipt" ] || return 0
    [ ! -L "$_apm_receipt" ] || return 0
    [ ! -x "$_apm_receipt" ] || return 0
    _apm_seen_version=""
    _apm_seen_owner=""
    _apm_seen_selected_bin=""
    _apm_seen_hook_dir=""
    _apm_seen_hook_kind=""
    _apm_seen_hook_digest=""
    _apm_seen_modify_path=""
    _apm_version=""
    _apm_owner=""
    _apm_selected_bin=""
    _apm_hook_dir=""
    _apm_hook_kind=""
    _apm_hook_digest=""
    _apm_modify_path=""
    while IFS= read -r _apm_line || [ -n "$_apm_line" ]; do
        case "$_apm_line" in
            "") return 0 ;;
            version=*)
                [ -z "$_apm_seen_version" ] || return 0
                _apm_seen_version=1
                _apm_version="${_apm_line#version=}"
                ;;
            owner=*)
                [ -z "$_apm_seen_owner" ] || return 0
                _apm_seen_owner=1
                _apm_owner="${_apm_line#owner=}"
                ;;
            selected_bin=*)
                [ -z "$_apm_seen_selected_bin" ] || return 0
                _apm_seen_selected_bin=1
                _apm_selected_bin="${_apm_line#selected_bin=}"
                ;;
            hook_dir=*)
                [ -z "$_apm_seen_hook_dir" ] || return 0
                _apm_seen_hook_dir=1
                _apm_hook_dir="${_apm_line#hook_dir=}"
                ;;
            hook_kind=*)
                [ -z "$_apm_seen_hook_kind" ] || return 0
                _apm_seen_hook_kind=1
                _apm_hook_kind="${_apm_line#hook_kind=}"
                ;;
            hook_digest=*)
                [ -z "$_apm_seen_hook_digest" ] || return 0
                _apm_seen_hook_digest=1
                _apm_hook_digest="${_apm_line#hook_digest=}"
                ;;
            modify_path=*)
                [ -z "$_apm_seen_modify_path" ] || return 0
                _apm_seen_modify_path=1
                _apm_modify_path="${_apm_line#modify_path=}"
                ;;
            *) return 0 ;;
        esac
    done < "$_apm_receipt"
    [ "$_apm_version" = "1" ] || return 0
    [ "$_apm_owner" = "native" ] || return 0
    [ "$_apm_selected_bin" = "$APM_INSTALL_DIR" ] || return 0
    case "$_apm_hook_kind" in posix|fish|none) ;; *) return 0 ;; esac
    case "$_apm_modify_path" in managed|disabled) ;; *) return 0 ;; esac
    [ -n "$_apm_seen_version$_apm_seen_owner$_apm_seen_selected_bin$_apm_seen_hook_dir$_apm_seen_hook_kind$_apm_seen_hook_digest$_apm_seen_modify_path" ] || return 0
    [ -n "$_apm_seen_version" ] && [ -n "$_apm_seen_owner" ] &&
        [ -n "$_apm_seen_selected_bin" ] && [ -n "$_apm_seen_hook_dir" ] &&
        [ -n "$_apm_seen_hook_kind" ] && [ -n "$_apm_seen_hook_digest" ] &&
        [ -n "$_apm_seen_modify_path" ] || return 0
    if [ "$_apm_hook_kind" = "none" ]; then
        [ "$_apm_hook_dir" = "none" ] && [ "$_apm_hook_digest" = "none" ] || return 0
    else
        case "$_apm_hook_dir" in /*) ;; *) return 0 ;; esac
        [ "$_apm_hook_digest" != "none" ] || return 0
    fi
    _APM_PREVIOUS_SHELL_SELECTED_BIN="$_apm_selected_bin"
    _APM_PREVIOUS_SHELL_HOOK_DIR="$_apm_hook_dir"
    _APM_PREVIOUS_SHELL_HOOK_KIND="$_apm_hook_kind"
    _APM_PREVIOUS_SHELL_HOOK_DIGEST="$_apm_hook_digest"
    _APM_PREVIOUS_SHELL_MODIFY_PATH="$_apm_modify_path"
    _APM_PREVIOUS_SHELL_RECEIPT_VALID=1
}

apm_mktemp_in_dir() {
    _apm_dir="$1"
    _apm_name="$2"
    mktemp "$_apm_dir/.$_apm_name.XXXXXX"
}

apm_write_shell_receipt() {
    _apm_modify_path="$1"
    _apm_hook_dir="$2"
    _apm_hook_kind="$3"
    _apm_hook_digest="$4"
    apm_path_is_safe_for_path "$APM_INSTALL_DIR" || return 0
    _apm_receipt="$APM_LIB_DIR/.apm-shell-setup"
    _apm_tmp="$(apm_mktemp_in_dir "$APM_LIB_DIR" "apm-shell-setup")" || return 1
    {
        printf 'version=1\n'
        printf 'owner=native\n'
        printf 'selected_bin=%s\n' "$APM_INSTALL_DIR"
        printf 'hook_dir=%s\n' "$_apm_hook_dir"
        printf 'hook_kind=%s\n' "$_apm_hook_kind"
        printf 'hook_digest=%s\n' "$_apm_hook_digest"
        printf 'modify_path=%s\n' "$_apm_modify_path"
    } > "$_apm_tmp" || { rm -f "$_apm_tmp"; return 1; }
    chmod 600 "$_apm_tmp" 2>/dev/null || true
    mv "$_apm_tmp" "$_apm_receipt" || return 1
    _APM_NATIVE_RECEIPT_WRITTEN=1
}

apm_preserve_previous_shell_receipt_to() {
    _apm_target_dir="$1"
    _apm_previous_receipt="$APM_LIB_DIR/.apm-shell-setup"
    [ -f "$_apm_previous_receipt" ] || return 0
    [ ! -L "$_apm_previous_receipt" ] || return 0
    cp -p "$_apm_previous_receipt" "$_apm_target_dir/.apm-shell-setup" || return 1
}

apm_detect_current_shell() {
    _apm_comm=""
    if command -v ps >/dev/null 2>&1; then
        _apm_comm="$(ps -p "$PPID" -o comm= 2>/dev/null | sed 's/^ *//;s/ *$//' || true)"
    fi
    _apm_base="${_apm_comm##*/}"
    _apm_base="${_apm_base#-}"
    case "$_apm_base" in
        bash|zsh|fish)
            printf '%s\n' "$_apm_base"
            return 0
            ;;
    esac
    _apm_base="${SHELL##*/}"
    _apm_base="${_apm_base#-}"
    case "$_apm_base" in
        bash|zsh|fish)
            printf '%s\n' "$_apm_base"
            return 0
            ;;
    esac
    printf 'unknown\n'
}

apm_detect_profile_shell() {
    _apm_login_base="${SHELL##*/}"
    _apm_login_base="${_apm_login_base#-}"
    case "$_apm_login_base" in
        bash|zsh|fish)
            printf '%s\n' "$_apm_login_base"
            return 0
            ;;
    esac
    apm_detect_current_shell
}

apm_is_desktop_shell_setup_eligible() {
    [ -n "${APM_SELF_UPDATE_SOURCE:-}" ] && {
        _APM_SHELL_SETUP_SKIP_REASON="self-update never edits shell profiles"
        return 1
    }
    [ "$(id -u)" -eq 0 ] && {
        _APM_SHELL_SETUP_SKIP_REASON="administrator installs never edit shell profiles"
        return 1
    }
    if apm_bool_env_active "${CI:-}" || apm_bool_env_active "${GITHUB_ACTIONS:-}" ||
        apm_bool_env_active "${TF_BUILD:-}" || apm_bool_env_active "${BUILD_BUILDID:-}" ||
        apm_bool_env_active "${BUILDKITE:-}" || apm_bool_env_active "${GITLAB_CI:-}"; then
        _APM_SHELL_SETUP_SKIP_REASON="CI environment detected"
        return 1
    fi
    if ! apm_has_controlling_tty; then
        _APM_SHELL_SETUP_SKIP_REASON="no interactive terminal detected"
        return 1
    fi
    case "$HOME" in
        /*) ;;
        *) _APM_SHELL_SETUP_SKIP_REASON="HOME is not an absolute path"; return 1 ;;
    esac
    [ -d "$HOME" ] && [ -w "$HOME" ] && [ -x "$HOME" ] || {
        _APM_SHELL_SETUP_SKIP_REASON="HOME is not writable"
        return 1
    }
    apm_path_is_safe_for_path "$APM_INSTALL_DIR" || {
        _APM_SHELL_SETUP_SKIP_REASON="the install bin path cannot be represented safely in PATH"
        return 1
    }
    _APM_DETECTED_SHELL="$(apm_detect_profile_shell)"
    case "$_APM_DETECTED_SHELL" in
        bash|zsh|fish) return 0 ;;
        *) _APM_SHELL_SETUP_SKIP_REASON="unsupported or unknown shell"; return 1 ;;
    esac
}

apm_posix_hook_content() {
    _apm_bin_word="$(apm_shell_quote "$1")"
    printf '%s\n' \
        "# APM shell setup generated by install.sh; do not edit inside this file." \
        "# selected_bin=$1" \
        "_apm_bin=$_apm_bin_word" \
        '_apm_old_path="${PATH-}"' \
        '_apm_had_path="${PATH+x}"' \
        'PATH="$_apm_bin"' \
        'if [ "$_apm_had_path" = "x" ]; then' \
        '  _apm_remaining="$_apm_old_path"' \
        '  while :; do' \
        '    case "$_apm_remaining" in' \
        '      *:*) _apm_entry="${_apm_remaining%%:*}"; _apm_remaining="${_apm_remaining#*:}"; _apm_more=1 ;;' \
        '      *) _apm_entry="$_apm_remaining"; _apm_more=0 ;;' \
        '    esac' \
        '    if [ "$_apm_entry" != "$_apm_bin" ]; then' \
        '      PATH="$PATH:$_apm_entry"' \
        '    fi' \
        '    [ "$_apm_more" = 1 ] || break' \
        'done' \
        'fi' \
        'export PATH' \
        'unset _apm_bin _apm_old_path _apm_had_path _apm_remaining _apm_entry _apm_more'
}

apm_fish_hook_content() {
    _apm_bin_word="$(apm_fish_quote "$1")"
    printf '%s\n' \
        "# APM shell setup generated by install.sh; do not edit inside this file." \
        "# selected_bin=$1" \
        "set -l apm_bin $_apm_bin_word" \
        "set -l apm_path_entries \$apm_bin" \
        "for apm_entry in \$PATH" \
        "    if test \"\$apm_entry\" != \"\$apm_bin\"" \
        "        set apm_path_entries \$apm_path_entries \$apm_entry" \
        "    end" \
        "end" \
        "set -gx PATH \$apm_path_entries"
}

apm_expected_profile_block() {
    _apm_hook="$1"
    _apm_shell="$2"
    printf '%s\n' "# >>> apm shell setup >>>"
    printf '%s\n' "# Generated by install.sh. Remove this block to stop loading APM."
    if [ "$_apm_shell" = "fish" ]; then
        printf 'test -f %s; and source %s\n' "$(apm_fish_quote "$_apm_hook")" "$(apm_fish_quote "$_apm_hook")"
    else
        printf '[ -f %s ] && . %s\n' "$(apm_shell_quote "$_apm_hook")" "$(apm_shell_quote "$_apm_hook")"
    fi
    printf '%s\n' "# <<< apm shell setup <<<"
}

apm_existing_path_owned_safe() {
    _apm_path="$1"
    [ ! -L "$_apm_path" ] || return 1
    [ -O "$_apm_path" ] || return 1
    if [ -d "$_apm_path" ]; then
        [ -w "$_apm_path" ] && [ -x "$_apm_path" ] || return 1
    else
        [ -f "$_apm_path" ] && [ -r "$_apm_path" ] && [ -w "$_apm_path" ] || return 1
    fi
}

apm_prepare_owned_directory() {
    _apm_dir="$1"
    _apm_base="$HOME"
    case "$_apm_dir" in "$HOME"|"$HOME"/*) _apm_base="$HOME" ;; *) _apm_base="" ;; esac
    if [ -z "$_apm_base" ] && [ -n "${ZDOTDIR:-}" ]; then
        case "$_apm_dir" in "$ZDOTDIR"|"$ZDOTDIR"/*) _apm_base="$ZDOTDIR" ;; esac
    fi
    if [ -z "$_apm_base" ] && [ -n "${XDG_CONFIG_HOME:-}" ]; then
        case "$_apm_dir" in "$XDG_CONFIG_HOME"|"$XDG_CONFIG_HOME"/*) _apm_base="$XDG_CONFIG_HOME" ;; esac
    fi
    [ -n "$_apm_base" ] || return 1
    _apm_current="$_apm_base"
    if [ -e "$_apm_current" ] || [ -L "$_apm_current" ]; then
        apm_existing_path_owned_safe "$_apm_current" || return 1
    else
        _apm_base_parent="${_apm_current%/*}"
        [ -n "$_apm_base_parent" ] && [ "$_apm_base_parent" != "$_apm_current" ] || return 1
        apm_existing_path_owned_safe "$_apm_base_parent" || return 1
        mkdir "$_apm_current" || return 1
        chmod 700 "$_apm_current" 2>/dev/null || true
    fi
    _apm_rest="${_apm_dir#"$_apm_base"}"
    _apm_rest="${_apm_rest#/}"
    while [ -n "$_apm_rest" ]; do
        _apm_part="${_apm_rest%%/*}"
        if [ "$_apm_part" = "$_apm_rest" ]; then
            _apm_rest=""
        else
            _apm_rest="${_apm_rest#*/}"
        fi
        [ -n "$_apm_part" ] || continue
        _apm_current="$_apm_current/$_apm_part"
        if [ -e "$_apm_current" ] || [ -L "$_apm_current" ]; then
            apm_existing_path_owned_safe "$_apm_current" || return 1
        else
            mkdir "$_apm_current" || return 1
            chmod 700 "$_apm_current" 2>/dev/null || true
        fi
    done
}

apm_profile_has_unreachable_exit() {
    _apm_file="$1"
    [ -f "$_apm_file" ] || return 1
    grep -E '^[[:space:]]*(exit|return)([[:space:]]+[0-9]+)?[[:space:]]*(#.*)?$' "$_apm_file" >/dev/null 2>&1
}

apm_write_generated_hook() {
    _apm_hook="$1"
    _apm_kind="$2"
    _apm_parent="${_apm_hook%/*}"
    [ "$_apm_parent" != "$_apm_hook" ] || return 1
    apm_prepare_owned_directory "$_apm_parent" || return 1
    _apm_tmp="$(apm_mktemp_in_dir "$_apm_parent" "${_apm_hook##*/}")" || return 1
    if [ "$_apm_kind" = "fish" ]; then
        apm_fish_hook_content "$APM_INSTALL_DIR" > "$_apm_tmp" || { rm -f "$_apm_tmp"; return 1; }
    else
        apm_posix_hook_content "$APM_INSTALL_DIR" > "$_apm_tmp" || { rm -f "$_apm_tmp"; return 1; }
    fi
    chmod 600 "$_apm_tmp" 2>/dev/null || true
    if [ -e "$_apm_hook" ] || [ -L "$_apm_hook" ]; then
        apm_existing_path_owned_safe "$_apm_hook" || { rm -f "$_apm_tmp"; return 1; }
        _apm_current_digest="$(apm_file_digest "$_apm_hook" || true)"
        _apm_generated_digest="$(apm_file_digest "$_apm_tmp" || true)"
        if [ -z "$_APM_PREVIOUS_SHELL_RECEIPT_VALID" ] ||
            {
                { [ "$_APM_PREVIOUS_SHELL_HOOK_DIR/${_apm_hook##*/}" != "$_apm_hook" ] ||
                    [ "$_APM_PREVIOUS_SHELL_HOOK_KIND" != "$_apm_kind" ] ||
                    [ "$_APM_PREVIOUS_SHELL_HOOK_DIGEST" != "$_apm_current_digest" ]; } &&
                [ "$_apm_current_digest" != "$_apm_generated_digest" ]
            }; then
            rm -f "$_apm_tmp"
            return 1
        fi
    fi
    mv "$_apm_tmp" "$_apm_hook" || return 1
    _APM_NEW_SHELL_HOOK_DIGEST="$(apm_file_digest "$_apm_hook" || printf 'none')"
    return 0
}

apm_profile_contains_exact_block() {
    _apm_file="$1"
    _apm_block="$2"
    _apm_content="$(cat "$_apm_file" 2>/dev/null || true)"
    case "$_apm_content" in
        *"$_apm_block"*) return 0 ;;
        *) return 1 ;;
    esac
}

apm_update_profile_file() {
    _apm_file="$1"
    _apm_block="$2"
    _apm_parent="${_apm_file%/*}"
    [ "$_apm_parent" != "$_apm_file" ] || return 1
    apm_prepare_owned_directory "$_apm_parent" || return 1
    [ ! -L "$_apm_file" ] || return 1
    if [ -e "$_apm_file" ]; then
        apm_existing_path_owned_safe "$_apm_file" || return 1
        apm_profile_has_unreachable_exit "$_apm_file" && return 1
        _apm_start_count="$(grep -F -c "# >>> apm shell setup >>>" "$_apm_file" 2>/dev/null || true)"
        _apm_end_count="$(grep -F -c "# <<< apm shell setup <<<" "$_apm_file" 2>/dev/null || true)"
        case "$_apm_start_count:$_apm_end_count" in
            0:0) ;;
            1:1)
                apm_profile_contains_exact_block "$_apm_file" "$_apm_block" || return 1
                return 0
                ;;
            *) return 1 ;;
        esac
        _apm_before_digest="$(apm_file_digest "$_apm_file")" || return 1
        _apm_tmp="$(apm_mktemp_in_dir "$_apm_parent" "${_apm_file##*/}.apm")" || return 1
        cp -p "$_apm_file" "$_apm_tmp" || return 1
        [ -s "$_apm_tmp" ] && printf '\n' >> "$_apm_tmp"
        printf '%s\n' "$_apm_block" >> "$_apm_tmp" || { rm -f "$_apm_tmp"; return 1; }
        [ "$(apm_file_digest "$_apm_file" || true)" = "$_apm_before_digest" ] ||
            { rm -f "$_apm_tmp"; return 1; }
        mv "$_apm_tmp" "$_apm_file" || return 1
    else
        _apm_tmp="$(apm_mktemp_in_dir "$_apm_parent" "${_apm_file##*/}.apm")" || return 1
        printf '%s\n' "$_apm_block" > "$_apm_tmp" || return 1
        chmod 600 "$_apm_tmp" 2>/dev/null || true
        [ ! -e "$_apm_file" ] || { rm -f "$_apm_tmp"; return 1; }
        mv "$_apm_tmp" "$_apm_file" || return 1
    fi
}

apm_remove_profile_block() {
    _apm_file="$1"
    _apm_block="$2"
    [ -f "$_apm_file" ] && [ ! -L "$_apm_file" ] || return 1
    apm_profile_contains_exact_block "$_apm_file" "$_apm_block" || return 0
    _apm_parent="${_apm_file%/*}"
    _apm_tmp="$(apm_mktemp_in_dir "$_apm_parent" "${_apm_file##*/}.apm-remove")" || return 1
    _apm_content="$(cat "$_apm_file")" || return 1
    _apm_prefix="${_apm_content%%"$_apm_block"*}"
    _apm_suffix="${_apm_content#*"$_apm_block"}"
    printf '%s%s' "$_apm_prefix" "$_apm_suffix" > "$_apm_tmp" || return 1
    mv "$_apm_tmp" "$_apm_file" || return 1
}

apm_restore_profile_after_partial_write() {
    _apm_file="$1"
    _apm_backup="$2"
    _apm_existed="$3"
    _apm_expected_digest="$4"
    [ -n "$_apm_expected_digest" ] || return 1
    [ -f "$_apm_file" ] && [ ! -L "$_apm_file" ] || return 1
    [ "$(apm_file_digest "$_apm_file" || true)" = "$_apm_expected_digest" ] || return 1
    if [ "$_apm_existed" = "1" ]; then
        [ -n "$_apm_backup" ] && [ -f "$_apm_backup" ] || return 1
        mv "$_apm_backup" "$_apm_file" || return 1
    else
        rm -f "$_apm_file" || return 1
    fi
}

apm_bash_login_profile() {
    for _apm_candidate in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
        if [ -e "$_apm_candidate" ]; then
            [ -r "$_apm_candidate" ] && {
                printf '%s\n' "$_apm_candidate"
                return 0
            }
            return 1
        fi
    done
    printf '%s\n' "$HOME/.bash_profile"
}

apm_zsh_profile() {
    if [ -n "${ZDOTDIR:-}" ]; then
        case "$ZDOTDIR" in
            /*) printf '%s\n' "$ZDOTDIR/.zshrc"; return 0 ;;
            *) return 1 ;;
        esac
    fi
    if [ -f "$HOME/.zshenv" ] &&
        grep -E '^[[:space:]]*(export[[:space:]]+)?ZDOTDIR=' "$HOME/.zshenv" >/dev/null 2>&1; then
        return 1
    fi
    printf '%s\n' "$HOME/.zshrc"
}

apm_fish_profile() {
    if [ -n "${XDG_CONFIG_HOME:-}" ]; then
        case "$XDG_CONFIG_HOME" in
            /*) printf '%s\n' "$XDG_CONFIG_HOME/fish/conf.d/apm.fish"; return 0 ;;
            *) return 1 ;;
        esac
    else
        printf '%s\n' "$HOME/.config/fish/conf.d/apm.fish"
    fi
}

apm_record_shell_setup_not_configured() {
    if [ "$_APM_PREVIOUS_SHELL_RECEIPT_VALID" = "1" ]; then
        apm_write_shell_receipt "$_APM_PREVIOUS_SHELL_MODIFY_PATH" "$_APM_PREVIOUS_SHELL_HOOK_DIR" "$_APM_PREVIOUS_SHELL_HOOK_KIND" "$_APM_PREVIOUS_SHELL_HOOK_DIGEST" ||
            apm_echo "${YELLOW}[!] APM installed, but shell setup receipt could not be saved.${NC}"
    elif [ -z "$_APM_PREVIOUS_SHELL_RECEIPT_PRESENT" ]; then
        apm_write_shell_receipt "managed" "none" "none" "none" ||
            apm_echo "${YELLOW}[!] APM installed, but shell setup receipt could not be saved.${NC}"
    fi
}

apm_configure_native_shell_path() {
    _apm_modify_path="managed"
    if [ -n "${APM_SELF_UPDATE_SOURCE:-}" ]; then
        echo "Self-update leaves existing shell PATH setup unchanged."
        return 0
    fi
    if [ "$_APM_MODIFY_PATH_REQUEST" = "disable" ]; then
        _apm_previous_hook_dir="none"
        _apm_previous_hook_kind="none"
        _apm_previous_hook_digest="none"
        if [ "$_APM_PREVIOUS_SHELL_RECEIPT_VALID" = "1" ]; then
            _apm_previous_hook_dir="$_APM_PREVIOUS_SHELL_HOOK_DIR"
            _apm_previous_hook_kind="$_APM_PREVIOUS_SHELL_HOOK_KIND"
            _apm_previous_hook_digest="$_APM_PREVIOUS_SHELL_HOOK_DIGEST"
        fi
        apm_write_shell_receipt "disabled" "$_apm_previous_hook_dir" "$_apm_previous_hook_kind" "$_apm_previous_hook_digest" ||
            apm_echo "${YELLOW}[!] APM installed, but shell setup preference could not be saved.${NC}"
        echo "APM_NO_MODIFY_PATH is set; no shell profiles were changed."
        echo "To remove existing APM shell setup, delete only blocks marked 'apm shell setup' from your shell profiles."
        apm_print_path_guidance "$APM_INSTALL_DIR"
        return 0
    fi
    if [ "$_APM_MODIFY_PATH_REQUEST" = "inherit" ] &&
        [ "$_APM_PREVIOUS_SHELL_RECEIPT_VALID" = "1" ] &&
        [ "$_APM_PREVIOUS_SHELL_MODIFY_PATH" = "disabled" ]; then
        apm_write_shell_receipt "disabled" "$_APM_PREVIOUS_SHELL_HOOK_DIR" "$_APM_PREVIOUS_SHELL_HOOK_KIND" "$_APM_PREVIOUS_SHELL_HOOK_DIGEST" ||
            apm_echo "${YELLOW}[!] APM installed, but shell setup preference could not be saved.${NC}"
        echo "Shell PATH setup remains disabled by the previous APM installer preference."
        echo "To re-enable on a normal desktop shell, rerun with APM_NO_MODIFY_PATH=0."
        apm_print_path_guidance "$APM_INSTALL_DIR"
        return 0
    fi
    if ! apm_is_desktop_shell_setup_eligible; then
        apm_record_shell_setup_not_configured
        apm_echo "${YELLOW}[!] APM installed, but PATH was not configured automatically: $_APM_SHELL_SETUP_SKIP_REASON.${NC}"
        apm_print_path_guidance "$APM_INSTALL_DIR"
        return 0
    fi
    _apm_hook_dir="$HOME/.apm/shell"
    case "$_APM_DETECTED_SHELL" in
        fish)
            _apm_hook="$_apm_hook_dir/fish.fish"
            _apm_profile="$(apm_fish_profile)" || {
                apm_record_shell_setup_not_configured
                apm_echo "${YELLOW}[!] APM installed, but PATH was not configured automatically: fish config location is ambiguous.${NC}"
                apm_print_path_guidance "$APM_INSTALL_DIR"
                return 0
            }
            _apm_kind="fish"
            ;;
        zsh)
            _apm_hook="$_apm_hook_dir/env"
            _apm_profile="$(apm_zsh_profile)" || {
                apm_record_shell_setup_not_configured
                apm_echo "${YELLOW}[!] APM installed, but PATH was not configured automatically: zsh config location is ambiguous.${NC}"
                apm_print_path_guidance "$APM_INSTALL_DIR"
                return 0
            }
            _apm_kind="posix"
            ;;
        *)
            _apm_hook="$_apm_hook_dir/env"
            _apm_profile="$(apm_bash_login_profile)" || {
                apm_record_shell_setup_not_configured
                apm_echo "${YELLOW}[!] APM installed, but PATH was not configured automatically: bash login profile is not readable.${NC}"
                apm_print_path_guidance "$APM_INSTALL_DIR"
                return 0
            }
            _apm_kind="posix"
            ;;
    esac
    if ! apm_write_generated_hook "$_apm_hook" "$_apm_kind"; then
        apm_record_shell_setup_not_configured
        apm_echo "${YELLOW}[!] APM installed, but PATH was not configured automatically: existing hook is not owned by this installer.${NC}"
        apm_print_path_guidance "$APM_INSTALL_DIR"
        return 0
    fi
    _apm_block="$(apm_expected_profile_block "$_apm_hook" "$_APM_DETECTED_SHELL")"
    _apm_profile_failed=""
    _apm_primary_had_block=""
    _apm_primary_existed=""
    _apm_primary_backup=""
    _apm_primary_written_digest=""
    _apm_configured_profiles="$_apm_profile"
    _APM_PROFILE_CHANGE_STATUS=""
    if [ -f "$_apm_profile" ] && apm_profile_contains_exact_block "$_apm_profile" "$_apm_block"; then
        _apm_primary_had_block=1
    fi
    if [ -e "$_apm_profile" ] && [ ! -L "$_apm_profile" ]; then
        _apm_primary_existed=1
        _apm_primary_backup="$(apm_mktemp_in_dir "${_apm_profile%/*}" "${_apm_profile##*/}.apm-backup" || true)"
        if [ -n "$_apm_primary_backup" ]; then
            cp -p "$_apm_profile" "$_apm_primary_backup" || _apm_primary_backup=""
        fi
    fi
    if ! apm_update_profile_file "$_apm_profile" "$_apm_block"; then
        _apm_profile_failed="$_apm_profile"
        [ -z "$_apm_primary_backup" ] || rm -f "$_apm_primary_backup"
    else
        _apm_primary_written_digest="$(apm_file_digest "$_apm_profile" || true)"
    fi
    if [ -z "$_apm_profile_failed" ] && [ "$_APM_DETECTED_SHELL" = "bash" ]; then
        if ! apm_update_profile_file "$HOME/.bashrc" "$_apm_block"; then
            _apm_profile_failed="$HOME/.bashrc"
            [ -n "$_apm_primary_had_block" ] ||
                apm_restore_profile_after_partial_write "$_apm_profile" "$_apm_primary_backup" "$_apm_primary_existed" "$_apm_primary_written_digest" ||
                _APM_PROFILE_CHANGE_STATUS="partial"
        else
            _apm_configured_profiles="$_apm_configured_profiles and $HOME/.bashrc"
        fi
    fi
    [ -z "$_apm_primary_backup" ] || rm -f "$_apm_primary_backup"
    if [ -n "$_apm_profile_failed" ]; then
        apm_echo "${YELLOW}[!] APM installed, but PATH was not configured automatically: cannot safely update $_apm_profile_failed.${NC}"
        apm_write_shell_receipt "managed" "$_apm_hook_dir" "$_apm_kind" "$_APM_NEW_SHELL_HOOK_DIGEST" ||
            apm_echo "${YELLOW}[!] APM installed, but shell setup receipt could not be saved.${NC}"
        apm_print_path_guidance "$APM_INSTALL_DIR"
        return 0
    fi
    _APM_NEW_SHELL_HOOK_DIR="$_apm_hook_dir"
    _APM_NEW_SHELL_HOOK_KIND="$_apm_kind"
    apm_write_shell_receipt "managed" "$_APM_NEW_SHELL_HOOK_DIR" "$_APM_NEW_SHELL_HOOK_KIND" "$_APM_NEW_SHELL_HOOK_DIGEST" ||
        apm_echo "${YELLOW}[!] APM installed, but shell setup receipt could not be saved.${NC}"
    apm_echo "${GREEN}[+] Shell PATH configured for $_APM_DETECTED_SHELL: $_apm_configured_profiles.${NC}"
    _apm_current_shell="$(apm_detect_current_shell)"
    if [ "$_apm_current_shell" = "$_APM_DETECTED_SHELL" ]; then
        echo "Open a new terminal, or run this for the current shell:"
    else
        echo "Open a new $_APM_DETECTED_SHELL terminal, or run this for the current shell:"
    fi
    if [ "$_apm_current_shell" = "$_APM_DETECTED_SHELL" ] && [ "$_APM_DETECTED_SHELL" = "fish" ]; then
        printf '  source %s\n' "$(apm_fish_quote "$_apm_hook")"
    elif [ "$_apm_current_shell" = "$_APM_DETECTED_SHELL" ]; then
        printf '  . %s\n' "$(apm_shell_quote "$_apm_hook")"
    elif [ "$_apm_current_shell" = "fish" ]; then
        printf '  set -gx PATH %s $PATH\n' "$(apm_fish_quote "$APM_INSTALL_DIR")"
    else
        printf '  export PATH=%s:"$PATH"\n' "$(apm_shell_quote "$APM_INSTALL_DIR")"
    fi
}

apm_is_recognized_bundle() {
    [ -f "$1/apm" ] && [ ! -L "$1/apm" ] && {
        { [ -f "$1/.apm-installed" ] && [ ! -L "$1/.apm-installed" ]; } ||
            { [ -f "$1/VERSION" ] && [ ! -L "$1/VERSION" ] &&
                [ -d "$1/_internal" ] && [ ! -L "$1/_internal" ]; }
    }
}

apm_probe_installation() {
    case "$1" in
        */*) _probe_parent="${1%/*}"
             [ -n "$_probe_parent" ] || _probe_parent="/" ;;
        *) _probe_parent="." ;;
    esac
    _probe_original_parent="$_probe_parent"
    while [ "$_probe_parent" != "/" ] && [ "$_probe_parent" != "." ]; do
        if [ -d "$_probe_parent" ] && [ ! -x "$_probe_parent" ]; then
            apm_install_error "Cannot inspect existing APM through $_probe_parent: directory is not searchable. Ask its owner to repair permissions before retrying."
        fi
        case "$_probe_parent" in
            */*) _probe_parent="${_probe_parent%/*}"
                 [ -n "$_probe_parent" ] || _probe_parent="/" ;;
            *) _probe_parent="." ;;
        esac
    done
    [ -e "$1" ] || [ -L "$1" ] || return 0
    _candidate="$(apm_real_path "$1")" ||
        apm_install_error "Cannot resolve existing APM at $1. Repair this path before reinstalling."
    _candidate_lib="${_candidate%/*}"
    [ -n "$_candidate_lib" ] || _candidate_lib="/"
    if [ ! -f "$_candidate" ] || [ "${_candidate##*/}" != "apm" ]; then
        apm_install_error "Invalid existing APM launcher at $1. Repair it with its original installer before retrying."
    fi
    case "$_candidate" in
        */Cellar/*|*/Caskroom/*|*/.linuxbrew/*)
            apm_install_error "Package-manager installation at $1. Update or uninstall it with its package manager; the installer will not replace it." ;;
    esac
    if ! apm_is_recognized_bundle "$_candidate_lib"; then
        apm_install_error "Unrecognized or package-manager installation at $1. Update or uninstall it with its original installer; refusing a second installation."
    fi
    if [ -n "$_apm_existing_binary" ] && [ "$_candidate" != "$_apm_existing_binary" ]; then
        apm_install_error "Conflicting APM installations at $_apm_existing_binary and $1. Remove the unwanted installation with its owner before retrying."
    fi
    _apm_existing_binary="$_candidate"
    _apm_existing_lib="$_candidate_lib"
    if [ "$(apm_real_path "$_probe_original_parent")" != "$(apm_real_path "$_candidate_lib")" ]; then
        _apm_existing_bin="$_probe_original_parent"
    fi
}

# One destination/ownership authority for bootstrap and self-update.
# Arguments are historical system entry points, also checked when absent from PATH.
apm_resolve_install_paths() {
    _apm_existing_binary=""
    _apm_existing_lib=""
    _apm_existing_bin=""
    APM_INSTALL_DIR="$(apm_trim_trailing_slashes "$APM_INSTALL_DIR")"
    APM_LIB_DIR="$(apm_trim_trailing_slashes "$APM_LIB_DIR")"
    if [ -n "$_APM_PREFIX_SET" ]; then
        APM_INSTALL_PREFIX="$(apm_trim_trailing_slashes "$APM_INSTALL_PREFIX")"
        case "$APM_INSTALL_PREFIX" in
            /*) ;;
            *) apm_install_error "--prefix must be an absolute path. Use --prefix /path/to/root." ;;
        esac
        case "/$APM_INSTALL_PREFIX/" in
            */../*|*/./*) apm_install_error "--prefix must not contain dot segments. Supply a normalized absolute path." ;;
        esac
        if [ "$APM_INSTALL_PREFIX" = "/" ]; then
            _apm_prefix_install_dir="/bin"
            _apm_prefix_lib_dir="/lib/apm"
        else
            _apm_prefix_install_dir="$APM_INSTALL_PREFIX/bin"
            _apm_prefix_lib_dir="$APM_INSTALL_PREFIX/lib/apm"
        fi
        if [ -n "$_APM_INSTALL_DIR_SET" ] &&
            [ "$APM_INSTALL_DIR" != "$_apm_prefix_install_dir" ]; then
            apm_install_error "--prefix derives APM_INSTALL_DIR=$_apm_prefix_install_dir, but APM_INSTALL_DIR=$APM_INSTALL_DIR was also provided. Use one destination selector."
        fi
        if [ -n "$_APM_LIB_DIR_SET" ] &&
            [ "$APM_LIB_DIR" != "$_apm_prefix_lib_dir" ]; then
            apm_install_error "--prefix derives APM_LIB_DIR=$_apm_prefix_lib_dir, but APM_LIB_DIR=$APM_LIB_DIR was also provided. Use one destination selector."
        fi
        APM_INSTALL_DIR="$_apm_prefix_install_dir"
        APM_LIB_DIR="$_apm_prefix_lib_dir"
        _APM_INSTALL_DIR_SET=1
        _APM_LIB_DIR_SET=1
    fi
    for _dir in "$APM_INSTALL_DIR" "$APM_LIB_DIR"; do
        case "$_dir" in
            /*) ;;
            *) apm_install_error "Install destinations must be absolute paths. Set APM_INSTALL_DIR and APM_LIB_DIR to absolute paths." ;;
        esac
        case "/$_dir/" in
            */../*|*/./*) apm_install_error "Install destinations must not contain dot segments. Supply normalized absolute paths." ;;
        esac
    done
    if [ "$(id -u)" -eq 0 ] &&
        { [ -z "$_APM_INSTALL_DIR_SET" ] || [ -z "$_APM_LIB_DIR_SET" ]; }; then
        apm_install_error "Administrator installation requires explicit APM_INSTALL_DIR and APM_LIB_DIR, or --prefix PATH. Run as an ordinary user for ~/.local defaults."
    fi
    apm_probe_installation "$APM_INSTALL_DIR/apm"
    apm_probe_installation "$APM_LIB_DIR/apm"
    apm_probe_installation "$HOME/.local/bin/apm"
    apm_probe_installation "$HOME/.local/lib/apm/apm"
    for _entry in "$@"; do
        apm_probe_installation "$_entry"
    done
    # Parse PATH without word splitting or glob expansion (paths may contain spaces).
    _remaining_path="$PATH:"
    while [ -n "$_remaining_path" ]; do
        _entry="${_remaining_path%%:*}"
        _remaining_path="${_remaining_path#*:}"
        apm_probe_installation "${_entry:-.}/apm"
    done
    if [ -n "${APM_SELF_UPDATE_SOURCE:-}" ]; then
        [ -f "$APM_SELF_UPDATE_SOURCE" ] ||
            apm_install_error "The running APM installation is missing. Reinstall with its original installer."
        apm_probe_installation "$APM_SELF_UPDATE_SOURCE"
        [ -n "$_apm_existing_binary" ] ||
            apm_install_error "The running APM installation is missing. Reinstall with its original installer."
    fi
    if [ -n "$_apm_existing_binary" ]; then
        if [ -z "$_APM_INSTALL_DIR_SET" ]; then
            [ -n "$_apm_existing_bin" ] ||
                apm_install_error "Cannot locate the existing APM launcher. Set APM_INSTALL_DIR and APM_LIB_DIR to its original destinations."
            APM_INSTALL_DIR="$_apm_existing_bin"
        fi
        if [ -z "$_APM_LIB_DIR_SET" ]; then
            APM_LIB_DIR="$_apm_existing_lib"
        fi
        if [ "$(apm_real_path "$APM_INSTALL_DIR/apm")" != "$_apm_existing_binary" ] ||
            [ "$(apm_real_path "$APM_LIB_DIR")" != "$_apm_existing_lib" ]; then
            apm_install_error "Requested destinations conflict with existing APM at $_apm_existing_binary. Keep its original destinations or uninstall it with its owner before migrating."
        fi
    fi
    _apm_bin_real="$(apm_real_path "$APM_INSTALL_DIR")" ||
        apm_install_error "Cannot resolve APM_INSTALL_DIR. Supply a valid absolute destination."
    _apm_lib_real="$(apm_real_path "$APM_LIB_DIR")" ||
        apm_install_error "Cannot resolve APM_LIB_DIR. Supply a valid absolute destination."
    case "$_apm_bin_real/" in
        "$_apm_lib_real/"*) apm_install_error "APM_INSTALL_DIR overlaps the bundle. Choose separate launcher and bundle destinations." ;;
    esac
    case "$_apm_lib_real/" in
        "$_apm_bin_real/apm/"*) apm_install_error "APM_LIB_DIR overlaps the launcher. Choose separate launcher and bundle destinations." ;;
    esac
}

apm_require_writable_directory() {
    if ! mkdir -p "$1" 2>/dev/null || [ ! -w "$1" ] || [ ! -x "$1" ]; then
        apm_install_error "Destination $1 is not writable. Choose user-owned APM_INSTALL_DIR and APM_LIB_DIR for a fresh install, or ask the installation owner to update it. The installer never runs sudo."
    fi
}

apm_require_owned_bundle() {
    [ ! -L "$APM_LIB_DIR" ] ||
        apm_install_error "APM_LIB_DIR is a symlink. Supply the original bundle directory, not a symlink."
    if [ -d "$APM_LIB_DIR" ]; then
        # Pass directory names as arguments; shell builtins check each batch.
        _unmanageable="$(find "$APM_LIB_DIR" ! -user "$(id -u)" -print -o \
            -type d -exec sh -c '
                for _dir do
                    if [ ! -w "$_dir" ] || [ ! -x "$_dir" ]; then
                        printf "%s\n" "$_dir"
                    fi
                done
            ' apm-permission-check {} +)" ||
            apm_install_error "Cannot inspect bundle ownership or permissions. Ask its owner to repair or update $APM_LIB_DIR."
        if [ -n "$_unmanageable" ]; then
            apm_install_error "Existing bundle $APM_LIB_DIR is not writable, searchable, or owned by this user. Ask its owner to update it; no files were removed."
        fi
    fi
}
# INSTALL_OWNERSHIP_END

# Banner
apm_echo "${BLUE}"
echo "+--------------------------------------------------------------+"
echo "|                         APM Installer                        |"
echo "|              The NPM for AI-Native Development               |"
echo "+--------------------------------------------------------------+"
apm_echo "${NC}"

# Platform detection
OS=$(uname -s)
ARCH=$(uname -m)

# Normalize architecture names
case $ARCH in
    x86_64)
        ARCH="x86_64"
        ;;
    arm64|aarch64)
        ARCH="arm64"
        ;;
    *)
        apm_echo "${RED}Error: Unsupported architecture: $ARCH${NC}"
        echo "Supported architectures: x86_64, arm64"
        exit 1
        ;;
esac

# Normalize OS names and set binary name
case $OS in
    Darwin)
        PLATFORM="darwin"
        DOWNLOAD_BINARY="apm-darwin-$ARCH.tar.gz"
        EXTRACTED_DIR="apm-darwin-$ARCH"
        ;;
    Linux)
        PLATFORM="linux"
        DOWNLOAD_BINARY="apm-linux-$ARCH.tar.gz"
        EXTRACTED_DIR="apm-linux-$ARCH"
        ;;
    *)
        apm_echo "${RED}Error: Unsupported operating system: $OS${NC}"
        echo "Supported platforms: macOS (Darwin), Linux"
        exit 1
        ;;
esac

apm_echo "${BLUE}Detected platform: $PLATFORM-$ARCH${NC}"
apm_echo "${BLUE}Target binary: $DOWNLOAD_BINARY${NC}"

# Parse options: --prefix PATH / --prefix=PATH, @v1.2.3, or VERSION env var.
apm_parse_installer_args "$@"
apm_parse_modify_path_env

# Enterprise bootstrap mirror helpers
is_truthy() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

is_public_github_url() {
    [ "${GITHUB_URL:-https://github.com}" = "https://github.com" ] || [ "${GITHUB_URL%/}" = "https://github.com" ]
}

fail_closed_error() {
    # $1 is a literal env var name from this script, not user input.
    apm_echo "${RED}Error: APM_NO_DIRECT_FALLBACK is set, but $1 is not configured.${NC}"
    shift
    printf '%s\n' "$*"
    exit 1
}

join_url_path() {
    _base="${1%/}"
    shift
    for _part in "$@"; do
        _part="${_part#/}"
        _part="${_part%/}"
        _base="$_base/$_part"
    done
    printf '%s' "$_base"
}

redact_url_credentials() {
    printf '%s' "$1" | sed -E 's#([A-Za-z][A-Za-z0-9+.-]*://)[^/@[:space:]]+@#\1***@#g'
}

release_metadata_url() {
    if [ -n "$APM_RELEASE_METADATA_URL" ]; then
        printf '%s' "${APM_RELEASE_METADATA_URL%/}"
    elif is_public_github_url; then
        printf 'https://api.github.com/repos/%s/releases/latest' "$APM_REPO"
    else
        printf '%s/api/v3/repos/%s/releases/latest' "${GITHUB_URL%/}" "$APM_REPO"
    fi
}

release_asset_url() {
    _tag_name="$1"
    _asset_name="$2"
    if [ -n "$APM_RELEASE_BASE_URL" ]; then
        join_url_path "$APM_RELEASE_BASE_URL" "$_tag_name" "$_asset_name"
    else
        printf '%s/%s/releases/download/%s/%s' "${GITHUB_URL%/}" "$APM_REPO" "$_tag_name" "$_asset_name"
    fi
}

pip_index_args() {
    if [ -n "$APM_PYPI_INDEX_URL" ]; then
        printf '%s %s' '--index-url' "$APM_PYPI_INDEX_URL"
    fi
}

fetch_release_metadata() {
    # Capture status and headers as well as the body; HTTP errors are not JSON errors.
    # -q ignores ambient curlrc auth/redirect settings. No redirects or token logging.
    CURL_EXIT_CODE=0
    if [ "$1" = "authenticated" ]; then
        METADATA_RESPONSE=$(curl -q -s -i --suppress-connect-headers --connect-timeout 10 --max-time 30 \
            -w '\n%{http_code}' -H "Authorization: token $AUTH_HEADER_VALUE" "$LATEST_RELEASE_URL") || CURL_EXIT_CODE=$?
    else
        METADATA_RESPONSE=$(curl -q -s -i --suppress-connect-headers --connect-timeout 10 --max-time 30 \
            -w '\n%{http_code}' "$LATEST_RELEASE_URL") || CURL_EXIT_CODE=$?
    fi
    # Skip informational header blocks, then stream the final response unchanged.
    # Never repeatedly concatenate the body: pretty-printed mirrors can be large.
    METADATA_RESPONSE=$(printf '%s\n' "$METADATA_RESPONSE" | awk '
        BEGIN { headers=1 }
        headers && /^HTTP\/[0-9.]+ [0-9][0-9][0-9]/ { interim=($2 >= 100 && $2 < 200) }
        headers && /^\r?$/ && !interim { headers=0 }
        !interim { print }')
    METADATA_STATUS=$(printf '%s\n' "$METADATA_RESPONSE" | tail -n 1)
    METADATA_HEADERS=$(printf '%s\n' "$METADATA_RESPONSE" | sed -n '1,/^\r\{0,1\}$/p')
    LATEST_RELEASE=$(printf '%s\n' "$METADATA_RESPONSE" | sed '1,/^\r\{0,1\}$/d' | sed '$d')
}

metadata_is_rate_limited() {
    [ "$METADATA_STATUS" = "429" ] || {
        [ "$METADATA_STATUS" = "403" ] && {
            printf '%s\n' "$METADATA_HEADERS" | grep -Eiq '^x-ratelimit-remaining: *0[[:space:]]*$|^retry-after: *[1-9][0-9]*[[:space:]]*$' ||
            printf '%s\n' "$LATEST_RELEASE" | grep -Eiq 'API rate limit exceeded|secondary rate limit|abuse detection mechanism'
        }
    }
}

# Function to check Python availability and version
check_python_requirements() {
    PYTHON_CMD=""
    # Check if Python is available
    if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
        return 1  # Python not available
    fi
    
    # Get Python command
    PYTHON_CMD="python3"
    if ! command -v python3 >/dev/null 2>&1; then
        PYTHON_CMD="python"
    fi
    
    # Check Python version (need 3.10+)
    PYTHON_VERSION=$($PYTHON_CMD -c 'import sys; print(".".join(map(str, sys.version_info[:2])))' 2>/dev/null)
    if [ -z "$PYTHON_VERSION" ]; then
        return 1
    fi
    
    # Compare version (need >= 3.10)
    REQUIRED_VERSION="3.10"
    if [ "$(printf '%s\n' "$REQUIRED_VERSION" "$PYTHON_VERSION" | sort -V | head -n1)" = "$REQUIRED_VERSION" ]; then
        return 0  # Python version is sufficient
    else
        return 1  # Python version too old
    fi
}

print_selected_pip_install_command() {
    [ -n "$PYTHON_CMD" ] || return 1
    if [ -n "$APM_PYPI_INDEX_URL" ]; then
        printf '  %s -m pip install --user --index-url "%s" apm-cli\n' \
            "$PYTHON_CMD" '$APM_PYPI_INDEX_URL'
    elif is_truthy "$APM_NO_DIRECT_FALLBACK"; then
        echo "  Set APM_PYPI_INDEX_URL to your internal PyPI proxy, then rerun this installer."
    else
        printf '  %s -m pip install --user apm-cli\n' "$PYTHON_CMD"
    fi
}

print_pip_recovery_guidance() {
    case "${PIP_FALLBACK_FAILURE:-python-unavailable}" in
        python-unavailable)
            apm_echo "${YELLOW}Python 3.10+ is not available on this system.${NC}"
            echo ""
            echo "Install Python 3.10+ first, then rerun this installer:"
            echo "  Ubuntu/Debian: sudo apt-get update && sudo apt-get install python3 python3-pip"
            echo "  CentOS/RHEL: sudo yum install python3 python3-pip"
            echo "  Alpine: apk add python3 py3-pip"
            echo "  macOS: brew install python3"
            ;;
        pip-unavailable)
            apm_echo "${YELLOW}pip is not available for $PYTHON_CMD.${NC}"
            echo "Install pip for that interpreter, then run:"
            print_selected_pip_install_command
            ;;
        install-failed)
            apm_echo "${YELLOW}The selected Python pip installation failed.${NC}"
            echo "After resolving the reported pip error, retry:"
            print_selected_pip_install_command
            ;;
    esac
}

# Function to attempt pip installation
try_pip_installation() {
    apm_resolve_install_paths /usr/local/bin/apm /opt/homebrew/bin/apm /usr/local/lib/apm/apm
    if [ -n "$_apm_existing_binary" ] || [ -n "$_APM_INSTALL_DIR_SET" ] ||
        [ -n "$_APM_LIB_DIR_SET" ] || [ "$(id -u)" -eq 0 ]; then
        apm_install_error "Pip fallback cannot preserve these installation destinations. Update with the original installer, or uninstall the existing installation before choosing pip."
    fi
    if ! check_python_requirements; then
        PIP_FALLBACK_FAILURE="python-unavailable"
        return 1
    fi
    
    # Query and invoke pip through the same interpreter, not an unrelated launcher.
    if ! "$PYTHON_CMD" -m pip --version >/dev/null 2>&1; then
        apm_echo "${RED}Error: pip is not available for $PYTHON_CMD${NC}"
        PIP_FALLBACK_FAILURE="pip-unavailable"
        return 1
    fi
    PIP_SCRIPTS_DIR="$("$PYTHON_CMD" -c 'import sysconfig; print(sysconfig.get_path("scripts", scheme=sysconfig.get_preferred_scheme("user")))')" ||
        apm_install_error "Cannot determine the pip user-script directory. Repair this Python installation before retrying; no package was installed."
    [ -n "$PIP_SCRIPTS_DIR" ] ||
        apm_install_error "Cannot determine the pip user-script directory. Repair this Python installation before retrying; no package was installed."
    
    # Try to install. In fail-closed mode, never fall back to public PyPI.
    if [ -n "$APM_PYPI_INDEX_URL" ]; then
        apm_echo "${BLUE}Attempting installation via $PYTHON_CMD -m pip...${NC}"
        apm_echo "${BLUE}Using APM_PYPI_INDEX_URL mirror for pip install.${NC}"
        PIP_INSTALL_OK=0
        "$PYTHON_CMD" -m pip install --user --index-url "$APM_PYPI_INDEX_URL" apm-cli || PIP_INSTALL_OK=$?
    elif is_truthy "$APM_NO_DIRECT_FALLBACK"; then
        fail_closed_error APM_PYPI_INDEX_URL "Set APM_PYPI_INDEX_URL to your internal PyPI proxy before using pip fallback."
    else
        apm_echo "${BLUE}Attempting installation via $PYTHON_CMD -m pip...${NC}"
        PIP_INSTALL_OK=0
        "$PYTHON_CMD" -m pip install --user apm-cli || PIP_INSTALL_OK=$?
    fi

    if [ "$PIP_INSTALL_OK" -eq 0 ]; then
        apm_echo "${GREEN}[+] APM installed successfully via pip!${NC}"
        _apm_run_hint="apm"
        
        # Check if apm is now available
        if command -v apm >/dev/null 2>&1; then
            INSTALLED_VERSION=$(apm --version 2>/dev/null || echo "unknown")
            apm_echo "${BLUE}Version: $INSTALLED_VERSION${NC}"
            apm_echo "${BLUE}Location: $(which apm)${NC}"
        else
            apm_echo "${YELLOW}[!] APM installed but not found in PATH${NC}"
            apm_print_path_guidance "$PIP_SCRIPTS_DIR"
        fi
        
        echo ""
        apm_echo "${GREEN}Installation complete!${NC}"
        echo ""
        apm_echo "${BLUE}Quick start:${NC}"
        printf '  %s init my-app          # Create a new APM project\n' "$_apm_run_hint"
        printf '  cd my-app && %s install # Install dependencies\n' "$_apm_run_hint"
        printf '  %s run                  # Run your first prompt\n' "$_apm_run_hint"
        echo ""
        apm_echo "${BLUE}Documentation:${NC} $GITHUB_URL/$APM_REPO"
        return 0
    else
        apm_echo "${RED}Error: pip installation failed${NC}"
        PIP_FALLBACK_FAILURE="install-failed"
        return 1
    fi
}

# Reject invalid requests before compatibility checks or external work.
apm_resolve_install_paths /usr/local/bin/apm /opt/homebrew/bin/apm /usr/local/lib/apm/apm

# Early glibc compatibility check for Linux
if [ "$PLATFORM" = "linux" ]; then
    # Get glibc version
    GLIBC_VERSION=$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
    REQUIRED_GLIBC="2.35"
    
    if [ -n "$GLIBC_VERSION" ]; then
        # Compare versions
        if [ "$(printf '%s\n' "$REQUIRED_GLIBC" "$GLIBC_VERSION" | sort -V | head -n1)" != "$REQUIRED_GLIBC" ]; then
            apm_echo "${YELLOW}[!] Compatibility Issue Detected${NC}"
            apm_echo "${YELLOW}Your glibc version: $GLIBC_VERSION${NC}"
            apm_echo "${YELLOW}Required version: $REQUIRED_GLIBC or newer${NC}"
            echo ""
            echo "The prebuilt binary will not work on your system."
            echo ""
            
            if try_pip_installation; then
                exit 0
            fi
            print_pip_recovery_guidance
            echo ""
            echo "Other installation options:"
            echo "  1. Use a system with glibc 2.35+ for the prebuilt binary"
            echo "  2. Build from source: git clone $GITHUB_URL/$APM_REPO.git && cd apm && uv sync && uv run pip install -e ."
            exit 1
        fi
    fi
fi

# Detect if running in a container and check compatibility
if [ -f "/.dockerenv" ] || [ -f "/run/.containerenv" ] || grep -q "/docker/" /proc/1/cgroup 2>/dev/null; then
    apm_echo "${YELLOW}[!] Container/Dev Container environment detected${NC}"
    apm_echo "${YELLOW}Note: PyInstaller binaries may have compatibility issues in containers.${NC}"
    apm_echo "${YELLOW}The installer will test the binary before changing the installation.${NC}"
    echo ""
fi

# Resolve auth token (needed for both API and download paths)
# Precedence: GITHUB_APM_PAT > GITHUB_TOKEN > GH_TOKEN (mirrors version_checker.py)
if [ -n "$GITHUB_APM_PAT" ]; then
    AUTH_HEADER_VALUE="$GITHUB_APM_PAT"
elif [ -n "$GITHUB_TOKEN" ]; then
    AUTH_HEADER_VALUE="$GITHUB_TOKEN"
elif [ -n "$GH_TOKEN" ]; then
    AUTH_HEADER_VALUE="$GH_TOKEN"
fi

# When VERSION is provided, skip GitHub API and compute download URL directly
if [ -n "$VERSION" ]; then
    TAG_NAME="$VERSION"
    if is_truthy "$APM_NO_DIRECT_FALLBACK" && [ -z "$APM_RELEASE_BASE_URL" ] && is_public_github_url; then
        fail_closed_error APM_RELEASE_BASE_URL "Set APM_RELEASE_BASE_URL to a mirror containing $TAG_NAME/$DOWNLOAD_BINARY."
    fi
    DOWNLOAD_URL=$(release_asset_url "$TAG_NAME" "$DOWNLOAD_BINARY")
    apm_echo "${GREEN}Version: $TAG_NAME${NC}"
    apm_echo "${BLUE}Download URL: $(redact_url_credentials "$DOWNLOAD_URL")${NC}"
fi

if [ -z "$TAG_NAME" ]; then
# Get latest release info
apm_echo "${YELLOW}Fetching latest release information...${NC}"

if is_truthy "$APM_NO_DIRECT_FALLBACK" && [ -z "$APM_RELEASE_METADATA_URL" ] && is_public_github_url; then
    fail_closed_error APM_RELEASE_METADATA_URL "Set APM_RELEASE_METADATA_URL to mirrored latest.json, or set VERSION to a pinned release."
fi

LATEST_RELEASE_URL=$(release_metadata_url)

# Fetch release info; include Authorization header when a token is already resolved
# (AUTH_HEADER_VALUE set earlier from GITHUB_APM_PAT > GITHUB_TOKEN > GH_TOKEN precedence).
# This avoids anonymous rate-limiting behind shared IPs / corporate NAT.
# Only attach the token when the request targets the canonical GitHub / configured
# GHES host. When APM_RELEASE_METADATA_URL routes to an operator mirror, the request
# stays UNAUTHENTICATED so the GitHub token is never transmitted cross-host (matches
# install.ps1, which fetches mirror metadata unauthenticated).
if [ -n "$AUTH_HEADER_VALUE" ] && [ -z "$APM_RELEASE_METADATA_URL" ]; then
    fetch_release_metadata authenticated
else
    fetch_release_metadata anonymous
fi

# Only the known public APM repository may recover from a rejected ambient token.
# Keep accepted tokens first for shared-IP rate limits; never downgrade throttles.
if [ "$CURL_EXIT_CODE" -eq 0 ] && [ -n "$AUTH_HEADER_VALUE" ] &&
    [ -z "$APM_RELEASE_METADATA_URL" ] && is_public_github_url &&
    [ "$APM_REPO" = "microsoft/apm" ] && ! is_truthy "$APM_NO_DIRECT_FALLBACK" &&
    { [ "$METADATA_STATUS" = "401" ] || [ "$METADATA_STATUS" = "403" ]; } &&
    ! metadata_is_rate_limited; then
    apm_echo "${BLUE}Credential rejected for public APM metadata (HTTP $METADATA_STATUS); retrying once anonymously.${NC}"
    fetch_release_metadata anonymous
fi

if [ "$CURL_EXIT_CODE" -ne 0 ]; then
    apm_echo "${RED}Error: Release metadata network request failed (curl $CURL_EXIT_CODE).${NC}"
    echo "Check connectivity, proxy and TLS settings, then retry."
    exit 1
fi

if metadata_is_rate_limited; then
    apm_echo "${RED}Error: Release metadata rate limit (HTTP $METADATA_STATUS).${NC}"
    echo "Wait for the limit to reset; for anonymous shared-IP limits, configure an accepted GitHub token."
    exit 1
fi

case "$METADATA_STATUS" in
    200) ;;
    401|403)
        apm_echo "${RED}Error: Release metadata authentication/authorization failed (HTTP $METADATA_STATUS).${NC}"
        echo "Check the credential's validity and repository access, or pin VERSION."
        exit 1 ;;
    3??)
        apm_echo "${RED}Error: Release metadata redirects are not followed (HTTP $METADATA_STATUS).${NC}"
        echo "Set APM_RELEASE_METADATA_URL to the final JSON endpoint, or pin VERSION."
        exit 1 ;;
    *)
        apm_echo "${RED}Error: Release metadata request failed (HTTP $METADATA_STATUS).${NC}"
        echo "Check the configured host/repository or metadata mirror, or pin VERSION."
        exit 1 ;;
esac

if [ -n "$APM_RELEASE_METADATA_URL" ] && [ -z "$LATEST_RELEASE" ]; then
    apm_echo "${RED}Error: Empty release metadata from APM_RELEASE_METADATA_URL${NC}"
    echo "Mirror URL: $(redact_url_credentials "$APM_RELEASE_METADATA_URL")"
    echo "Check that the mirror is reachable and publishes GitHub-compatible latest.json."
    exit 1
fi

# Check if we got a valid response (should contain tag_name)
if ! echo "$LATEST_RELEASE" | grep -q '"tag_name":'; then
    if [ -n "$APM_RELEASE_METADATA_URL" ]; then
        apm_echo "${RED}Error: Invalid release metadata from APM_RELEASE_METADATA_URL${NC}"
        echo "Mirror URL: $(redact_url_credentials "$APM_RELEASE_METADATA_URL")"
        echo "Publish a GitHub-compatible JSON document with a tag_name field."
        exit 1
    fi
    apm_echo "${RED}Error: Invalid release metadata; expected a tag_name field.${NC}"
    echo "Check the configured metadata source or pin VERSION to a known release."
    exit 1
fi

# Extract tag name and download URLs
# Use grep -o to extract just the matching portion (handles single-line JSON)
TAG_NAME=$(echo "$LATEST_RELEASE" | grep -o '"tag_name": *"[^"]*"' | awk -F'"' '{print $4}')
if is_truthy "$APM_NO_DIRECT_FALLBACK" && [ -z "$APM_RELEASE_BASE_URL" ] && is_public_github_url; then
    fail_closed_error APM_RELEASE_BASE_URL "Set APM_RELEASE_BASE_URL to a mirror containing $TAG_NAME/$DOWNLOAD_BINARY."
fi
DOWNLOAD_URL=$(release_asset_url "$TAG_NAME" "$DOWNLOAD_BINARY")

# Extract API asset URL for private repository downloads. Do not use GitHub API
# asset fallback when APM_RELEASE_BASE_URL is set; mirror mode must fail closed.
ASSET_URL=""
if [ -z "$APM_RELEASE_BASE_URL" ]; then
    ASSET_URL=$(echo "$LATEST_RELEASE" | grep -B 3 "\"name\": \"$DOWNLOAD_BINARY\"" | grep -o '"url": *"[^"]*"' | awk -F'"' '{print $4}')
fi

if [ -z "$TAG_NAME" ]; then
    apm_echo "${RED}Error: Could not determine latest release version${NC}"
    echo ""
    echo "This could mean:"
    echo "  1. No releases found in the repository"
    echo "  2. API response format is unexpected"
    echo "  3. Token doesn't have sufficient permissions"
    echo "  4. Repository doesn't exist or is inaccessible"
    exit 1
fi

apm_echo "${GREEN}Latest version: $TAG_NAME${NC}"
apm_echo "${BLUE}Download URL: $(redact_url_credentials "$DOWNLOAD_URL")${NC}"
fi

# Create temporary directory
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

# Integrity failures never enter the binary-compatibility pip fallback.
checksum_error() {
    apm_echo "${RED}Error: $1${NC}"
    printf '%s\n' "$2"
    exit 1
}

download_checksum_with_auth() {
    # Construct API URLs on the configured GitHub host, never a metadata URL.
    if is_public_github_url; then
        CHECKSUM_API="https://api.github.com/repos/$APM_REPO/releases"
    else
        CHECKSUM_API="${GITHUB_URL%/}/api/v3/repos/$APM_REPO/releases"
    fi
    CHECKSUM_RELEASE="${LATEST_RELEASE:-}"
    if [ -z "$CHECKSUM_RELEASE" ]; then
        if ! CHECKSUM_RELEASE=$(curl -L --fail --silent --show-error \
            -H "Authorization: token $AUTH_HEADER_VALUE" \
            "$CHECKSUM_API/tags/$TAG_NAME"); then
            CHECKSUM_RELEASE=""
        fi
    fi
    # Read only direct name/id fields of objects in the root assets array.
    # Tokenize quoted strings so compact JSON and nested uploader IDs are safe.
    CHECKSUM_ASSET_ID=$(printf '%s\n' "$CHECKSUM_RELEASE" | LC_ALL=C awk -v asset="$DOWNLOAD_BINARY.sha256" '
        {
            rest = $0
            while (match(rest, /"([^"\\]|\\.)*"|[][{}:,]|[^][{}:,[:space:]]+/)) {
                token = substr(rest, RSTART, RLENGTH)
                rest = substr(rest, RSTART + RLENGTH)
                if (token == "{" || token == "[") {
                    if (token == "[" && depth == 1 && key[depth] == "\"assets\"")
                        assets = depth + 1
                    depth++
                    kind[depth] = token
                    key[depth] = ""
                    want_key[depth] = (token == "{")
                    if (assets && depth == assets + 1 && token == "{") {
                        name = id = ""
                        names = ids = 0
                    }
                } else if (token == "}" || token == "]") {
                    if (assets && depth == assets + 1 && token == "}" &&
                        name == "\"" asset "\"") {
                        matches++
                        if (names == 1 && ids == 1) selected = id
                    }
                    if (depth == assets) assets = 0
                    depth--
                } else if (token == ",") {
                    want_key[depth] = (kind[depth] == "{")
                } else if (token != ":") {
                    if (want_key[depth]) {
                        key[depth] = token
                        want_key[depth] = 0
                    } else if (assets && depth == assets + 1) {
                        if (key[depth] == "\"name\"") { name = token; names++ }
                        if (key[depth] == "\"id\"") {
                            ids++
                            id = (token ~ /^[1-9][0-9]*$/) ? token : ""
                        }
                    }
                }
            }
        }
        END { if (depth == 0 && matches == 1) printf "%s", selected }
    ')
    if [ -n "$CHECKSUM_ASSET_ID" ]; then
        if curl -L --fail --silent --show-error \
            -H "Authorization: token $AUTH_HEADER_VALUE" \
            -H "Accept: application/octet-stream" \
            "$CHECKSUM_API/assets/$CHECKSUM_ASSET_ID" -o "$CHECKSUM_PATH"; then
            return 0
        fi
    fi
    curl -L --fail --silent --show-error \
        -H "Authorization: token $AUTH_HEADER_VALUE" \
        "$CHECKSUM_URL" -o "$CHECKSUM_PATH"
}

if command -v sha256sum >/dev/null 2>&1; then
    CHECKSUM_TOOL="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
    CHECKSUM_TOOL="shasum"
else
    checksum_error "SHA-256 verification is unavailable." \
        "Install sha256sum (coreutils) or shasum (Perl Digest::SHA), then retry."
fi

if [ -n "$APM_RELEASE_BASE_URL" ]; then
    CHECKSUM_REMEDIATION="Check mirror access. Ask the mirror operator to synchronize the original publisher archive and $TAG_NAME/$DOWNLOAD_BINARY.sha256 together, then retry."
else
    CHECKSUM_REMEDIATION="Check release access (and token permissions for private releases). Retry with a release that publishes $DOWNLOAD_BINARY.sha256; report missing or invalid sidecars to the release maintainer."
fi
CHECKSUM_URL=$(release_asset_url "$TAG_NAME" "$DOWNLOAD_BINARY.sha256")
CHECKSUM_PATH="$TMP_DIR/$DOWNLOAD_BINARY.sha256"
apm_echo "${YELLOW}Fetching archive checksum...${NC}"
if ! curl -L --fail --silent --show-error "$CHECKSUM_URL" -o "$CHECKSUM_PATH"; then
    # Mirrors never receive GitHub auth or fall back to GitHub assets.
    if [ -n "$AUTH_HEADER_VALUE" ] && [ -z "$APM_RELEASE_BASE_URL" ]; then
        if ! download_checksum_with_auth; then
            checksum_error "Could not download the release checksum." "$CHECKSUM_REMEDIATION"
        fi
    else
        checksum_error "Could not download the release checksum." "$CHECKSUM_REMEDIATION"
    fi
fi

# Accept one standard record bound to this basename, before the larger download.
if ! EXPECTED_SHA256=$(LC_ALL=C awk -v asset="$DOWNLOAD_BINARY" '
    {
        sub(/\r$/, "")
        digest = substr($0, 1, 64)
        separator = substr($0, 65, 2)
        if (NR != 1 || length(digest) != 64 || digest ~ /[^0-9a-fA-F]/ ||
            (separator != "  " && separator != " *") || substr($0, 67) != asset)
            exit 1
    }
    END {
        if (NR != 1) exit 1
        print tolower(digest)
    }
' "$CHECKSUM_PATH"); then
    checksum_error "Malformed release checksum." "$CHECKSUM_REMEDIATION"
fi

# Download binary
apm_echo "${YELLOW}Downloading APM...${NC}"

# Try downloading without authentication first (for public repos)
if curl -L --fail --progress-bar "$DOWNLOAD_URL" -o "$TMP_DIR/$DOWNLOAD_BINARY"; then
    apm_echo "${GREEN}[+] Download successful${NC}"
else
    # If unauthenticated download fails, try with authentication if available.
    # Never attach the token in mirror mode: APM_RELEASE_BASE_URL points at an
    # operator-configured host, so a failed mirror download must fail closed below
    # (matches install.ps1, which leaves mirror asset downloads unauthenticated).
    if [ -n "$AUTH_HEADER_VALUE" ] && [ -z "$APM_RELEASE_BASE_URL" ]; then
        apm_echo "${BLUE}Download failed, retrying with authentication...${NC}"
        
        # For private repositories, use GitHub API with proper headers
        if [ -n "$ASSET_URL" ]; then
            apm_echo "${BLUE}Using GitHub API for private repository access...${NC}"
            if curl -L --fail --progress-bar \
                -H "Authorization: token $AUTH_HEADER_VALUE" \
                -H "Accept: application/octet-stream" \
                "$ASSET_URL" -o "$TMP_DIR/$DOWNLOAD_BINARY"; then
                apm_echo "${GREEN}[+] Download successful via GitHub API${NC}"
            else
                apm_echo "${BLUE}GitHub API download failed, trying direct URL with auth...${NC}"
                if curl -L --fail --progress-bar -H "Authorization: token $AUTH_HEADER_VALUE" "$DOWNLOAD_URL" -o "$TMP_DIR/$DOWNLOAD_BINARY"; then
                    apm_echo "${GREEN}[+] Download successful with authentication${NC}"
                else
                    apm_echo "${RED}Error: Failed to download APM CLI even with authentication${NC}"
                    echo "Direct URL: $(redact_url_credentials "$DOWNLOAD_URL")"
                    echo "API URL: $(redact_url_credentials "$ASSET_URL")"
                    echo "This might mean:"
                    echo "  1. No binary available for your platform ($PLATFORM-$ARCH)"
                    echo "  2. Network connectivity issues"
                    echo "  3. The release doesn't include binaries yet"
                    echo "  4. Invalid GitHub token or insufficient permissions"
                    echo ""
                    echo "For private repositories, ensure your token has the required permissions."
                    echo "You can try installing from source instead:"
                    echo "  git clone $GITHUB_URL/$APM_REPO.git"
                    echo "  cd apm && uv sync && uv run pip install -e ."
                    exit 1
                fi
            fi
        else
            apm_echo "${BLUE}No API URL available, trying direct URL with auth...${NC}"
            if curl -L --fail --progress-bar -H "Authorization: token $AUTH_HEADER_VALUE" "$DOWNLOAD_URL" -o "$TMP_DIR/$DOWNLOAD_BINARY"; then
                apm_echo "${GREEN}[+] Download successful with authentication${NC}"
            else
                if [ -n "$APM_RELEASE_BASE_URL" ]; then
                    apm_echo "${RED}Error: Failed to download APM CLI from APM_RELEASE_BASE_URL mirror${NC}"
                    echo "Mirror URL: $(redact_url_credentials "$DOWNLOAD_URL")"
                    echo "Check that the mirror is reachable and contains $TAG_NAME/$DOWNLOAD_BINARY."
                    exit 1
                fi
                apm_echo "${RED}Error: Failed to download APM CLI even with authentication${NC}"
                echo "URL: $(redact_url_credentials "$DOWNLOAD_URL")"
                echo "This might mean:"
                echo "  1. No binary available for your platform ($PLATFORM-$ARCH)"
                echo "  2. Network connectivity issues"
                echo "  3. The release doesn't include binaries yet"
                echo "  4. Invalid GitHub token or insufficient permissions"
                echo ""
                echo "For private repositories, ensure your token has the required permissions."
                echo "You can try installing from source instead:"
                echo "  git clone $GITHUB_URL/$APM_REPO.git"
                echo "  cd apm && uv sync && uv run pip install -e ."
                exit 1
            fi
        fi
    else
        if [ -n "$APM_RELEASE_BASE_URL" ]; then
            apm_echo "${RED}Error: Failed to download APM CLI from APM_RELEASE_BASE_URL mirror${NC}"
            echo "Mirror URL: $(redact_url_credentials "$DOWNLOAD_URL")"
            echo "Check that the mirror is reachable and contains $TAG_NAME/$DOWNLOAD_BINARY."
            exit 1
        fi
        apm_echo "${RED}Error: Failed to download APM${NC}"
        echo "URL: $(redact_url_credentials "$DOWNLOAD_URL")"
        echo "This might mean:"
        echo "  1. No binary available for your platform ($PLATFORM-$ARCH)"
        echo "  2. Network connectivity issues"
        echo "  3. The release doesn't include binaries yet"
        echo "  4. Private repository requires authentication"
        echo ""
        echo "For private repositories, set GITHUB_APM_PAT environment variable:"
        echo "  export GITHUB_APM_PAT=your_token_here"
        echo "  curl -sSL -H \"Authorization: token \$GITHUB_APM_PAT\" \\"
        echo "    https://raw.githubusercontent.com/microsoft/apm/main/install.sh | \\"
        echo "    GITHUB_APM_PAT=\$GITHUB_APM_PAT sh"
        echo ""
        echo "You can also try installing from source:"
        echo "  git clone $GITHUB_URL/$APM_REPO.git"
        echo "  cd apm && uv sync && uv run pip install -e ."
        exit 1
    fi
fi

# Verify the exact release archive before extraction or any binary execution.
apm_echo "${YELLOW}Verifying archive checksum...${NC}"
if [ "$CHECKSUM_TOOL" = "sha256sum" ]; then
    if ! HASH_OUTPUT=$(sha256sum "$TMP_DIR/$DOWNLOAD_BINARY"); then
        checksum_error "SHA-256 hashing failed." "Check the downloaded archive and sha256sum installation, then retry."
    fi
else
    if ! HASH_OUTPUT=$(shasum -a 256 "$TMP_DIR/$DOWNLOAD_BINARY"); then
        checksum_error "SHA-256 hashing failed." "Check the downloaded archive and shasum installation, then retry."
    fi
fi
ACTUAL_SHA256=$(printf '%s\n' "$HASH_OUTPUT" | awk '{print tolower($1)}')
if [ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]; then
    if [ -n "$APM_RELEASE_BASE_URL" ]; then
        CHECKSUM_REMEDIATION="Ask the mirror operator to resynchronize the original publisher archive and sidecar together. Do not bypass verification."
    else
        CHECKSUM_REMEDIATION="Retry once; if the mismatch repeats, report it to the release maintainer. Do not bypass verification."
    fi
    checksum_error "Archive checksum verification failed for $DOWNLOAD_BINARY." "$CHECKSUM_REMEDIATION"
fi
apm_echo "${GREEN}[+] Archive checksum verified${NC}"

# Extract binary from tar.gz
apm_echo "${YELLOW}Extracting binary...${NC}"
if tar -xzf "$TMP_DIR/$DOWNLOAD_BINARY" -C "$TMP_DIR"; then
    apm_echo "${GREEN}[+] Extraction successful${NC}"
else
    apm_echo "${RED}Error: Failed to extract binary from archive${NC}"
    exit 1
fi

# Make binary executable
chmod +x "$TMP_DIR/$EXTRACTED_DIR/$BINARY_NAME"

# INSTALL_BINARY_CHECK_BEGIN
# Test the binary
# Use if/else to capture exit code without triggering set -e.
# When glibc is too old the binary exits 255 immediately;
# we must survive that so the pip-fallback path below is reachable.
apm_echo "${YELLOW}Testing binary...${NC}"
if BINARY_TEST_OUTPUT=$("$TMP_DIR/$EXTRACTED_DIR/$BINARY_NAME" --version 2>&1); then
    BINARY_TEST_EXIT_CODE=0
else
    BINARY_TEST_EXIT_CODE=$?
fi

if [ $BINARY_TEST_EXIT_CODE -eq 0 ]; then
    apm_echo "${GREEN}[+] Binary test successful${NC}"
else
    apm_echo "${RED}Error: Downloaded binary failed to run${NC}"
    apm_echo "${YELLOW}Exit code: $BINARY_TEST_EXIT_CODE${NC}"
    apm_echo "${YELLOW}Error output:${NC}"
    echo "$BINARY_TEST_OUTPUT"
    echo ""
    
    # Try to provide helpful context
    if echo "$BINARY_TEST_OUTPUT" | grep -q "GLIBC"; then
        apm_echo "${YELLOW}[!] glibc version incompatibility detected${NC}"
        if [ -n "$GLIBC_VERSION" ]; then
            echo "Your system has glibc $GLIBC_VERSION but the binary requires glibc 2.35+"
        fi
        echo ""
    fi
    
    if try_pip_installation; then
        exit 0
    fi
    
    # If pip fallback failed, provide manual instructions
    echo ""
    apm_echo "${BLUE}Manual installation options:${NC}"
    echo ""
    
    print_pip_recovery_guidance
    echo ""
    
    echo "1. Homebrew (macOS/Linux): brew install apm (no tap needed)"
    echo ""
    echo "2. From source:"
    echo "   git clone $GITHUB_URL/$APM_REPO.git"
    echo "   cd apm && uv sync && uv run pip install -e ."
    echo ""
    
    if [ "$PLATFORM" = "linux" ]; then
        apm_echo "${BLUE}Debug information:${NC}"
        echo "Check missing libraries: ldd $TMP_DIR/$EXTRACTED_DIR/$BINARY_NAME"
        echo ""
    fi
    
    echo "Need help? Create an issue at: $GITHUB_URL/$APM_REPO/issues"
    exit 1
fi

# INSTALL_BINARY_CHECK_END

# Resolve before either installation path can create a competing installation.
apm_resolve_install_paths /usr/local/bin/apm /opt/homebrew/bin/apm /usr/local/lib/apm/apm

# Install binary directory structure
apm_echo "${YELLOW}Installing APM CLI to $APM_INSTALL_DIR...${NC}"

# --- APM_LIB_DIR safety validation ---
# Prevent accidental data loss when APM_LIB_DIR is set to a broad/shared path.
# Four guards: absolute path, suffix, blocklist, recognized bundle identity.
# INSTALL_SAFETY_BEGIN
# Extracted for testability; do not remove the begin/end markers.
# Source with INSTALL_OWNERSHIP for the shared bundle identity predicate.
apm_lib_dir_validate() {
    _apm_lib_dir="$1"
    while [ "$_apm_lib_dir" != "/" ] && [ "${_apm_lib_dir%/}" != "$_apm_lib_dir" ]; do
        _apm_lib_dir="${_apm_lib_dir%/}"
    done

    # 1. Absolute-path guard: must be absolute
    case "$_apm_lib_dir" in
        /*) ;;
        *) return 11 ;;
    esac

    # 2. Suffix guard: must end with /apm (for example, /lib/apm)
    case "$_apm_lib_dir" in
        */apm) ;;
        *) return 12 ;;
    esac

    # 3. Blocklist guard: reject known shared/broad parent directories
    #    (resolved to real path where available, to catch symlink bypasses)
    _apm_lib_dir_real="$(readlink -f "$_apm_lib_dir" 2>/dev/null || realpath "$_apm_lib_dir" 2>/dev/null || echo "$_apm_lib_dir")"

    _apm_safe=true
    while IFS= read -r _apm_dir; do
        [ -z "$_apm_dir" ] && continue
        _apm_dir_real="$(readlink -f "$_apm_dir" 2>/dev/null || realpath "$_apm_dir" 2>/dev/null || echo "$_apm_dir")"
        if [ "$_apm_lib_dir_real" = "$_apm_dir_real" ]; then
            _apm_safe=false
            break
        fi
    done <<APM_BLOCKLIST_EOF
$HOME
$HOME/.local
$HOME/.local/share
$HOME/.config
/usr
/usr/local
/opt
/tmp
/
APM_BLOCKLIST_EOF

    if [ "$_apm_safe" != "true" ]; then
        return 13
    fi

    # 4. Reuse discovery's bundle identity before deleting a non-empty directory.
    if [ -d "$_apm_lib_dir" ] && [ "$(ls -A "$_apm_lib_dir" 2>/dev/null)" ]; then
        if ! apm_is_recognized_bundle "$_apm_lib_dir"; then
            return 14
        fi
    fi

    return 0
}

# INSTALL_SAFETY_END -- extracted for testability; do not remove markers.

_rc=0
apm_lib_dir_validate "$APM_LIB_DIR" || _rc=$?
if [ "$_rc" -ne 0 ]; then
    apm_echo "${RED}+--------------------------------------------------------------+${NC}"
    apm_echo "${RED}|  REFUSING: APM_LIB_DIR=\"$APM_LIB_DIR\"${NC}"
    apm_echo "${RED}+--------------------------------------------------------------+${NC}"
    case $_rc in
        11)
            apm_echo "${RED}|  APM_LIB_DIR must be an absolute path.${NC}"
            apm_echo "${RED}|  Relative paths are not accepted for safety.${NC}"
            ;;
        12)
            apm_echo "${RED}|  APM_LIB_DIR must end with /apm.${NC}"
            apm_echo "${RED}|  This prevents accidental deletion of non-APM data.${NC}"
            apm_echo "${RED}|  Example: APM_LIB_DIR=\$HOME/.local/lib/apm${NC}"
            ;;
        13)
            apm_echo "${RED}|  This path is a shared system directory. Installing here${NC}"
            apm_echo "${RED}|  would delete non-APM data.${NC}"
            apm_echo "${RED}|  Use a dedicated APM directory (e.g. /usr/local/lib/apm).${NC}"
            ;;
        14)
            apm_echo "${RED}|  This directory exists but does not appear to be a${NC}"
            apm_echo "${RED}|  previous APM installation. Refusing to delete it.${NC}"
            apm_echo "${RED}|  Inspect this directory with its original owner.${NC}"
            apm_echo "${RED}|  For a fresh install, choose a different empty APM_LIB_DIR.${NC}"
            ;;
    esac
    apm_echo "${RED}+--------------------------------------------------------------+${NC}"
    exit 1
fi

# Check both destinations and the entire old bundle before removing anything.
apm_require_owned_bundle
apm_require_writable_directory "$(dirname "$APM_LIB_DIR")"
apm_require_writable_directory "$APM_INSTALL_DIR"
apm_read_shell_receipt "$APM_LIB_DIR/.apm-shell-setup"

_apm_lib_parent="$(dirname "$APM_LIB_DIR")"
_apm_stage_dir="$(mktemp -d "$_apm_lib_parent/.apm-stage.XXXXXX")" ||
    apm_install_error "Cannot create a staging directory in $_apm_lib_parent."
_apm_backup_dir=""
_apm_had_old_bundle=""
if [ -d "$APM_LIB_DIR" ]; then
    _apm_had_old_bundle=1
fi

if ! cp -r "$TMP_DIR/$EXTRACTED_DIR"/* "$_apm_stage_dir/" ||
    ! touch "$_apm_stage_dir/.apm-installed"; then
    rm -rf "$_apm_stage_dir"
    apm_install_error "Could not stage the downloaded APM bundle. Existing installation was left unchanged."
fi

if [ -n "$_apm_had_old_bundle" ] &&
    ! apm_preserve_previous_shell_receipt_to "$_apm_stage_dir"; then
    rm -rf "$_apm_stage_dir"
    apm_install_error "Could not preserve the previous shell setup receipt. Existing installation was left unchanged."
fi

if ! INSTALLED_VERSION=$("$_apm_stage_dir/$BINARY_NAME" --version); then
    rm -rf "$_apm_stage_dir"
    apm_install_error "Downloaded APM failed its --version check. Existing installation was left unchanged."
fi

apm_restore_old_bundle_after_failed_swap() {
    if [ -n "$_apm_had_old_bundle" ] && [ -n "$_apm_backup_dir" ] && [ -d "$_apm_backup_dir" ]; then
        rm -rf "$APM_LIB_DIR" || return 1
        mv "$_apm_backup_dir" "$APM_LIB_DIR" || return 1
    elif [ -z "$_apm_had_old_bundle" ]; then
        rm -rf "$APM_LIB_DIR" || return 1
        rm -f "$APM_INSTALL_DIR/$BINARY_NAME" || return 1
    fi
    return 0
}

apm_abort_after_failed_swap() {
    _apm_failure_message="$1"
    if apm_restore_old_bundle_after_failed_swap; then
        apm_install_error "$_apm_failure_message Existing installation was restored."
    fi
    if [ -n "$_apm_had_old_bundle" ] && [ -n "$_apm_backup_dir" ] && [ -d "$_apm_backup_dir" ]; then
        apm_install_error "$_apm_failure_message Could not restore the previous APM bundle from backup $_apm_backup_dir. Move it back to $APM_LIB_DIR after resolving permissions."
    fi
    apm_install_error "$_apm_failure_message Could not roll back the incomplete fresh install completely. Remove $APM_INSTALL_DIR/$BINARY_NAME and $APM_LIB_DIR before retrying."
}

if [ -n "$_apm_had_old_bundle" ]; then
    _apm_backup_dir="$(mktemp -d "$_apm_lib_parent/.apm-backup.XXXXXX")" ||
        { rm -rf "$_apm_stage_dir"; apm_install_error "Cannot create a backup directory in $_apm_lib_parent."; }
    rmdir "$_apm_backup_dir" || { rm -rf "$_apm_stage_dir"; apm_install_error "Cannot prepare bundle backup."; }
    mv "$APM_LIB_DIR" "$_apm_backup_dir" ||
        { rm -rf "$_apm_stage_dir" "$_apm_backup_dir"; apm_install_error "Could not back up existing APM bundle. Existing installation was left unchanged."; }
fi

if ! mv "$_apm_stage_dir" "$APM_LIB_DIR"; then
    rm -rf "$_apm_stage_dir"
    apm_abort_after_failed_swap "Could not activate the staged APM bundle."
fi

_apm_link_tmp="$(apm_mktemp_in_dir "$APM_INSTALL_DIR" "$BINARY_NAME.link")" ||
    apm_abort_after_failed_swap "Cannot create a temporary launcher link."
rm -f "$_apm_link_tmp"
if ! ln -s "$APM_LIB_DIR/$BINARY_NAME" "$_apm_link_tmp" ||
    ! mv -f "$_apm_link_tmp" "$APM_INSTALL_DIR/$BINARY_NAME"; then
    rm -f "$_apm_link_tmp"
    apm_abort_after_failed_swap "Could not update the APM launcher."
fi
if ! INSTALLED_VERSION=$("$APM_INSTALL_DIR/$BINARY_NAME" --version); then
    apm_abort_after_failed_swap "Installed APM at $APM_INSTALL_DIR/$BINARY_NAME failed its --version check."
fi
if [ -n "$_apm_backup_dir" ] && [ -d "$_apm_backup_dir" ]; then
    rm -rf "$_apm_backup_dir"
fi
_apm_on_path="$(command -v apm || true)"
_apm_run_hint="apm"
if [ -n "${APM_SELF_UPDATE_SOURCE:-}" ]; then
    apm_echo "${GREEN}[+] APM installed successfully!${NC}"
    apm_echo "${BLUE}Version: $INSTALLED_VERSION${NC}"
    apm_echo "${BLUE}Location: $APM_INSTALL_DIR/$BINARY_NAME -> $APM_LIB_DIR/$BINARY_NAME${NC}"
    echo "Self-update leaves existing shell PATH setup unchanged."
elif [ -n "$_apm_on_path" ] &&
    [ "$(apm_real_path "$_apm_on_path")" = "$(apm_real_path "$APM_LIB_DIR/$BINARY_NAME")" ]; then
    apm_echo "${GREEN}[+] APM installed successfully!${NC}"
    apm_echo "${BLUE}Version: $INSTALLED_VERSION${NC}"
    apm_echo "${BLUE}Location: $APM_INSTALL_DIR/$BINARY_NAME -> $APM_LIB_DIR/$BINARY_NAME${NC}"
    apm_configure_native_shell_path
else
    apm_echo "${YELLOW}[!] APM installed but not found in PATH${NC}"
    apm_configure_native_shell_path
fi

echo ""
apm_echo "${GREEN}Installation complete!${NC}"
echo ""
apm_echo "${BLUE}Quick start:${NC}"
printf '  %s init my-app          # Create a new APM project\n' "$_apm_run_hint"
printf '  cd my-app && %s install # Install dependencies\n' "$_apm_run_hint"
printf '  %s run                  # Run your first prompt\n' "$_apm_run_hint"
echo ""
apm_echo "${BLUE}Documentation:${NC} $GITHUB_URL/$APM_REPO"
apm_echo "${BLUE}Need help?${NC} Create an issue at $GITHUB_URL/$APM_REPO/issues"
