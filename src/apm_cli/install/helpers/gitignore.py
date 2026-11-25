"""Keep ``apm_modules/`` out of git for project-scope installs (#3879)."""

import os
from pathlib import Path

from ...constants import APM_MODULES_GITIGNORE_PATTERN, GITIGNORE_FILENAME
from ...utils.console import _rich_info, _rich_warning


def _newline(text: str) -> str:
    """The file's own line ending (its first one), else the platform's (#5458)."""
    end = text.find("\n")
    if end < 0:
        return os.linesep
    return "\r\n" if end > 0 and text[end - 1] == "\r" else "\n"


def update_gitignore_for_apm_modules(logger=None):
    """Add apm_modules/ to .gitignore if not already present."""
    gitignore_path = Path(GITIGNORE_FILENAME)
    apm_modules_pattern = APM_MODULES_GITIGNORE_PATTERN

    # Read current .gitignore content
    current_content = []
    current_text = ""
    if gitignore_path.exists():
        try:
            with open(gitignore_path, encoding="utf-8", newline="") as f:
                current_text = f.read()
            current_content = [line.rstrip("\n\r") for line in current_text.splitlines()]
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
        from ...integration import write_seam

        # The full text, so the deploy journal knows the bytes it writes (#4863).
        nl = _newline(current_text)
        separator = nl if current_content and current_content[-1].strip() else ""
        addition = f"{separator}{nl}# APM dependencies{nl}{apm_modules_pattern}{nl}"
        write_seam.write_text(gitignore_path, current_text + addition, newline="")

        if logger:
            logger.progress(f"Added {apm_modules_pattern} to .gitignore")
        else:
            _rich_info(f"Added {apm_modules_pattern} to .gitignore")
    except Exception as e:
        if logger:
            logger.warning(f"Could not update .gitignore: {e}")
        else:
            _rich_warning(f"Could not update .gitignore: {e}")
