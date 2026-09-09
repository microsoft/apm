# Source the real installer and stop before staging/download/install.
curl() { "$TEST_PYTHON" "$TEST_FIXTURES/curl.py" "$@"; }
uname() {
    case "$1" in
        -s) printf '%s\n' "$TEST_OS" ;;
        -m) printf '%s\n' x86_64 ;;
        *) return 98 ;;
    esac
}
ldd() { printf '%s\n' 'ldd (GNU libc) 2.39'; }
mktemp() { printf '%s\n' 'METADATA_CHECKPOINT' >&2; return 97; }
deny() { printf '%s\n' 'Unexpected side effect' >&2; return 98; }
sudo() { deny; }
tar() { deny; }
mv() { deny; }
cp() { deny; }
rm() { deny; }
chmod() { deny; }
mkdir() { deny; }
pip() { deny; }
pip3() { deny; }
apm() { deny; }
TEST_HISTORICAL_APM="${TEST_HISTORICAL_APM:-$HOME/historical/bin/apm}"
TEST_INSTALLER_COPY="$HOME/public-release-metadata-install.sh"
sed 's#apm_resolve_install_paths /usr/local/bin/apm /opt/homebrew/bin/apm /usr/local/lib/apm/apm#apm_resolve_install_paths "$TEST_HISTORICAL_APM"#g' \
    "$TEST_ROOT/install.sh" >"$TEST_INSTALLER_COPY"
. "$TEST_INSTALLER_COPY"
