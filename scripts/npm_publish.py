#!/usr/bin/env python3
"""
Generate npm packages for each platform and publish them.

This script:
1. Reads the version from pyproject.toml
2. Generates package.json for the main wrapper and platform packages using Jinja2 templates
3. Copies the compiled binaries into each package directory in dist/npm/
4. Ready for npm publish
"""

import logging
import shutil
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

# Configure logging
logging.basicConfig(level=logging.INFO, format="[*] %(message)s")
logger = logging.getLogger(__name__)

# Platform configuration: (npm_platform, npm_arch)
PLATFORMS = [
    ("linux", "x64"),
    ("linux", "arm64"),
    ("darwin", "x64"),
    ("darwin", "arm64"),
    ("win32", "x64"),
]

# Mapping from npm names to APM release archive names
PLATFORM_MAP = {"linux": "linux", "darwin": "darwin", "win32": "windows"}
ARCH_MAP = {"x64": "x86_64", "arm64": "arm64"}


def get_archive_name(npm_platform: str, npm_arch: str) -> str:
    """Convert npm platform/arch to APM release archive name."""
    platform_name = PLATFORM_MAP[npm_platform]
    arch_name = ARCH_MAP[npm_arch]
    return f"apm-{platform_name}-{arch_name}"


def get_repo_root() -> Path:
    """Get the repository root directory."""
    return Path(__file__).parent.parent


def get_version() -> str:
    """Get the version from pyproject.toml."""
    repo_root = get_repo_root()
    manifest_path = repo_root / "pyproject.toml"

    with open(manifest_path) as f:
        content = f.read()

    # Parse version line: version = "0.10.0"
    for line in content.split("\n"):
        if line.startswith("version ="):
            # Extract version from: version = "0.10.0"
            version = line.split('"')[1]
            return version

    raise ValueError("Could not find version in pyproject.toml")


def render_platform_package_json(
    version: str,
    npm_platform: str,
    npm_arch: str,
) -> str:
    """Render platform package.json using Jinja2 template."""
    repo_root = get_repo_root()
    template_dir = repo_root / "packages/@microsoft/apm-cli/templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    template = env.get_template("platform-package.json.jinja2")

    return template.render(
        version=version,
        os=npm_platform,
        cpu=npm_arch,
    )


def generate_platform_package(npm_platform: str, npm_arch: str) -> None:
    """Generate a single platform package into dist/npm/."""
    repo_root = get_repo_root()
    version = get_version()
    archive_name = get_archive_name(npm_platform, npm_arch)
    package_name = f"apm-cli-{npm_platform}-{npm_arch}"

    # Generate into dist/npm/
    dist_root = repo_root / "dist/npm"
    package_root = dist_root / package_name
    package_json_path = package_root / "package.json"

    # Create package directory if it doesn't exist
    package_root.mkdir(parents=True, exist_ok=True)
    logger.info(f"Package directory: {package_root}")

    # Render and write package.json
    pkg_json_content = render_platform_package_json(
        version=version,
        npm_platform=npm_platform,
        npm_arch=npm_arch,
    )
    logger.info(f"Writing {package_json_path}")
    package_json_path.write_text(pkg_json_content)

    # Find and copy the binary content folder (dist/{archive_name} or artifacts/{archive_name}/dist/{archive_name})
    candidates = [
        repo_root / "dist" / archive_name,
        repo_root / "artifacts" / archive_name / "dist" / archive_name,
    ]

    binary_src = None
    for candidate in candidates:
        if candidate.exists():
            binary_src = candidate
            break
    if not binary_src:
        logger.error(f"Could not find binary content for {archive_name} in expected locations:")
        for candidate in candidates:
            logger.error(f"  - {candidate}")
        raise FileNotFoundError(f"Binary content for {archive_name} not found")

    # Copy binary content to package directory
    logger.info(f"Copying binary content from {binary_src} to {package_root}")
    shutil.copytree(binary_src, package_root, dirs_exist_ok=True)

    # Copy README.md and LICENSE from repo root
    for filename in ["README.md", "LICENSE"]:
        src = repo_root / filename
        dest = package_root / filename
        if src.exists():
            logger.info(f"Copying {src} -> {dest}")
            shutil.copy2(src, dest)


def update_main_package() -> None:
    """Generate the main apm-cli package with launcher and dependencies into dist/npm/."""
    repo_root = get_repo_root()
    version = get_version()

    # Generate into dist/npm/
    dist_root = repo_root / "dist/npm"
    main_package_root = dist_root / "apm-cli"
    manifest_path = main_package_root / "package.json"

    # Ensure main package directory exists
    main_package_root.mkdir(parents=True, exist_ok=True)
    logger.info(f"Generating main package at {main_package_root}")

    # Render and write package.json using template
    template_dir = repo_root / "packages/@microsoft/apm-cli/templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    template = env.get_template("apm-cli-package.json.jinja2")

    pkg_json_content = template.render(
        version=version,
        platforms=PLATFORMS,
    )
    logger.info(f"Writing {manifest_path}")
    manifest_path.write_text(pkg_json_content)

    # Ensure bin directory exists
    bin_dir = main_package_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    # Copy launcher from repo (packages/@microsoft/apm-cli/bin/apm)
    src_launcher = repo_root / "packages/@microsoft/apm-cli/bin/apm"
    launcher_target = bin_dir / "apm"
    if src_launcher.exists():
        logger.info(f"Copying launcher {src_launcher} -> {launcher_target}")
        shutil.copy2(src_launcher, launcher_target)
        launcher_target.chmod(0o755)
    else:
        logger.warning(f"Launcher not found at {src_launcher}")
        logger.warning("Please ensure packages/@microsoft/apm-cli/bin/apm exists")

    # Copy README.md and LICENSE from repo root
    for filename in ["README.md", "LICENSE"]:
        src = repo_root / filename
        dest = main_package_root / filename
        if src.exists():
            logger.info(f"Copying {src} -> {dest}")
            shutil.copy2(src, dest)


def main() -> None:
    """Generate all platform packages."""
    logger.info("Starting npm package generation")

    repo_root = get_repo_root()
    version = get_version()
    logger.info(f"Version: {version}")

    # Generate each platform package
    for npm_platform, npm_arch in PLATFORMS:
        logger.info(f"Generating package for {npm_platform}-{npm_arch}")
        try:
            generate_platform_package(npm_platform, npm_arch)
        except Exception as e:
            logger.error(f"Failed to generate package for {npm_platform}-{npm_arch}: {e}")
            sys.exit(1)

    # Update main package
    try:
        update_main_package()
    except Exception as e:
        logger.error(f"Failed to update main package: {e}")
        sys.exit(1)

    logger.info("All packages generated successfully")


if __name__ == "__main__":
    main()
