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
from collections import defaultdict
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
    return f"apm-{PLATFORM_MAP[npm_platform]}-{ARCH_MAP[npm_arch]}"


def get_repo_root() -> Path:
    return Path(__file__).parent.parent


def get_version(repo_root: Path) -> str:
    """Get the version from pyproject.toml."""
    import toml
    
    manifest_path = repo_root / "pyproject.toml"
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = toml.load(f)
        
    return data["project"]["version"]


def setup_jinja_env(repo_root: Path) -> Environment:
    template_dir = repo_root / "packages/@microsoft/apm-cli/templates"
    return Environment(loader=FileSystemLoader(str(template_dir)))


def copy_common_files(repo_root: Path, dest_dir: Path) -> None:
    """Copy README.md and LICENSE to the package directory."""
    for filename in ["README.md", "LICENSE"]:
        src = repo_root / filename
        dest = dest_dir / filename
        if src.exists():
            shutil.copy2(src, dest)


def write_template(env: Environment, template_name: str, dest_path: Path, **kwargs) -> None:
    """Render a Jinja2 template and write to disk."""
    content = env.get_template(template_name).render(**kwargs)
    dest_path.write_text(content)
    logger.info(f"Writing {dest_path}")


def find_binary_content(repo_root: Path, archive_name: str) -> Path:
    """Find the compiled binary content folder."""
    candidates = [
        repo_root / "dist" / archive_name,
        repo_root / "artifacts" / archive_name / "dist" / archive_name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
            
    logger.error(f"Could not find binary content for {archive_name} in expected locations:")
    for candidate in candidates:
        logger.error(f"  - {candidate}")
    raise FileNotFoundError(f"Binary content for {archive_name} not found")


def generate_platform_package(
    env: Environment, repo_root: Path, npm_dist_root: Path, npm_platform: str, npm_arch: str, version: str
) -> None:
    """Generate a single platform package into dist/npm/."""
    package_name = f"apm-cli-{npm_platform}-{npm_arch}"
    package_root = npm_dist_root / package_name
    package_root.mkdir(parents=True, exist_ok=True)

    # 1. Write package.json
    write_template(
        env,
        "platform-package.json.jinja2",
        package_root / "package.json",
        version=version,
        npm_platform=npm_platform,
        npm_arch=npm_arch,
    )

    # 2. Copy binary content
    binary_src = find_binary_content(repo_root, get_archive_name(npm_platform, npm_arch))
    logger.info(f"Copying binary content from {binary_src} to {package_root}")
    shutil.copytree(binary_src, package_root, dirs_exist_ok=True)

    # 3. Copy standard files
    copy_common_files(repo_root, package_root)


def generate_main_package(env: Environment, repo_root: Path, npm_dist_root: Path, version: str) -> None:
    """Generate the main apm-cli package with launcher and dependencies into dist/npm/."""
    main_package_root = npm_dist_root / "apm-cli"
    main_package_root.mkdir(parents=True, exist_ok=True)

    # 1. Write package.json
    write_template(
        env,
        "apm-cli-package.json.jinja2",
        main_package_root / "package.json",
        version=version,
        platforms=PLATFORMS,
    )

    # 2. Write launcher (bin/apm)
    bin_dir = main_package_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    
    grouped_platforms = defaultdict(list)
    for p_os, p_arch in PLATFORMS:
        grouped_platforms[p_os].append(p_arch)

    launcher_target = bin_dir / "apm"
    write_template(
        env,
        "apm-cli-bin.js.jinja2",
        launcher_target,
        grouped_platforms=dict(grouped_platforms),
    )
    launcher_target.chmod(0o755)

    # 3. Copy standard files
    copy_common_files(repo_root, main_package_root)


def main() -> None:
    """Generate all npm packages for APM."""
    logger.info("Starting npm package generation")

    repo_root = get_repo_root()
    npm_dist_root = repo_root / "dist" / "npm"
    version = get_version(repo_root)
    env = setup_jinja_env(repo_root)
    
    logger.info(f"Version: {version}")

    try:
        for npm_platform, npm_arch in PLATFORMS:
            logger.info(f"Generating package for {npm_platform}-{npm_arch}")
            generate_platform_package(env, repo_root, npm_dist_root, npm_platform, npm_arch, version)

        logger.info("Generating main package")
        generate_main_package(env, repo_root, npm_dist_root, version)
        
        logger.info("All packages generated successfully")
    except Exception as e:
        logger.error(f"Failed during package generation: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
