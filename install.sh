#!/bin/bash
set -e

# APM CLI Installer Script
# Usage: curl -sSL https://aka.ms/apm-unix | sh
# Specific version:     curl -sSL https://aka.ms/apm-unix | sh -s -- @v1.2.3   (or VERSION=v1.2.3)
# Custom install dir:   curl -sSL https://aka.ms/apm-unix | APM_INSTALL_DIR=$HOME/.local/bin sh
# Custom repository:    APM_REPO=ghe-org/apm sh install.sh
# GitHub Enterprise:    GITHUB_URL=https://gh.corp.com sh install.sh
# For private repositories, use with authentication:
#   curl -sSL -H "Authorization: token $GITHUB_APM_PAT" \
#     https://raw.githubusercontent.com/microsoft/apm/main/install.sh | \
#     GITHUB_APM_PAT=$GITHUB_APM_PAT sh

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration (all overridable via environment variables)
APM_REPO="${APM_REPO:-microsoft/apm}"
APM_INSTALL_DIR="${APM_INSTALL_DIR:-/usr/local/bin}"
BINARY_NAME="apm"
GITHUB_URL="${GITHUB_URL:-https://github.com}"

# Banner
echo -e "${BLUE}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                         APM Installer                        ║"
echo "║              The NPM for AI-Native Development               ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

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
        echo -e "${RED}Error: Unsupported architecture: $ARCH${NC}"
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
        echo -e "${RED}Error: Unsupported operating system: $OS${NC}"
        echo "Supported platforms: macOS (Darwin), Linux"
        exit 1
        ;;
esac

echo -e "${BLUE}Detected platform: $PLATFORM-$ARCH${NC}"
echo -e "${BLUE}Target binary: $DOWNLOAD_BINARY${NC}"

# Parse version: @v1.2.3 as arg, or VERSION env var
# Usage: sh install.sh @v1.2.3  or  VERSION=v1.2.3 sh install.sh
if [ -z "$VERSION" ] && [ -n "$1" ]; then
    VERSION="${1#@}"
fi

# Function to check Python availability and version
check_python_requirements() {
    # Check if Python is available
    if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
        return 1  # Python not available
    fi
    
    # Get Python command
    PYTHON_CMD="python3"
    if ! command -v python3 >/dev/null 2>&1; then
        PYTHON_CMD="python"
    fi
    
    # Check Python version (need 3.9+)
    PYTHON_VERSION=$($PYTHON_CMD -c 'import sys; print(".".join(map(str, sys.version_info[:2])))' 2>/dev/null)
    if [ -z "$PYTHON_VERSION" ]; then
        return 1
    fi
    
    # Compare version (need >= 3.9)
    REQUIRED_VERSION="3.9"
    if [ "$(printf '%s\n' "$REQUIRED_VERSION" "$PYTHON_VERSION" | sort -V | head -n1)" = "$REQUIRED_VERSION" ]; then
        return 0  # Python version is sufficient
    else
        return 1  # Python version too old
    fi
}

# Function to attempt pip installation
try_pip_installation() {
    echo -e "${BLUE}Attempting installation via pip...${NC}"
    
    # Determine pip command
    PIP_CMD=""
    if command -v pip3 >/dev/null 2>&1; then
        PIP_CMD="pip3"
    elif command -v pip >/dev/null 2>&1; then
        PIP_CMD="pip"
    else
        echo -e "${RED}Error: pip is not available${NC}"
        return 1
    fi
    
    # Try to install
    if $PIP_CMD install --user apm-cli; then
        echo -e "${GREEN}[+] APM installed successfully via pip!${NC}"
        
        # Check if apm is now available
        if command -v apm >/dev/null 2>&1; then
            INSTALLED_VERSION=$(apm --version 2>/dev/null || echo "unknown")
            echo -e "${BLUE}Version: $INSTALLED_VERSION${NC}"
            echo -e "${BLUE}Location: $(which apm)${NC}"
        else
            echo -e "${YELLOW}[!] APM installed but not found in PATH${NC}"
            echo "You may need to add ~/.local/bin to your PATH:"
            echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        fi
        
        echo ""
        echo -e "${GREEN}Installation complete!${NC}"
        echo ""
        echo -e "${BLUE}Quick start:${NC}"
        echo "  apm init my-app          # Create a new APM project"
        echo "  cd my-app && apm install # Install dependencies"
        echo "  apm run                  # Run your first prompt"
        echo ""
        echo -e "${BLUE}Documentation:${NC} $GITHUB_URL/$APM_REPO"
        return 0
    else
        echo -e "${RED}Error: pip installation failed${NC}"
        return 1
    fi
}

# Early glibc compatibility check for Linux
if [ "$PLATFORM" = "linux" ]; then
    # Get glibc version
    GLIBC_VERSION=$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
    REQUIRED_GLIBC="2.35"
    
    if [ -n "$GLIBC_VERSION" ]; then
        # Compare versions
        if [ "$(printf '%s\n' "$REQUIRED_GLIBC" "$GLIBC_VERSION" | sort -V | head -n1)" != "$REQUIRED_GLIBC" ]; then
            echo -e "${YELLOW}[!] Compatibility Issue Detected${NC}"
            echo -e "${YELLOW}Your glibc version: $GLIBC_VERSION${NC}"
            echo -e "${YELLOW}Required version: $REQUIRED_GLIBC or newer${NC}"
            echo ""
            echo "The prebuilt binary will not work on your system."
            echo ""
            
            # Check if Python/pip are available
            if check_python_requirements; then
                echo -e "${BLUE}Python 3.9+ detected. Installing via pip instead...${NC}"
                echo ""
                if try_pip_installation; then
                    exit 0
                fi
            else
                echo -e "${RED}Python 3.9+ is not available on this system.${NC}"
                echo ""
                echo "To install APM, you need either:"
                echo "  1. Python 3.9+ and pip: pip install --user apm-cli"
                echo "  2. A system with glibc 2.35+ to use the prebuilt binary"
                echo "  3. Build from source: git clone $GITHUB_URL/$APM_REPO.git && cd apm && uv sync && uv run pip install -e ."
                echo ""
                echo "To install Python 3.9+:"
                echo "  Ubuntu/Debian: sudo apt-get update && sudo apt-get install python3 python3-pip"
                echo "  CentOS/RHEL: sudo yum install python3 python3-pip"
                echo "  Alpine: apk add python3 py3-pip"
                exit 1
            fi
        fi
    fi
fi

# Detect if running in a container and check compatibility
if [ -f "/.dockerenv" ] || [ -f "/run/.containerenv" ] || grep -q "/docker/" /proc/1/cgroup 2>/dev/null; then
    echo -e "${YELLOW}[!] Container/Dev Container environment detected${NC}"
    echo -e "${YELLOW}Note: PyInstaller binaries may have compatibility issues in containers.${NC}"
    echo -e "${YELLOW}If installation fails, consider using: pip install --user apm-cli${NC}"
    echo ""
fi

# Check if we have permission to install to the configured directory.
# Only warn if the dir already exists; mkdir -p later handles non-existent dirs.
if [ -e "$APM_INSTALL_DIR" ] && [ ! -w "$APM_INSTALL_DIR" ]; then
    echo -e "${YELLOW}Note: Will need sudo permissions to install to $APM_INSTALL_DIR${NC}"
fi

# Resolve auth token (needed for both API and download paths)
if [ -n "$GITHUB_APM_PAT" ]; then
    AUTH_HEADER_VALUE="$GITHUB_APM_PAT"
elif [ -n "$GITHUB_TOKEN" ]; then
    AUTH_HEADER_VALUE="$GITHUB_TOKEN"
fi

# When VERSION is provided, skip GitHub API and compute download URL directly
if [ -n "$VERSION" ]; then
    TAG_NAME="$VERSION"
    DOWNLOAD_URL="$GITHUB_URL/$APM_REPO/releases/download/$TAG_NAME/$DOWNLOAD_BINARY"
    echo -e "${GREEN}Version: $TAG_NAME${NC}"
    echo -e "${BLUE}Download URL: $DOWNLOAD_URL${NC}"
fi

if [ -z "$TAG_NAME" ]; then
# Get latest release info
echo -e "${YELLOW}Fetching latest release information...${NC}"

# Try to fetch release info without authentication first (for public repos)
LATEST_RELEASE=$(curl -s "https://api.github.com/repos/$APM_REPO/releases/latest")
CURL_EXIT_CODE=$?

# Check if the response indicates authentication is required (private repo)
# Only try authentication if curl failed OR we got a "Not Found" message OR response is empty
if [ $CURL_EXIT_CODE -ne 0 ] || [ -z "$LATEST_RELEASE" ] || echo "$LATEST_RELEASE" | grep -q '"message".*"Not Found"'; then
    echo -e "${BLUE}Repository appears to be private, trying with authentication...${NC}"

    # Check if we have GitHub token for private repo access
    AUTH_HEADER_VALUE=""
    if [ -n "$GITHUB_APM_PAT" ]; then
        echo -e "${BLUE}Using GITHUB_APM_PAT for private repository access${NC}"
        AUTH_HEADER_VALUE="$GITHUB_APM_PAT"
    elif [ -n "$GITHUB_TOKEN" ]; then
        echo -e "${BLUE}Using GITHUB_TOKEN for private repository access${NC}"
        AUTH_HEADER_VALUE="$GITHUB_TOKEN"
    else
        echo -e "${RED}Error: Repository is private but no authentication token found${NC}"
        echo "Please set GITHUB_APM_PAT or GITHUB_TOKEN environment variable:"
        echo "  export GITHUB_APM_PAT=your_token_here"
        echo "  curl -sSL -H \"Authorization: token \$GITHUB_APM_PAT\" \\"
        echo "    https://raw.githubusercontent.com/microsoft/apm/main/install.sh | \\"
        echo "    GITHUB_APM_PAT=\$GITHUB_APM_PAT sh"
        exit 1
    fi

    # Retry with authentication
    LATEST_RELEASE=$(curl -s -H "Authorization: token $AUTH_HEADER_VALUE" "https://api.github.com/repos/$APM_REPO/releases/latest")
    CURL_EXIT_CODE=$?
fi

if [ $CURL_EXIT_CODE -ne 0 ] || [ -z "$LATEST_RELEASE" ]; then
    echo -e "${RED}Error: Failed to fetch release information${NC}"
    echo "Please check your internet connection and try again."
    exit 1
fi

# Check if we got a valid response (should contain tag_name)
if ! echo "$LATEST_RELEASE" | grep -q '"tag_name":'; then
    echo -e "${RED}Error: Invalid API response received${NC}"

    # Check if the response contains an error message
    if echo "$LATEST_RELEASE" | grep -q '"message"'; then
        echo -e "${RED}GitHub API Error:${NC}"
        echo "$LATEST_RELEASE" | grep '"message"' | sed 's/.*"message": *"\([^"]*\)".*/\1/'
    fi
    exit 1
fi

# Extract tag name and download URLs
# Use grep -o to extract just the matching portion (handles single-line JSON)
TAG_NAME=$(echo "$LATEST_RELEASE" | grep -o '"tag_name": *"[^"]*"' | awk -F'"' '{print $4}')
DOWNLOAD_URL="$GITHUB_URL/$APM_REPO/releases/download/$TAG_NAME/$DOWNLOAD_BINARY"

# Extract API asset URL for private repository downloads
ASSET_URL=$(echo "$LATEST_RELEASE" | grep -B 3 "\"name\": \"$DOWNLOAD_BINARY\"" | grep -o '"url": *"[^"]*"' | awk -F'"' '{print $4}')

if [ -z "$TAG_NAME" ]; then
    echo -e "${RED}Error: Could not determine latest release version${NC}"
    echo -e "${BLUE}Debug: Full API response:${NC}" >&2
    echo "$LATEST_RELEASE" >&2
    echo ""
    echo "This could mean:"
    echo "  1. No releases found in the repository"
    echo "  2. API response format is unexpected"
    echo "  3. Token doesn't have sufficient permissions"
    echo "  4. Repository doesn't exist or is inaccessible"
    exit 1
fi

echo -e "${GREEN}Latest version: $TAG_NAME${NC}"
echo -e "${BLUE}Download URL: $DOWNLOAD_URL${NC}"
fi

# Create temporary directory
TMP_DIR=$(mktemp -d)
trap "rm -rf $TMP_DIR" EXIT

# Download binary
echo -e "${YELLOW}Downloading APM...${NC}"

# Try downloading without authentication first (for public repos)
if curl -L --fail --silent --show-error "$DOWNLOAD_URL" -o "$TMP_DIR/$DOWNLOAD_BINARY"; then
    echo -e "${GREEN}[+] Download successful${NC}"
else
    # If unauthenticated download fails, try with authentication if available
    if [ -n "$AUTH_HEADER_VALUE" ]; then
        echo -e "${BLUE}Download failed, retrying with authentication...${NC}"
        
        # For private repositories, use GitHub API with proper headers
        if [ -n "$ASSET_URL" ]; then
            echo -e "${BLUE}Using GitHub API for private repository access...${NC}"
            if curl -L --fail --silent --show-error \
                -H "Authorization: token $AUTH_HEADER_VALUE" \
                -H "Accept: application/octet-stream" \
                "$ASSET_URL" -o "$TMP_DIR/$DOWNLOAD_BINARY"; then
                echo -e "${GREEN}[+] Download successful via GitHub API${NC}"
            else
                echo -e "${BLUE}GitHub API download failed, trying direct URL with auth...${NC}"
                if curl -L --fail --silent --show-error -H "Authorization: token $AUTH_HEADER_VALUE" "$DOWNLOAD_URL" -o "$TMP_DIR/$DOWNLOAD_BINARY"; then
                    echo -e "${GREEN}[+] Download successful with authentication${NC}"
                else
                    echo -e "${RED}Error: Failed to download APM CLI even with authentication${NC}"
                    echo "Direct URL: $DOWNLOAD_URL"
                    echo "API URL: $ASSET_URL"
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
            echo -e "${BLUE}No API URL available, trying direct URL with auth...${NC}"
            if curl -L --fail --silent --show-error -H "Authorization: token $AUTH_HEADER_VALUE" "$DOWNLOAD_URL" -o "$TMP_DIR/$DOWNLOAD_BINARY"; then
                echo -e "${GREEN}[+] Download successful with authentication${NC}"
            else
                echo -e "${RED}Error: Failed to download APM CLI even with authentication${NC}"
                echo "URL: $DOWNLOAD_URL"
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
        echo -e "${RED}Error: Failed to download APM${NC}"
        echo "URL: $DOWNLOAD_URL"
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

# Extract binary from tar.gz
echo -e "${YELLOW}Extracting binary...${NC}"
if tar -xzf "$TMP_DIR/$DOWNLOAD_BINARY" -C "$TMP_DIR"; then
    echo -e "${GREEN}[+] Extraction successful${NC}"
else
    echo -e "${RED}Error: Failed to extract binary from archive${NC}"
    exit 1
fi

# Make binary executable
chmod +x "$TMP_DIR/$EXTRACTED_DIR/$BINARY_NAME"

# Test the binary
# Use if/else to capture exit code without triggering set -e.
# When glibc is too old the binary exits 255 immediately;
# we must survive that so the pip-fallback path below is reachable.
echo -e "${YELLOW}Testing binary...${NC}"
if BINARY_TEST_OUTPUT=$("$TMP_DIR/$EXTRACTED_DIR/$BINARY_NAME" --version 2>&1); then
    BINARY_TEST_EXIT_CODE=0
else
    BINARY_TEST_EXIT_CODE=$?
fi

if [ $BINARY_TEST_EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}[+] Binary test successful${NC}"
else
    echo -e "${RED}Error: Downloaded binary failed to run${NC}"
    echo -e "${YELLOW}Exit code: $BINARY_TEST_EXIT_CODE${NC}"
    echo -e "${YELLOW}Error output:${NC}"
    echo "$BINARY_TEST_OUTPUT"
    echo ""
    
    # Try to provide helpful context
    if echo "$BINARY_TEST_OUTPUT" | grep -q "GLIBC"; then
        echo -e "${YELLOW}[!] glibc version incompatibility detected${NC}"
        if [ -n "$GLIBC_VERSION" ]; then
            echo "Your system has glibc $GLIBC_VERSION but the binary requires glibc 2.35+"
        fi
        echo ""
    fi
    
    # Attempt automatic fallback to pip
    echo -e "${BLUE}Attempting automatic fallback to pip installation...${NC}"
    echo ""
    
    if check_python_requirements; then
        if try_pip_installation; then
            exit 0
        fi
    fi
    
    # If pip fallback failed, provide manual instructions
    echo ""
    echo -e "${BLUE}Manual installation options:${NC}"
    echo ""
    
    if ! check_python_requirements; then
        echo -e "${YELLOW}Note: Python 3.9+ is not available on your system${NC}"
        echo ""
        echo "Install Python first:"
        echo "  Ubuntu/Debian: sudo apt-get update && sudo apt-get install python3 python3-pip"
        echo "  CentOS/RHEL: sudo yum install python3 python3-pip"
        echo "  Alpine: apk add python3 py3-pip"
        echo "  macOS: brew install python3"
        echo ""
        echo "Then install APM:"
        echo "  pip3 install --user apm-cli"
        echo ""
    else
        echo "1. PyPI (recommended): pip3 install --user apm-cli"
        echo ""
    fi
    
    echo "2. Homebrew (macOS/Linux): brew install microsoft/apm/apm"
    echo ""
    echo "3. From source:"
    echo "   git clone $GITHUB_URL/$APM_REPO.git"
    echo "   cd apm && uv sync && uv run pip install -e ."
    echo ""
    
    if [ "$PLATFORM" = "linux" ]; then
        echo -e "${BLUE}Debug information:${NC}"
        echo "Check missing libraries: ldd $TMP_DIR/$EXTRACTED_DIR/$BINARY_NAME"
        echo ""
    fi
    
    echo "Need help? Create an issue at: $GITHUB_URL/$APM_REPO/issues"
    exit 1
fi

# Install binary directory structure
echo -e "${YELLOW}Installing APM CLI to $APM_INSTALL_DIR...${NC}"

# APM installation directory (for the complete bundle)
APM_LIB_DIR="${APM_LIB_DIR:-$(dirname "$APM_INSTALL_DIR")/lib/apm}"

# Remove any existing installation
if [ -d "$APM_LIB_DIR" ]; then
    if [ -w "$(dirname "$APM_LIB_DIR")" ]; then
        rm -rf "$APM_LIB_DIR"
    else
        sudo rm -rf "$APM_LIB_DIR"
    fi
fi

# Create installation directory
if [ -w "$(dirname "$APM_LIB_DIR")" ]; then
    mkdir -p "$APM_LIB_DIR"
    cp -r "$TMP_DIR/$EXTRACTED_DIR"/* "$APM_LIB_DIR/"
else
    sudo mkdir -p "$APM_LIB_DIR"
    sudo cp -r "$TMP_DIR/$EXTRACTED_DIR"/* "$APM_LIB_DIR/"
fi

# Create symlink pointing to the actual binary
if mkdir -p "$APM_INSTALL_DIR" 2>/dev/null && [ -w "$APM_INSTALL_DIR" ]; then
    ln -sf "$APM_LIB_DIR/$BINARY_NAME" "$APM_INSTALL_DIR/$BINARY_NAME"
else
    sudo mkdir -p "$APM_INSTALL_DIR"
    sudo ln -sf "$APM_LIB_DIR/$BINARY_NAME" "$APM_INSTALL_DIR/$BINARY_NAME"
fi

# Verify installation
if command -v apm >/dev/null 2>&1; then
    INSTALLED_VERSION=$(apm --version 2>/dev/null || echo "unknown")
    echo -e "${GREEN}[+] APM installed successfully!${NC}"
    echo -e "${BLUE}Version: $INSTALLED_VERSION${NC}"
    echo -e "${BLUE}Location: $APM_INSTALL_DIR/$BINARY_NAME -> $APM_LIB_DIR/$BINARY_NAME${NC}"
else
    echo -e "${YELLOW}[!] APM installed but not found in PATH${NC}"
    echo "You may need to add $APM_INSTALL_DIR to your PATH environment variable."
    echo "Add this line to your shell profile (.bashrc, .zshrc, etc.):"
    echo "  export PATH=\"$APM_INSTALL_DIR:\$PATH\""
fi

echo ""
echo -e "${GREEN}Installation complete!${NC}"
echo ""
echo -e "${BLUE}Quick start:${NC}"
echo "  apm init my-app          # Create a new APM project"
echo "  cd my-app && apm install # Install dependencies"
echo "  apm run                  # Run your first prompt"
echo ""
echo -e "${BLUE}Documentation:${NC} $GITHUB_URL/$APM_REPO"
echo -e "${BLUE}Need help?${NC} Create an issue at $GITHUB_URL/$APM_REPO/issues"
