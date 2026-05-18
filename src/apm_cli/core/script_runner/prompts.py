"""Script runner for APM NPM-like script execution."""

from __future__ import annotations

from pathlib import Path


def _search_apm_modules_for_prompt(self, search_name: str, name: str) -> Path | None:
    """Search apm_modules for a prompt file, detecting collisions.

    Returns the single matching Path, or None if nothing found.
    Raises RuntimeError on collision.
    """
    apm_modules = Path("apm_modules")
    if not apm_modules.exists():
        return None

    raw_matches = list(apm_modules.rglob(search_name))

    for skill_dir in apm_modules.rglob(name):
        if skill_dir.is_dir():
            skill_file = skill_dir / "SKILL.md"
            if skill_file.exists():
                raw_matches.append(skill_file)

    matches = [m for m in raw_matches if not m.is_symlink()]

    if len(matches) == 0:
        return None
    elif len(matches) == 1:
        return matches[0]
    else:
        self._handle_prompt_collision(name, matches)
        return None


def _find_in_owner_packages(
    self, owner_dir: Path, prompt_name: str, qualified_path: str
) -> Path | None:
    """Search an owner's package directories for a prompt matching qualified_path."""
    for pkg_dir in owner_dir.iterdir():
        if not pkg_dir.is_dir():
            continue
        for prompt_path in pkg_dir.rglob(prompt_name):
            if self._matches_qualified_path(prompt_path, qualified_path):
                return prompt_path
    return None


def _discover_prompt_file(self, name: str) -> Path | None:
    """Discover prompt files by name across local and dependencies.

    Supports both simple names and qualified paths:
    - Simple: "code-review" -> searches everywhere
    - Qualified: "github/awesome-copilot/code-review" -> searches specific package

    Search order for simple names:
    1. Local root: ./{name}.prompt.md
    2. Local prompts: .apm/prompts/{name}.prompt.md
    3. GitHub convention: .github/prompts/{name}.prompt.md
    4. Dependencies: apm_modules/**/.apm/prompts/{name}.prompt.md
    5. Dependencies root: apm_modules/**/{name}.prompt.md

    Args:
        name: Script/prompt name or qualified path (owner/repo/name)

    Returns:
        Path to discovered prompt file, or None if not found

    Raises:
        RuntimeError: If multiple prompts found with same name (collision)
    """
    # Check if this is a qualified path (contains /)
    if "/" in name:
        return self._discover_qualified_prompt(name)

    # Ensure name doesn't already have .prompt.md extension
    if name.endswith(".prompt.md"):  # noqa: SIM108
        search_name = name
    else:
        search_name = f"{name}.prompt.md"

    # 1. Check local paths first (highest priority)
    local_search_paths = [
        Path(search_name),  # Local root
        Path(f".apm/prompts/{search_name}"),  # APM prompts dir
        Path(f".github/prompts/{search_name}"),  # GitHub convention
    ]

    for path in local_search_paths:
        if path.exists() and not path.is_symlink():
            return path

    # 2. Search in dependencies and detect collisions
    return _search_apm_modules_for_prompt(self, search_name, name)


def _discover_qualified_prompt(self, qualified_path: str) -> Path | None:
    """Discover prompt using qualified path (owner/repo/name format).

    Args:
        qualified_path: Qualified path like "github/awesome-copilot/code-review"

    Returns:
        Path to discovered prompt file, or None if not found
    """
    # Parse qualified path: owner/repo/name or owner/repo-name/name
    parts = qualified_path.split("/")

    if len(parts) < 2:
        return None

    # Extract prompt name (last part)
    prompt_name = parts[-1]
    if not prompt_name.endswith(".prompt.md"):
        prompt_name = f"{prompt_name}.prompt.md"

    # Build possible package directory patterns
    # Could be: owner/repo or owner/repo-promptname (virtual packages)
    apm_modules = Path("apm_modules")
    if not apm_modules.exists():
        return None

    # Try to find matching package directory
    owner = parts[0]

    # Check if owner directory exists
    owner_dir = apm_modules / owner
    if not owner_dir.exists():
        return None

    # For subdirectory packages (skills), check for SKILL.md first
    # e.g., github/awesome-copilot/skills/architecture-blueprint-generator
    # installs to apm_modules/github/awesome-copilot/skills/architecture-blueprint-generator/SKILL.md
    if len(parts) >= 3:
        subdir_path = apm_modules.joinpath(*parts)
        skill_file = subdir_path / "SKILL.md"
        if skill_file.exists():
            return skill_file

    # Search within this owner's packages for .prompt.md files
    return _find_in_owner_packages(self, owner_dir, prompt_name, qualified_path)


def _matches_qualified_path(self, prompt_path: Path, qualified_path: str) -> bool:
    """Check if a prompt path matches the qualified path specification.

    Args:
        prompt_path: Actual path to prompt file
        qualified_path: User-specified qualified path

    Returns:
        True if paths match
    """
    # For now, just check if the qualified path components appear in the prompt path
    # This is a simple heuristic that works for most cases
    path_str = str(prompt_path)
    qualified_parts = qualified_path.split("/")

    # Check if owner is in the path
    if qualified_parts[0] not in path_str:
        return False

    # Check if prompt name matches
    prompt_name = qualified_parts[-1]
    if not prompt_name.endswith(".prompt.md"):
        prompt_name = f"{prompt_name}.prompt.md"

    return prompt_path.name == prompt_name


def _handle_prompt_collision(self, name: str, matches: list[Path]) -> None:
    """Handle collision when multiple prompts found with same name.

    Args:
        name: Prompt name that has collisions
        matches: List of matching prompt paths

    Raises:
        RuntimeError: Always raises with helpful disambiguation message
    """
    # Build helpful error message
    error_msg = f"Multiple prompts found for '{name}':\n"

    # List all matches with their package paths
    for match in matches:
        # Extract package identifier from path
        path_parts = match.parts
        if "apm_modules" in path_parts:
            idx = path_parts.index("apm_modules")
            if idx + 2 < len(path_parts):
                owner = path_parts[idx + 1]
                pkg = path_parts[idx + 2]
                error_msg += f"  - {owner}/{pkg} ({match})\n"
            else:
                error_msg += f"  - {match}\n"
        else:
            error_msg += f"  - {match}\n"

    error_msg += "\nPlease specify using qualified path:\n"

    # Suggest qualified paths based on matches
    for match in matches:
        path_parts = match.parts
        if "apm_modules" in path_parts:
            idx = path_parts.index("apm_modules")
            if idx + 2 < len(path_parts):
                owner = path_parts[idx + 1]
                pkg = path_parts[idx + 2]
                error_msg += f"  apm run {owner}/{pkg}/{name}\n"

    error_msg += "\nOr add an explicit script to apm.yml:\n"
    error_msg += "  scripts:\n"
    error_msg += f'    my-{name}: "copilot -p <path-to-preferred-prompt>"\n'

    raise RuntimeError(error_msg)


def _is_virtual_package_reference(self, name: str) -> bool:
    """Check if a name looks like a virtual package reference.

    Virtual packages have format:
    - owner/repo/path/to/file.prompt.md (virtual file)
    - owner/repo/skills/name (virtual subdirectory/skill)
    - owner/repo/collections/name (virtual subdirectory)

    Args:
        name: Name to check

    Returns:
        True if this looks like a virtual package reference
    """
    # Must have at least one slash
    if "/" not in name:
        return False

    from ...models.apm_package import DependencyReference

    try:
        dep_ref = DependencyReference.parse(name)
        return dep_ref.is_virtual
    except Exception:
        return False


def _auto_install_virtual_package(self, package_ref: str) -> bool:
    """Auto-install a virtual package.

    Handles two types of virtual packages:
    - Virtual files: owner/repo/prompts/file.prompt.md
    - Virtual subdirectories (skills, collections): owner/repo/skills/name

    Args:
        package_ref: Virtual package reference

    Returns:
        True if installation succeeded, False otherwise
    """
    try:
        from ...deps.github_downloader import GitHubPackageDownloader
        from ...models.apm_package import DependencyReference

        # Parse the reference as-is  -- no extension guessing
        dep_ref = DependencyReference.parse(package_ref)

        if not dep_ref.is_virtual:
            return False

        # Ensure apm_modules exists
        apm_modules = Path("apm_modules")
        apm_modules.mkdir(parents=True, exist_ok=True)

        # Use the canonical install path from the dependency reference
        target_path = dep_ref.get_install_path(apm_modules)

        # Check if already installed
        if target_path.exists():
            print(f"  [i]  Package already installed at {target_path}")
            return True

        # Download the virtual package
        downloader = GitHubPackageDownloader()

        print(f"   Downloading from {dep_ref.to_github_url()}")

        if dep_ref.is_virtual_subdirectory():
            package_info = downloader.download_subdirectory_package(dep_ref, target_path)
        else:
            package_info = downloader.download_virtual_file_package(dep_ref, target_path)

        # PackageInfo has a 'package' attribute which is an APMPackage
        print(f"  [+] Installed {package_info.package.name} v{package_info.package.version}")

        # Update apm.yml to include this dependency
        self._add_dependency_to_config(package_ref)

        return True

    except Exception as e:
        print(f"  [x] Auto-install failed: {e}")
        return False


def _add_dependency_to_config(self, package_ref: str) -> None:
    """Add a virtual package dependency to apm.yml.

    Args:
        package_ref: Virtual package reference to add
    """
    config_path = Path("apm.yml")

    # Skip if apm.yml doesn't exist (e.g., in test environments)
    if not config_path.exists():
        return

    # Load current config
    from ...utils.yaml_io import dump_yaml, load_yaml

    config = load_yaml(config_path) or {}

    # Ensure dependencies.apm section exists
    if "dependencies" not in config:
        config["dependencies"] = {}
    if "apm" not in config["dependencies"]:
        config["dependencies"]["apm"] = []

    # Add the dependency if not already present
    if package_ref not in config["dependencies"]["apm"]:
        config["dependencies"]["apm"].append(package_ref)

        # Write back to file
        dump_yaml(config, config_path)

        print(f"  [i]  Added {package_ref} to apm.yml dependencies")


def _create_minimal_config(self) -> None:
    """Create a minimal apm.yml for zero-config usage.

    This enables running virtual packages without apm init.
    """
    minimal_config = {
        "name": Path.cwd().name,
        "version": "1.0.0",
        "description": "Auto-generated for zero-config virtual package execution",
    }

    from ...utils.yaml_io import dump_yaml

    dump_yaml(minimal_config, "apm.yml")

    print("  [i]  Created minimal apm.yml for zero-config execution")
