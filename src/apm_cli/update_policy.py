"""Build-time policy for APM self-update behavior.

Package maintainers can patch this module during build to disable self-update
and show users a package-manager-specific update command.
"""

# Default guidance when self-update is disabled.
DEFAULT_SELF_UPDATE_DISABLED_MESSAGE = (
    "Self-update is disabled for this APM distribution. Update APM using your package manager."
)

# Build-time policy values.
#
# Packagers can patch these constants during build, for example:
# - SELF_UPDATE_ENABLED = False
# - SELF_UPDATE_DISABLED_MESSAGE = "Update with: pixi update apm-cli"
SELF_UPDATE_ENABLED = True
SELF_UPDATE_DISABLED_MESSAGE = DEFAULT_SELF_UPDATE_DISABLED_MESSAGE


def _is_printable_ascii(value: str) -> bool:
    """Return True when value contains only printable ASCII characters."""
    return all(" " <= char <= "~" for char in value)


def is_self_update_enabled() -> bool:
    """Return True when this build allows self-update."""
    return SELF_UPDATE_ENABLED is True


def get_self_update_disabled_message() -> str:
    """Return the guidance message shown when self-update is disabled."""
    if SELF_UPDATE_DISABLED_MESSAGE is None:
        return DEFAULT_SELF_UPDATE_DISABLED_MESSAGE

    message = str(SELF_UPDATE_DISABLED_MESSAGE).strip()
    if not message:
        return DEFAULT_SELF_UPDATE_DISABLED_MESSAGE

    if not _is_printable_ascii(message):
        return DEFAULT_SELF_UPDATE_DISABLED_MESSAGE

    return message


def get_update_hint_message() -> str:
    """Return the update hint used in startup notifications."""
    if is_self_update_enabled():
        return "Run apm update to upgrade"
    return get_self_update_disabled_message()
