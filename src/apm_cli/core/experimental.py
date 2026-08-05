"""Experimental feature-flag subsystem for APM CLI.

Provides a lightweight, static-registry mechanism to gate new or changed
behaviour behind named feature flags.  Early adopters can opt-in via
``apm experimental enable <name>`` without branching or separate builds.

**Caller convention (mandatory):**

    Import ``is_enabled`` at *function scope*, never at module level, to
    avoid triggering config I/O at import time for unrelated commands::

        def my_function():
            from apm_cli.core.experimental import is_enabled
            if is_enabled("verbose_version"):
                ...

**Security invariant:**

    Experimental flags MUST NOT gate security-critical behaviour -- content
    scanning, path validation, lockfile integrity, token handling, MCP trust
    boundary checks, collision detection, or any check documented in
    ``enterprise/security.md``.  ``~/.apm/config.json`` is user-writable
    and carries user-equivalent trust only.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Registry dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExperimentalFlag:
    """Descriptor for a single experimental feature flag.

    Attributes:
        name: Internal snake_case identifier (must match the ``FLAGS`` key).
        description: One-line summary (<=80 chars, printable ASCII only).
        default: Registry default -- must be ``False`` for every flag.
        hint: Optional next-step message shown after a successful ``enable``.
    """

    name: str
    description: str
    default: bool
    hint: str | None = None


# ---------------------------------------------------------------------------
# Static registry -- add new flags here
# ---------------------------------------------------------------------------

FLAGS: dict[str, ExperimentalFlag] = {
    "verbose_version": ExperimentalFlag(
        name="verbose_version",
        description="Show Python version, platform, and install path in 'apm --version'.",
        default=False,
        hint="Run 'apm --version' to see the new output.",
    ),
    "copilot_app": ExperimentalFlag(
        name="copilot_app",
        description="Deploy prompts as workflows into the GitHub Copilot desktop App.",
        default=False,
        hint=(
            "Add workflow frontmatter (e.g. 'interval: manual') to any "
            ".prompt.md, then install "
            "with '--target copilot-app' (project or '--global' user scope). "
            "Workflows arrive disabled; enable them from the Copilot app's "
            "Workflows tab."
        ),
    ),
    "grok_cloud": ExperimentalFlag(
        name="grok_cloud",
        description="Deploy skills to xAI Grok Cloud.",
        default=False,
        hint="Use '--target grok-cloud' to deploy skills.",
    ),
    "marketplace_authoring": ExperimentalFlag(
        name="marketplace_authoring",
        description="Enable marketplace authoring commands (init, build, publish, etc.).",
        default=False,
        hint="Run 'apm marketplace --help' to see available commands.",
    ),
    "registries": ExperimentalFlag(
        name="registries",
        description="Enable REST-based APM package registries in apm.yml.",
        default=False,
        hint=("Use registries: in apm.yml. See https://microsoft.github.io/apm/guides/registries/"),
    ),
    "canvas": ExperimentalFlag(
        name="canvas",
        description="Ship Copilot CLI canvas extensions via .apm/extensions/ bundles.",
        default=False,
        hint=(
            "Author a canvas under .apm/extensions/<name>/extension.mjs, then "
            "'apm install' deploys it to .github/extensions/. Dependency-provided "
            "canvases are executable and blocked unless approved via allowExecutables "
            "in apm.yml and 'apm approve <pkg>'. See "
            "https://microsoft.github.io/apm/integrations/canvas/"
        ),
    ),
    "external_scanners": ExperimentalFlag(
        name="external_scanners",
        description="External SARIF scanner ingestion + optional audit at install time.",
        default=False,
        hint=(
            "Opt-in per run with 'apm audit --external sarif --external-sarif "
            "report.sarif' (any SARIF 2.1.0 tool), or '--external skillspector' "
            "when its CLI is on PATH. Also unlocks running 'apm audit' during "
            "'apm install' -- set the mode with 'apm config set audit-on-install "
            "warn|block' or an apm-policy.yml 'security.audit.on_install' rule. "
            "See https://microsoft.github.io/apm/integrations/external-scanners/"
        ),
    ),
    "openclaw": ExperimentalFlag(
        name="openclaw",
        description="Deploy skills to OpenClaw agent runtime directories.",
        default=False,
        hint=(
            "Use '--target openclaw' to deploy skills to your project, "
            "or '--target openclaw --global' for your personal OpenClaw "
            "skills at ~/.openclaw/skills/."
        ),
    ),
    "hermes": ExperimentalFlag(
        name="hermes",
        description="Deploy skills, AGENTS.md, and MCP servers to the Hermes agent.",
        default=False,
        hint=(
            "Use '--target hermes' to deploy skills + AGENTS.md to your "
            "project, or '--target hermes --global' for your personal Hermes "
            "home at ~/.hermes/ (skills + MCP servers in config.yaml)."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Graduated flags
# ---------------------------------------------------------------------------

GRADUATED_FLAGS: dict[str, str] = {
    "copilot_cowork": (
        "The 'copilot-cowork' target is now generally available -- no flag is "
        "needed.\nRun: apm install --target copilot-cowork --global"
    ),
}
"""Flags that graduated to GA, mapped to migration guidance.

A graduated flag is NOT a typo, so ``difflib`` suggestions are actively
harmful here: the nearest registered name is usually an unrelated feature
(``copilot-cowork`` -> ``copilot-app``), and following that suggestion
silently enables the wrong behaviour.  Entries stay here permanently so
stale scripts get a correct answer rather than a plausible wrong one.

The precedence is closest-match, not graduated-first: an exact graduated
name always wins, but a graduated *near-miss* only wins when it scores
above the best live-flag match.  Otherwise ``copilot-ap`` -- a typo for
the live ``copilot-app`` -- would route to the graduated ``copilot-cowork``
hint and reintroduce the wrong-answer failure from the other direction.

Add an entry whenever a flag is removed from :data:`FLAGS` because its
behaviour became the default.  Keys MUST NOT overlap :data:`FLAGS` -- a
live flag would shadow the entry and render it dead code.
"""


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------


def normalise_flag_name(name: str) -> str:
    """Normalise a CLI flag name to its internal snake_case form.

    Accepts both ``verbose-version`` and ``verbose_version``.
    """
    return name.replace("-", "_").lower()


def display_name(name: str) -> str:
    """Convert an internal snake_case flag name to kebab-case for display."""
    return name.replace("_", "-")


# ---------------------------------------------------------------------------
# Config access helper
# ---------------------------------------------------------------------------


def _get_experimental_section() -> dict:
    """Return the ``experimental`` section from config as a dict.

    If the value is not a dict (e.g. user hand-edited the file to an int
    or string), returns an empty dict so every consumer fails closed.
    """
    from apm_cli.config import get_config

    experimental = get_config().get("experimental", {})
    return experimental if isinstance(experimental, dict) else {}


# ---------------------------------------------------------------------------
# Core query
# ---------------------------------------------------------------------------


def is_enabled(name: str) -> bool:
    """Check whether an experimental flag is currently enabled.

    Derives directly from ``get_config()`` (already cached in
    ``apm_cli.config._config_cache``).  Net cost per call: two dict
    lookups after the first config load -- no I/O, no intermediate
    object allocation.

    Args:
        name: Internal snake_case flag identifier.

    Returns:
        ``True`` if the flag is enabled, ``False`` otherwise.

    Raises:
        ValueError: If *name* is not a registered flag (fail loud on typos
            in shipped code).
    """
    if name not in FLAGS:
        raise ValueError(
            f"Unknown experimental flag: {name!r}. Registered flags: {', '.join(sorted(FLAGS))}"
        )

    experimental = _get_experimental_section()

    value = experimental.get(name)
    # Reject non-bool overrides -- fail closed to registry default.
    if not isinstance(value, bool):
        return FLAGS[name].default
    return value


# ---------------------------------------------------------------------------
# Mutators (thin wrappers around apm_cli.config.update_config)
# ---------------------------------------------------------------------------


def _match_ratio(probe: str, candidate: str) -> float:
    """Similarity of ``probe`` to ``candidate`` on difflib's 0..1 scale.

    Used to compare a graduated-flag near-miss against the best live-flag
    near-miss so the closer of the two wins.
    """
    return difflib.SequenceMatcher(None, probe, candidate).ratio()


def validate_flag_name(name: str) -> str:
    """Validate and normalise a flag name from CLI input.

    Returns the normalised snake_case name on success.

    Raises:
        ValueError: If the flag is not registered.  ``args[1]`` carries
            ``difflib``-based suggestions; ``args[2]`` carries migration
            guidance when the name resolves to a graduated flag rather
            than a typo, and ``args[3]`` the graduated flag's display name.
    """
    normalised = normalise_flag_name(name)
    if normalised in FLAGS:
        return normalised

    display = display_name(normalised)
    msg = f"Unknown experimental feature: {display}"

    guidance = GRADUATED_FLAGS.get(normalised)
    graduated_name = normalised if guidance is not None else None

    suggestions = difflib.get_close_matches(
        normalised,
        FLAGS.keys(),
        n=3,
        cutoff=0.6,
    )

    if guidance is None:
        # A near-miss on a graduated name ('copilot-cowrk') must not fall
        # through to FLAGS matching, which would suggest an unrelated target.
        # It must not shadow a *better* live match either: 'copilot-ap' is a
        # typo for the live 'copilot-app', not for the graduated
        # 'copilot-cowork', and routing it to the graduated hint would send
        # the caller to a different feature entirely -- the same wrong-answer
        # failure this registry exists to prevent. Prefer whichever side
        # actually matches more closely.
        graduated_match = difflib.get_close_matches(
            normalised, GRADUATED_FLAGS.keys(), n=1, cutoff=0.6
        )
        if graduated_match:
            best_live = max(
                (_match_ratio(normalised, name) for name in suggestions),
                default=0.0,
            )
            if _match_ratio(normalised, graduated_match[0]) > best_live:
                graduated_name = graduated_match[0]
                guidance = GRADUATED_FLAGS[graduated_name]

    if guidance is not None:
        raise ValueError(msg, [], guidance, display_name(graduated_name))

    raise ValueError(msg, [display_name(s) for s in suggestions], None)


def _set_flag(name: str, value: bool) -> ExperimentalFlag:
    """Set an experimental flag to a bool value and persist the override.

    Args:
        name: Snake_case flag identifier (already validated).
        value: ``True`` to enable, ``False`` to disable.

    Returns:
        The ``ExperimentalFlag`` descriptor for post-mutation messaging.
    """
    from apm_cli.config import update_config

    flag = FLAGS[name]
    experimental = dict(_get_experimental_section())
    experimental[name] = value
    update_config({"experimental": experimental})
    return flag


def enable(name: str) -> ExperimentalFlag:
    """Enable an experimental flag and persist the override.

    Args:
        name: Snake_case flag identifier (already validated).

    Returns:
        The ``ExperimentalFlag`` descriptor for post-enable messaging.
    """
    return _set_flag(name, True)


def disable(name: str) -> ExperimentalFlag:
    """Disable an experimental flag and persist the override.

    Args:
        name: Snake_case flag identifier (already validated).

    Returns:
        The ``ExperimentalFlag`` descriptor for post-disable messaging.
    """
    return _set_flag(name, False)


def reset(name: str | None = None) -> int:
    """Reset one or all experimental flags to their registry defaults.

    When *name* is ``None``, clears all keys from ``experimental``
    (sets it to ``{}``).  When *name* is given, removes only that
    single key.

    Args:
        name: Snake_case flag identifier, or ``None`` for bulk reset.

    Returns:
        Number of keys that were actually removed.
    """
    from apm_cli.config import update_config

    experimental = dict(_get_experimental_section())

    if name is not None:
        if name in experimental:
            del experimental[name]
            update_config({"experimental": experimental})
            return 1
        return 0

    # Bulk reset -- remove all keys
    count = len(experimental)
    if count:
        update_config({"experimental": {}})
    return count


def get_overridden_flags() -> dict[str, bool]:
    """Return the dict of flags that have user overrides in config.

    Only includes flags that are still registered in ``FLAGS``.
    Values are the current override booleans.
    """
    experimental = _get_experimental_section()
    return {k: v for k, v in experimental.items() if k in FLAGS and isinstance(v, bool)}


def get_stale_config_keys() -> list[str]:
    """Return config keys under ``experimental`` that are not in ``FLAGS``.

    These are leftovers from removed flags and are safe to clean up via
    ``apm experimental reset``.
    """
    experimental = _get_experimental_section()
    return [k for k in experimental if k not in FLAGS]


def get_malformed_flag_keys() -> list[str]:
    """Return registered flag names whose config values are not booleans.

    These are known flags with corrupt values (e.g. ``"true"`` instead of
    ``True``).  They are safe to remove via ``apm experimental reset``.
    """
    experimental = _get_experimental_section()
    return [k for k in experimental if k in FLAGS and not isinstance(experimental[k], bool)]
