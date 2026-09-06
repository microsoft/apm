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
. "$TEST_ROOT/install.sh"
