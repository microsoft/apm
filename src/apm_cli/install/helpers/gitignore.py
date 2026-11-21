"""Keep ``apm_modules/`` out of git for project-scope installs (#3879)."""

from pathlib import Path

from ...constants import APM_MODULES_GITIGNORE_PATTERN, GITIGNORE_FILENAME
from ...utils.console import _rich_info, _rich_warning


def update_gitignore_for_apm_modules(logger=None):
    """Add apm_modules/ to .gitignore if not already present."""
    gitignore_path = Path(GITIGNORE_FILENAME)
    apm_modules_pattern = APM_MODULES_GITIGNORE_PATTERN

    # Read current .gitignore content
    current_content = []
    if gitignore_path.exists():
        try:
            with open(gitignore_path, encoding="utf-8") as f:
                current_content = [line.rstrip("\n\r") for line in f.readlines()]
        except Exception as e:
            if logger:
                logger.warning(f"Could not read .gitignore: {e}")
            else:
                _rich_warning(f"Could not read .gitignore: {e}")
            return

    # Check if apm_modules/ is already in .gitignore
    if any(line.strip() == apm_modules_pattern for line in current_content):
        return  # Already present

    # Add apm_modules/ to .gitignore
    try:
        with open(gitignore_path, "a", encoding="utf-8") as f:
            # Add a blank line before our entry if file isn't empty
            if current_content and current_content[-1].strip():
                f.write("\n")
            f.write(f"\n# APM dependencies\n{apm_modules_pattern}\n")

        if logger:
            logger.progress(f"Added {apm_modules_pattern} to .gitignore")
        else:
            _rich_info(f"Added {apm_modules_pattern} to .gitignore")
    except Exception as e:
        if logger:
            logger.warning(f"Could not update .gitignore: {e}")
        else:
            _rich_warning(f"Could not update .gitignore: {e}")
