#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

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
        echo -e "${RED}Unsupported architecture: $ARCH${NC}"
        exit 1
        ;;
esac

# Normalize OS names and set binary name
case $OS in
    Darwin)
        PLATFORM="darwin"
        BINARY_NAME="apm-darwin-$ARCH"
        ;;
    Linux)
        PLATFORM="linux"
        BINARY_NAME="apm-linux-$ARCH"
        ;;
    *)
        echo -e "${RED}Unsupported operating system: $OS${NC}"
        exit 1
        ;;
esac

echo -e "${BLUE}Building APM binary for $PLATFORM-$ARCH${NC}"
echo -e "${BLUE}Output binary: $BINARY_NAME${NC}"

# Clean previous builds
echo -e "${YELLOW}Cleaning previous builds...${NC}"
rm -rf build/build/ dist/

# Check if PyInstaller is available via uv
if ! uv run pyinstaller --version &> /dev/null; then
    echo -e "${RED}PyInstaller not found. Make sure dependencies are installed with: uv sync --extra build${NC}"
    exit 1
fi

# Check if UPX is available (optional, for compression)
if command -v upx &> /dev/null; then
    echo -e "${GREEN}UPX found - binary will be compressed${NC}"
else
    echo -e "${YELLOW}UPX not found - binary will not be compressed (install with: brew install upx)${NC}"
fi

# Inject build SHA into version.py
VERSION_FILE="src/apm_cli/version.py"
BUILD_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "")
if [ -n "$BUILD_SHA" ]; then
    echo -e "${YELLOW}Injecting build SHA: $BUILD_SHA${NC}"
    sed -i.bak "s/^__BUILD_SHA__ = None$/__BUILD_SHA__ = \"$BUILD_SHA\"/" "$VERSION_FILE"
    # Guarantee restore on any exit (success, failure, or signal)
    trap 'if [ -f "${VERSION_FILE}.bak" ]; then mv "${VERSION_FILE}.bak" "$VERSION_FILE"; fi' EXIT
fi

# Build binary
echo -e "${YELLOW}Building binary with PyInstaller...${NC}"
uv run pyinstaller build/apm.spec

# Check if build was successful (onedir mode creates dist/apm/apm)
if [ ! -f "dist/apm/apm" ]; then
    echo -e "${RED}Build failed - binary not found${NC}"
    exit 1
fi

# Rename the directory to have the platform-specific name
mv "dist/apm" "dist/$BINARY_NAME"

# Make binary executable
chmod +x "dist/$BINARY_NAME/apm"

# Test the binary
echo -e "${YELLOW}Testing binary...${NC}"
if "./dist/$BINARY_NAME/apm" --version; then
    echo -e "${GREEN}✓ Binary test successful${NC}"
else
    echo -e "${RED}✗ Binary test failed${NC}"
    exit 1
fi

# Enforce the glibc floor when APM_MAX_GLIBC is set. Release builds set 2.28 so
# the binary runs on RHEL/Rocky/Alma 8. PyInstaller copies libpython and the
# extension modules from the build interpreter, so the check reads every ELF file.
if [ "$PLATFORM" = "linux" ] && [ -n "${APM_MAX_GLIBC:-}" ]; then
    echo -e "${YELLOW}Checking that bundled ELF files need glibc <= ${APM_MAX_GLIBC}...${NC}"
    if ! command -v objdump &> /dev/null; then
        echo -e "${RED}objdump not found - install binutils to run the glibc check${NC}"
        exit 1
    fi
    GLIBC_VIOLATIONS=""
    GLIBC_CHECKED=0
    while IFS= read -r -d '' elf; do
        GLIBC_CHECKED=$((GLIBC_CHECKED + 1))
        required=$(objdump -T "$elf" 2>/dev/null | grep -o 'GLIBC_[0-9][0-9.]*' | sed 's/GLIBC_//' | sort -uV | tail -1)
        if [ -n "$required" ] && [ "$(printf '%s\n%s\n' "$APM_MAX_GLIBC" "$required" | sort -V | tail -1)" != "$APM_MAX_GLIBC" ]; then
            GLIBC_VIOLATIONS="${GLIBC_VIOLATIONS}  GLIBC_${required} ${elf}\n"
        fi
    done < <(find "dist/$BINARY_NAME" -type f \( -name 'apm' -o -name '*.so' -o -name '*.so.*' \) -print0)
    if [ -n "$GLIBC_VIOLATIONS" ]; then
        echo -e "${RED}✗ Bundled files require glibc newer than ${APM_MAX_GLIBC}:${NC}"
        echo -e "$GLIBC_VIOLATIONS"
        echo -e "${RED}Build with a Python targeting an older glibc (e.g. UV_PYTHON_PREFERENCE=only-managed)${NC}"
        exit 1
    fi
    if [ "$GLIBC_CHECKED" -eq 0 ]; then
        echo -e "${RED}✗ No ELF files found under dist/$BINARY_NAME${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ ${GLIBC_CHECKED} bundled ELF files work with glibc ${APM_MAX_GLIBC}${NC}"
fi

# Show binary info
echo -e "${GREEN}✓ Build complete!${NC}"
echo -e "${BLUE}Binary: ./dist/$BINARY_NAME/apm${NC}"
echo -e "${BLUE}Size: $(du -h "dist/$BINARY_NAME" | tail -1 | cut -f1)${NC}"

# Create checksum for the binary directory (as expected by CI workflow)
if command -v sha256sum &> /dev/null; then
    sha256sum "dist/$BINARY_NAME/apm" > "dist/$BINARY_NAME.sha256"
    echo -e "${BLUE}Checksum: ./dist/$BINARY_NAME.sha256${NC}"
elif command -v shasum &> /dev/null; then
    shasum -a 256 "dist/$BINARY_NAME/apm" > "dist/$BINARY_NAME.sha256"
    echo -e "${BLUE}Checksum: ./dist/$BINARY_NAME.sha256${NC}"
fi

echo -e "${GREEN}Ready for release!${NC}"
