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
if [ ! -f "dist/apm/apm" ] || [ ! -f "dist/apm/apmx" ]; then
    echo -e "${RED}Build failed - binary not found${NC}"
    exit 1
fi

# Rename the directory to have the platform-specific name
mv "dist/apm" "dist/$BINARY_NAME"

# Make binary executable
chmod +x "dist/$BINARY_NAME/apm" "dist/$BINARY_NAME/apmx"

# Test the binary
echo -e "${YELLOW}Testing binary...${NC}"
if "./dist/$BINARY_NAME/apm" --version; then
    echo -e "${GREEN}✓ Binary test successful${NC}"
else
    echo -e "${RED}✗ Binary test failed${NC}"
    exit 1
fi

./dist/"$BINARY_NAME"/apmx --version
./dist/"$BINARY_NAME"/apmx --help > /dev/null

# Show binary info
echo -e "${GREEN}✓ Build complete!${NC}"
echo -e "${BLUE}Binary: ./dist/$BINARY_NAME/apm${NC}"
echo -e "${BLUE}Size: $(du -h "dist/$BINARY_NAME" | tail -1 | cut -f1)${NC}"

# Create checksum for the binary directory (as expected by CI workflow)
if command -v sha256sum &> /dev/null; then
    sha256sum "dist/$BINARY_NAME/apm" "dist/$BINARY_NAME/apmx" > "dist/$BINARY_NAME.sha256"
    echo -e "${BLUE}Checksum: ./dist/$BINARY_NAME.sha256${NC}"
elif command -v shasum &> /dev/null; then
    shasum -a 256 "dist/$BINARY_NAME/apm" "dist/$BINARY_NAME/apmx" > "dist/$BINARY_NAME.sha256"
    echo -e "${BLUE}Checksum: ./dist/$BINARY_NAME.sha256${NC}"
fi

echo -e "${GREEN}Ready for release!${NC}"
