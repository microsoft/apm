#!/bin/sh
set -e

# VERSION is sourced from devcontainer-feature.json options (uppercased option id)
VERSION="${VERSION-"latest"}"

case "$VERSION" in
    latest) ;;
    *)
        if ! printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
            echo "ERROR: VERSION must be 'latest' or a semver string (e.g. 1.2.3), got: '${VERSION}'"
            exit 1
        fi
        ;;
esac

if [ "$(id -u)" -ne 0 ]; then
    echo 'ERROR: install.sh must run as root. Add "USER root" to your Dockerfile before this feature.'
    exit 1
fi

# -- Install uv (idempotent — skip if already on PATH) ------------------------
if command -v uv >/dev/null 2>&1; then
    echo "uv already installed at $(command -v uv) — skipping"
else
    # curl is only needed to fetch the uv installer
    if ! command -v curl >/dev/null 2>&1; then
        echo "curl not found — installing..."
        if command -v apt-get >/dev/null 2>&1; then
            apt-get update -y -qq
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl
        elif command -v apk >/dev/null 2>&1; then
            apk add --no-cache curl
        elif command -v dnf >/dev/null 2>&1; then
            dnf install -y curl
        else
            echo "ERROR: curl is not installed and the package manager is not recognised."
            exit 1
        fi
    fi
    echo "Installing uv..."
    _uv_tmp="$(mktemp /tmp/uv_install.XXXXXX)"
    trap 'rm -f "$_uv_tmp"' EXIT INT TERM
    curl -LsSf https://astral.sh/uv/install.sh > "$_uv_tmp"
    UV_INSTALL_DIR=/usr/local/bin sh "$_uv_tmp"
fi

echo "Installing APM CLI (version: ${VERSION})..."

# -- Ensure Python 3.10+ is available -----------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 not found — installing via system package manager..."
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -y -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-pip git
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache python3 py3-pip git
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y python3 python3-pip git
    else
        echo "ERROR: Python 3 is not installed and the package manager is not recognised."
        echo "Please use a base image that includes Python 3.10+, or install it manually."
        exit 1
    fi
fi

# -- Ensure git is available (apm uses GitPython at startup) ------------------
if ! command -v git >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -y -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache git
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y git
    else
        echo "ERROR: git is not installed and the package manager is not recognised."
        echo "Please use a base image that includes git, or install it manually."
        exit 1
    fi
fi

# Validate Python version meets apm-cli requirement (>=3.10)
PYTHON_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
PYTHON_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 10 ]; }; then
    PYTHON_VER=$(python3 -c "import sys; print('.'.join(map(str, sys.version_info[:3])))")
    echo "ERROR: apm-cli requires Python 3.10+, found Python ${PYTHON_VER}."
    echo "Use a base image with Python 3.10+ (e.g. ubuntu:22.04) or include the Python devcontainer feature."
    exit 1
fi

# -- Locate pip ----------------------------------------------------------------
PIP_CMD=""
if command -v pip3 >/dev/null 2>&1; then
    PIP_CMD="pip3"
elif command -v pip >/dev/null 2>&1; then
    PIP_CMD="pip"
else
    python3 -m ensurepip --upgrade 2>/dev/null || true
    if command -v pip3 >/dev/null 2>&1; then
        PIP_CMD="pip3"
    elif command -v pip >/dev/null 2>&1; then
        PIP_CMD="pip"
    else
        echo "ERROR: pip is not available and could not be bootstrapped."
        exit 1
    fi
fi

# -- Build pip package spec ---------------------------------------------------
if [ "$VERSION" = "latest" ]; then
    PKG_SPEC="apm-cli"
else
    PKG_SPEC="apm-cli==${VERSION}"
fi

# -- Install ------------------------------------------------------------------
# Ubuntu 24.04+ enforces PEP 668 ("externally managed environment") and rejects
# plain `pip install`. Detect the specific error and retry with the flag.
install_apm() {
    _install_out=$($PIP_CMD install "$PKG_SPEC" 2>&1) && { echo "$_install_out"; return 0; }
    echo "$_install_out"
    if echo "$_install_out" | grep -q "externally-managed-environment"; then
        echo "Retrying with --break-system-packages (PEP 668 distro)..."
        $PIP_CMD install --break-system-packages "$PKG_SPEC"
    else
        return 1
    fi
}

install_apm

# -- Ensure bash is present (Alpine ships only ash; devcontainer test scripts require bash) --
if command -v apk >/dev/null 2>&1 && ! command -v bash >/dev/null 2>&1; then
    apk add --no-cache bash
fi

# -- Verify -------------------------------------------------------------------
if command -v apm >/dev/null 2>&1; then
    echo "[+] APM $(apm --version) installed at $(command -v apm)"
else
    echo "WARNING: apm was installed but is not in PATH."
    echo "Ensure the pip bin directory is in PATH (usually /usr/local/bin or ~/.local/bin)."
fi
