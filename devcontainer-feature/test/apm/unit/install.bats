#!/usr/bin/env bats
# Unit tests for devcontainer-feature/src/apm/install.sh
# PATH is fully isolated to STUB_BIN — no network, no real packages, no Docker.

load "../../test_helper/bats-support/load"
load "../../test_helper/bats-assert/load"

INSTALL_SH="$(cd "$(dirname "$BATS_TEST_FILENAME")/../../../src/apm" && pwd)/install.sh"

# ── Helpers ──────────────────────────────────────────────────────────────────

setup() {
    STUB_BIN="$BATS_TEST_TMPDIR/bin"
    /bin/mkdir -p "$STUB_BIN"
    export STUB_BIN

    # Delegate stubs for utilities install.sh needs with real behaviour.
    # Resolved against the real PATH before we lock it down.
    local real_grep real_sh real_mktemp
    real_grep="$(PATH=/usr/bin:/bin command -v grep)"
    real_sh="$(PATH=/usr/bin:/bin command -v sh)"
    real_mktemp="$(PATH=/usr/bin:/bin command -v mktemp)"
    printf '#!/bin/sh\nexec "%s" "$@"\n' "$real_grep"   > "$STUB_BIN/grep"
    printf '#!/bin/sh\nexec "%s" "$@"\n' "$real_sh"     > "$STUB_BIN/sh"
    printf '#!/bin/sh\nexec "%s" "$@"\n' "$real_mktemp" > "$STUB_BIN/mktemp"
    /bin/chmod +x "$STUB_BIN/grep" "$STUB_BIN/sh" "$STUB_BIN/mktemp"

    # Pre-stage python3 stub content; package-manager stubs cp this into place.
    /bin/cat > "$STUB_BIN/_python3_stub" <<'EOF'
#!/bin/sh
case "$*" in
    *version_info.minor*) echo "12"    ;;
    *version_info.major*) echo "3"     ;;
    *version_info*3*)     echo "3.12.0" ;;
    *)                    exit 0       ;;
esac
EOF
    /bin/chmod +x "$STUB_BIN/_python3_stub"
    # NOTE: PATH is NOT locked here — test code needs rm, cat, etc.
    # We'll lock it per-test using run_with_stubs()
}

# Helper: runs sh with PATH locked to STUB_BIN + /bin (for sh, core utilities)
# STUB_BIN is first so stubs shadow any real system commands.
run_with_stubs() {
    PATH="$STUB_BIN:/bin" run sh "$INSTALL_SH" "$@"
}

# make_stub <name> <exit_code> [output_text]
make_stub() {
    local name="$1" rc="$2" out="${3:-}"
    {
        printf '#!/bin/sh\n'
        [ -n "$out" ] && printf 'echo "%s"\n' "$out"
        printf 'exit %d\n' "$rc"
    } > "$STUB_BIN/$name"
    /bin/chmod +x "$STUB_BIN/$name"
}

# Copies the pre-staged python3 stub into STUB_BIN.
make_python3_stub() {
    /bin/cp "$STUB_BIN/_python3_stub" "$STUB_BIN/python3"
}

# make_old_python3_stub <major> <minor>  — simulates an older Python.
make_old_python3_stub() {
    local major="${1:-3}" minor="${2:-8}"
    /bin/cat > "$STUB_BIN/python3" <<EOF
#!/bin/sh
case "\$*" in
    *version_info.minor*) echo "$minor"          ;;
    *version_info.major*) echo "$major"          ;;
    *version_info*3*)     echo "$major.$minor.0" ;;
    *)                    exit 0                 ;;
esac
EOF
    /bin/chmod +x "$STUB_BIN/python3"
}

# make_pkg_mgr_stub <cmd>  — creates a package-manager stub that side-effects
# a python3 stub (simulating a successful install of python3) and records args.
make_pkg_mgr_stub() {
    local cmd="$1"
    /bin/cat > "$STUB_BIN/$cmd" <<EOF
#!/bin/sh
echo "\$@" >> "${STUB_BIN}/_${cmd}_args"
/bin/cp "${STUB_BIN}/_python3_stub" "${STUB_BIN}/python3"
exit 0
EOF
    /bin/chmod +x "$STUB_BIN/$cmd"
}

# Full happy-path environment: root, all tools present, install succeeds.
setup_happy_path() {
    make_stub    id   0 "0"
    make_stub    uv   0 "0.4.0"
    make_python3_stub
    make_stub    git  0
    make_stub    pip3 0 "Successfully installed apm-cli"
    make_stub    apm  0 "0.9.0"
}

# ── Root check ────────────────────────────────────────────────────────────────

@test "exits 1 with clear message when not run as root" {
    make_stub id 0 "1"   # id -u → 1 (non-root)

    run_with_stubs

    assert_failure
    assert_output --partial "must run as root"
}

@test "continues past root check when run as root" {
    setup_happy_path

    run_with_stubs

    assert_success
}

# ── uv install (idempotency) ──────────────────────────────────────────────────

@test "skips curl when uv is already on PATH" {
    setup_happy_path
    # curl is intentionally absent — any curl call would crash the script

    run_with_stubs

    assert_success
}

@test "installs uv via curl when uv is not on PATH" {
    setup_happy_path
    rm -f "$STUB_BIN/uv"
    make_stub curl 0   # curl outputs nothing; sh receives empty input and exits 0

    run_with_stubs

    assert_success
}

# ── Python 3 install ──────────────────────────────────────────────────────────

@test "skips Python install when python3 is already on PATH" {
    setup_happy_path

    run_with_stubs

    assert_success
}

@test "installs python3 via apt-get when missing" {
    setup_happy_path
    rm -f "$STUB_BIN/python3"
    make_pkg_mgr_stub apt-get

    run_with_stubs

    assert_success
}

@test "installs python3 via apk when apt-get is absent" {
    setup_happy_path
    rm -f "$STUB_BIN/python3"
    # No apt-get stub — falls through to apk
    make_pkg_mgr_stub apk

    run_with_stubs

    assert_success
    assert_output --partial "Python 3 not found"
}

@test "installs python3 via dnf when apt-get and apk are absent" {
    setup_happy_path
    rm -f "$STUB_BIN/python3"
    # No apt-get or apk stubs — falls through to dnf
    make_pkg_mgr_stub dnf

    run_with_stubs

    assert_success
    assert_output --partial "Python 3 not found"
}

@test "exits 1 with clear message when no supported package manager is found" {
    setup_happy_path
    rm -f "$STUB_BIN/python3"
    # No apt-get, apk, or dnf stubs

    run_with_stubs

    assert_failure
    assert_output --partial "package manager is not recognised"
}

# ── git install ───────────────────────────────────────────────────────────────

@test "installs git via apt-get when git is missing" {
    setup_happy_path
    rm -f "$STUB_BIN/git"
    make_stub apt-get 0

    run_with_stubs

    assert_success
}

@test "installs git via apk when apt-get is absent" {
    setup_happy_path
    rm -f "$STUB_BIN/git"
    # No apt-get stub — falls through to apk
    /bin/cat > "$STUB_BIN/apk" <<EOF
#!/bin/sh
echo "\$@" >> "${STUB_BIN}/_apk_args"
exit 0
EOF
    /bin/chmod +x "$STUB_BIN/apk"

    run_with_stubs

    assert_success
    [ -f "$STUB_BIN/_apk_args" ]
}

@test "installs git via dnf when apt-get and apk are absent" {
    setup_happy_path
    rm -f "$STUB_BIN/git"
    # No apt-get or apk stubs — falls through to dnf
    /bin/cat > "$STUB_BIN/dnf" <<EOF
#!/bin/sh
echo "\$@" >> "${STUB_BIN}/_dnf_args"
exit 0
EOF
    /bin/chmod +x "$STUB_BIN/dnf"

    run_with_stubs

    assert_success
    [ -f "$STUB_BIN/_dnf_args" ]
}

@test "exits 1 with clear message when git is missing and no package manager is available" {
    setup_happy_path
    rm -f "$STUB_BIN/git"
    # No apt-get, apk, or dnf stubs

    run_with_stubs

    assert_failure
    assert_output --partial "git"
}

# ── Python version guard ───────────────────────────────────────────────────────

@test "exits 1 with clear message when Python is 3.8 (too old)" {
    setup_happy_path
    make_old_python3_stub 3 8

    run_with_stubs

    assert_failure
    assert_output --partial "requires Python 3.10+"
    assert_output --partial "3.8"
}

@test "exits 1 with clear message when Python is 2.7" {
    setup_happy_path
    make_old_python3_stub 2 7

    run_with_stubs

    assert_failure
    assert_output --partial "requires Python 3.10+"
}

@test "continues when Python is exactly 3.10 (minimum boundary)" {
    setup_happy_path
    /bin/cat > "$STUB_BIN/python3" <<'EOF'
#!/bin/sh
case "$*" in
    *version_info.minor*) echo "10"    ;;
    *version_info.major*) echo "3"     ;;
    *version_info*3*)     echo "3.10.0" ;;
    *)                    exit 0       ;;
esac
EOF
    /bin/chmod +x "$STUB_BIN/python3"

    run_with_stubs

    assert_success
}

# ── pip discovery ─────────────────────────────────────────────────────────────

@test "uses pip3 when pip3 is on PATH" {
    setup_happy_path

    run_with_stubs

    assert_success
}

@test "falls back to pip when pip3 is absent" {
    setup_happy_path
    rm -f "$STUB_BIN/pip3"
    make_stub pip 0 "Successfully installed apm-cli"

    run_with_stubs

    assert_success
}

@test "bootstraps pip via ensurepip when neither pip3 nor pip is found" {
    setup_happy_path
    rm -f "$STUB_BIN/pip3"

    # python3 -m ensurepip side-effects a pip3 stub into STUB_BIN
    /bin/cat > "$STUB_BIN/python3" <<EOF
#!/bin/sh
case "\$*" in
    *version_info.minor*) echo "12" ;;
    *version_info.major*) echo "3"  ;;
    *ensurepip*)
        printf '#!/bin/sh\necho "Successfully installed apm-cli"\nexit 0\n' > "${STUB_BIN}/pip3"
        /bin/chmod +x "${STUB_BIN}/pip3"
        exit 0 ;;
    *) exit 0 ;;
esac
EOF
    /bin/chmod +x "$STUB_BIN/python3"

    run_with_stubs

    assert_success
}

@test "exits 1 when pip cannot be bootstrapped" {
    setup_happy_path
    rm -f "$STUB_BIN/pip3"
    # python3 ensurepip exits 0 but creates nothing (default stub behaviour)

    run_with_stubs

    assert_failure
    assert_output --partial "pip is not available"
}

# ── Package spec ──────────────────────────────────────────────────────────────

@test "installs plain apm-cli when VERSION is latest" {
    setup_happy_path
    /bin/cat > "$STUB_BIN/pip3" <<'EOF'
#!/bin/sh
echo "ARGS:$*"
exit 0
EOF
    /bin/chmod +x "$STUB_BIN/pip3"

    VERSION=latest PATH="$STUB_BIN:/bin" run sh "$INSTALL_SH"

    assert_success
    assert_output --partial "ARGS:install apm-cli"
    refute_output --partial "apm-cli=="
}

@test "pins version when VERSION is set to a semver string" {
    setup_happy_path
    /bin/cat > "$STUB_BIN/pip3" <<'EOF'
#!/bin/sh
echo "ARGS:$*"
exit 0
EOF
    /bin/chmod +x "$STUB_BIN/pip3"

    VERSION=0.8.11 PATH="$STUB_BIN:/bin" run sh "$INSTALL_SH"

    assert_success
    assert_output --partial "ARGS:install apm-cli==0.8.11"
}

# ── PEP 668 retry ─────────────────────────────────────────────────────────────

@test "retries install with --break-system-packages on PEP 668 error" {
    setup_happy_path
    /bin/cat > "$STUB_BIN/pip3" <<EOF
#!/bin/sh
case "\$*" in
    *--break-system-packages*) echo "Successfully installed"; exit 0 ;;
    *) echo "ERROR: externally-managed-environment"; exit 1            ;;
esac
EOF
    /bin/chmod +x "$STUB_BIN/pip3"

    run_with_stubs

    assert_success
}

@test "exits 1 on non-PEP-668 pip error without retrying" {
    setup_happy_path
    /bin/cat > "$STUB_BIN/pip3" <<'EOF'
#!/bin/sh
echo "ERROR: Could not find a version that satisfies the requirement"
exit 1
EOF
    /bin/chmod +x "$STUB_BIN/pip3"

    VERSION=99.99.99 PATH="$STUB_BIN:/bin" run sh "$INSTALL_SH"

    assert_failure
    refute_output --partial "--break-system-packages"
}

# ── Post-install verification ─────────────────────────────────────────────────

@test "prints installed version and path when apm is on PATH" {
    setup_happy_path

    run_with_stubs

    assert_success
}

@test "prints warning (not failure) when apm is not on PATH after install" {
    setup_happy_path
    rm -f "$STUB_BIN/apm"

    run_with_stubs

    assert_success
    assert_output --partial "WARNING: apm was installed but is not in PATH"
}

# ── uv install failure ────────────────────────────────────────────────────────

@test "exits non-zero when curl fails during uv install" {
    setup_happy_path
    rm -f "$STUB_BIN/uv"
    make_stub curl 1

    run_with_stubs

    assert_failure
}

# ── Combined python3+git apt-get install ──────────────────────────────────────

@test "apt-get install includes git alongside python3" {
    setup_happy_path
    rm -f "$STUB_BIN/python3"
    make_pkg_mgr_stub apt-get

    run_with_stubs

    assert_success
    grep -q 'git' "$STUB_BIN/_apt-get_args"
}

# ── UV_INSTALL_DIR ────────────────────────────────────────────────────────────

@test "passes UV_INSTALL_DIR=/usr/local/bin to the uv installer" {
    setup_happy_path
    rm -f "$STUB_BIN/uv"
    # curl emits a script that echoes the env var; sh executes it
    /bin/cat > "$STUB_BIN/curl" <<'EOF'
#!/bin/sh
echo 'echo "UV_INSTALL_DIR=$UV_INSTALL_DIR"'
exit 0
EOF
    /bin/chmod +x "$STUB_BIN/curl"

    run_with_stubs

    assert_success
    assert_output --partial "UV_INSTALL_DIR=/usr/local/bin"
}

# ── VERSION default ───────────────────────────────────────────────────────────

@test "defaults to latest when VERSION is unset" {
    setup_happy_path
    /bin/cat > "$STUB_BIN/pip3" <<'EOF'
#!/bin/sh
echo "ARGS:$*"
exit 0
EOF
    /bin/chmod +x "$STUB_BIN/pip3"

    unset VERSION
    PATH="$STUB_BIN:/bin" run sh "$INSTALL_SH"

    assert_success
    assert_output --partial "ARGS:install apm-cli"
    refute_output --partial "apm-cli=="
}

# ── Python 3.9 boundary ───────────────────────────────────────────────────────

@test "exits 1 when Python is 3.9 (one below minimum)" {
    setup_happy_path
    make_old_python3_stub 3 9

    run_with_stubs

    assert_failure
    assert_output --partial "requires Python 3.10+"
    assert_output --partial "3.9"
}

# ── VERSION validation ────────────────────────────────────────────────────────

@test "exits 1 with clear message when VERSION is empty string" {
    setup_happy_path
    VERSION="" PATH="$STUB_BIN:/bin" run sh "$INSTALL_SH"
    assert_failure
    assert_output --partial "VERSION"
}

@test "exits 1 with clear message when VERSION is not latest or semver" {
    setup_happy_path
    VERSION=abc PATH="$STUB_BIN:/bin" run sh "$INSTALL_SH"
    assert_failure
    assert_output --partial "VERSION"
}

@test "exits 1 when VERSION has only two version components" {
    setup_happy_path
    VERSION=1.2 PATH="$STUB_BIN:/bin" run sh "$INSTALL_SH"
    assert_failure
    assert_output --partial "VERSION"
}

@test "continues when VERSION is a valid three-part semver string" {
    setup_happy_path
    VERSION=1.2.3 PATH="$STUB_BIN:/bin" run sh "$INSTALL_SH"
    assert_success
}

@test "exits 1 when VERSION has four version components" {
    setup_happy_path
    VERSION=1.2.3.4 PATH="$STUB_BIN:/bin" run sh "$INSTALL_SH"
    assert_failure
    assert_output --partial "VERSION"
}
