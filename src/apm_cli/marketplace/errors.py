"""Marketplace-specific error hierarchy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.remediation import (
    MARKETPLACE_FETCH_FAILED,
    MARKETPLACE_ORIGIN_RESTORE,
    MARKETPLACE_ORIGIN_SWITCH,
    MARKETPLACE_ORIGIN_SWITCH_GLOBAL,
    MARKETPLACE_UNREGISTERED_SHORTHAND,
    MARKETPLACE_UNREGISTERED_URL,
    either_sentence,
)

if TYPE_CHECKING:
    from .resolver import CrossRepoMisconfigRisk


class MarketplaceError(Exception):
    """Base class for marketplace errors."""

    pass


class MarketplaceNotFoundError(MarketplaceError):
    """Raised when a registered marketplace cannot be found."""

    def __init__(self, name: str, host: str = "github.com"):
        self.name = name
        self.host = host
        # Interpolate the active host so GHES users get a copy-paste-ready
        # URL that works for them. Callers should pass the current host
        # (e.g. default_host()); fall back to github.com to preserve the
        # public-cloud default.
        register = either_sentence(
            MARKETPLACE_UNREGISTERED_URL.bind(host=host), MARKETPLACE_UNREGISTERED_SHORTHAND
        )
        super().__init__(
            f"Marketplace '{name}' is not registered. {register} "
            "Run 'apm marketplace list' to see registered marketplaces."
        )


class ManualMarketplaceConflictError(MarketplaceError):
    """Raised when a manifest entry targets a manually registered marketplace name."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(
            f"Marketplace '{name}' was registered manually with 'apm marketplace add'; "
            "the user manifest does not overwrite it."
        )


class PluginNotFoundError(MarketplaceError):
    """Raised when a plugin is not found in a marketplace."""

    def __init__(self, plugin_name: str, marketplace_name: str):
        self.plugin_name = plugin_name
        self.marketplace_name = marketplace_name
        super().__init__(
            f"Plugin '{plugin_name}' not found in marketplace '{marketplace_name}'. "
            f"Run 'apm marketplace browse {marketplace_name}' to see available plugins."
        )


class DependencyConfusionError(MarketplaceError, ValueError):
    """A marketplace entry resolved to an ambiguous bare cross-repo source (#1326).

    Raised only by
    :func:`~apm_cli.marketplace.dependency_confusion.admit_marketplace_resolution`.
    ``ValueError`` stays a base so resolution-error handlers keep recording it.
    """

    def __init__(self, risk: CrossRepoMisconfigRisk) -> None:
        self.risk = risk
        super().__init__(
            "\n".join(
                [
                    f"refused (dependency-confusion risk #1326): bare"
                    f" `{risk.locator_field}: {risk.bare_repo_field}` on enterprise"
                    f" marketplace '{risk.marketplace_host}' is ambiguous."
                    f" Host-qualify the plugin `{risk.locator_field}` field in"
                    f" marketplace.json to one of:",
                    f"  - '{risk.suggested_qualified_repo}' (enterprise dep on this marketplace)",
                    f"  - 'github.com/{risk.bare_repo_field}' (declared cross-host dep on public github.com)",
                ]
            )
        )


def _manifest_entry_label(entry: str | dict | None, plugin_name: str) -> str:
    """Name a dependency the way ``apm.yml`` writes it (RA-042).

    A marketplace install records the resolved source (a string or a
    ``git`` + ``path`` mapping), never the plugin name, so the remedy quotes
    that entry. Without it, fall back to the plugin name.
    """
    if isinstance(entry, str) and entry:
        return repr(entry)
    if isinstance(entry, dict) and entry:
        fields = ", ".join(f"{key}: {value}" for key, value in entry.items())
        return f"the entry '{fields}'"
    return repr(plugin_name)


class MarketplaceOriginConflictError(MarketplaceError):
    """Raised when an alias now names a different marketplace than the lock recorded.

    *user_scope* renders the switch remedy against the user manifest
    (``~/.apm/apm.yml``, ``apm install -g``). The restore remedy is the same at
    both scopes: the marketplace registry is user-global.
    """

    def __init__(
        self,
        marketplace_name: str,
        plugin_name: str,
        locked: str,
        current: str,
        *,
        user_scope: bool = False,
        manifest_entry: str | dict | None = None,
    ):
        import shlex

        from ..utils.diagnostics import printable_ascii_text
        from .source_identity import parse_marketplace_source

        self.marketplace_name = marketplace_name
        self.plugin_name = plugin_name
        self.locked_origin = locked
        self.current_origin = current
        self.user_scope = user_scope
        self.manifest_entry = manifest_entry
        # The locked origin is lock-file data: render it as printable ASCII and
        # advise restoring the lock before re-registering what it names.
        shown_locked = printable_ascii_text(locked)
        shown_current = printable_ascii_text(current)
        try:
            if shown_locked != locked:
                raise ValueError(locked)
            parse_marketplace_source(locked)
        except ValueError:
            keep = (
                "Otherwise, verify the locked source, then register it again with "
                f"'apm marketplace add' under the name {marketplace_name!r}."
            )
        else:
            remedy = MARKETPLACE_ORIGIN_RESTORE.bind(
                source=shlex.quote(locked), name=shlex.quote(marketplace_name)
            )
            keep = f"Otherwise, {remedy.clause()}."
        switch_entry = MARKETPLACE_ORIGIN_SWITCH_GLOBAL if user_scope else MARKETPLACE_ORIGIN_SWITCH
        switch = switch_entry.bind(
            plugin=_manifest_entry_label(manifest_entry, plugin_name)
        ).clause()
        lockfile = "~/.apm/apm.lock.yaml" if user_scope else "apm.lock.yaml"
        super().__init__(
            f"marketplace {marketplace_name!r} now points at {shown_current}, but {lockfile} "
            f"recorded {plugin_name!r} from {shown_locked}. Nothing was changed. "
            f"If {lockfile} was not changed on purpose, restore it from version control. "
            f"{keep} To switch marketplaces on purpose, {switch}."
        )


class MarketplaceProvenanceError(MarketplaceError):
    """Raised when a locked marketplace row cannot be re-verified against its catalog (#4228)."""


class MarketplaceYmlError(MarketplaceError):
    """Raised when marketplace.yml validation or parsing fails."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class MarketplaceFetchError(MarketplaceError):
    """Raised when fetching marketplace data fails."""

    def __init__(self, name: str, reason: str = "", *, retry_hint: str | None = None):
        self.name = name
        self.reason = reason
        detail = f": {reason}" if reason else ""
        retry = (
            retry_hint
            if retry_hint is not None
            else MARKETPLACE_FETCH_FAILED.bind(name=name).sentence()
        )
        super().__init__(f"Failed to fetch marketplace '{name}'{detail}. {retry}")


class MarketplaceEgressRefusedError(MarketplaceFetchError):
    """Raised when ``PROXY_REGISTRY_ONLY`` refuses a direct catalog fetch.

    A policy refusal, not a network failure: callers must not fall back to a
    cached catalog or report it as unreachable.
    """


class MarketplaceFreshnessError(MarketplaceFetchError):
    """Raised when an explicit refresh cannot revalidate a marketplace catalog.

    Explicit freshness (``apm install --refresh``, ``apm update``,
    ``apm marketplace update``) never falls back to cached catalog data, so
    a failure here means no catalog answer was used, not that a plugin is
    missing.
    """

    RETRY_HINT = (
        "The cached marketplace was kept and nothing was changed. Retry when the "
        "marketplace is reachable; a plain 'apm install' still uses the cached copy."
    )

    def __init__(self, name: str, reason: str = ""):
        super().__init__(name, reason, retry_hint=self.RETRY_HINT)
        detail = f": {reason}" if reason else ""
        self.args = (
            f"Could not verify that marketplace '{name}' is current{detail}. {self.RETRY_HINT}",
        )


class MarketplaceNotCachedError(MarketplaceFetchError):
    """An advisory read during an explicit refresh found no cached catalog."""

    def __init__(self, name: str):
        super().__init__(
            name,
            "no cached marketplace for an advisory read during an explicit refresh",
            retry_hint="",
        )


# ---------------------------------------------------------------------------
# Builder errors (used by builder.py and ref_resolver.py)
# ---------------------------------------------------------------------------


class BuildError(MarketplaceError):
    """Base class for errors raised during marketplace build."""

    def __init__(self, message: str, *, package: str = ""):
        self.package = package
        super().__init__(message)


class NoMatchingVersionError(BuildError):
    """No remote tag satisfies the requested semver range."""

    def __init__(self, package: str, version_range: str, *, detail: str = ""):
        self.version_range = version_range
        extra = f" ({detail})" if detail else ""
        super().__init__(
            f"No tag matching version '{version_range}' found for package '{package}'{extra}",
            package=package,
        )


class RefNotFoundError(BuildError):
    """An explicit ref (tag/branch/SHA) was not found on the remote."""

    def __init__(self, package: str, ref: str, remote: str):
        self.ref = ref
        self.remote = remote
        super().__init__(
            f"Ref '{ref}' not found on remote '{remote}' for package '{package}'",
            package=package,
        )


class HeadNotAllowedError(BuildError):
    """Resolved ref is HEAD or a branch name and allow_head is False."""

    def __init__(self, package: str, ref: str):
        self.ref = ref
        super().__init__(
            f"Package '{package}' resolves to branch/HEAD ref '{ref}'. "
            f"Branch refs are mutable and not recommended for reproducible builds. "
            f"Pin 'ref' to a tag or a full 40-character commit SHA "
            f"(for example: apm marketplace package set {package} --ref <tag-or-sha>).",
            package=package,
        )


class OfflineMissError(BuildError):
    """Offline mode requested but the ref cache has no entry for the remote."""

    def __init__(self, package: str, remote: str):
        self.remote = remote
        super().__init__(
            f"Offline mode: no cached refs for '{remote}' "
            f"(package '{package}'). Run a build online first.",
            package=package,
        )


class GitLsRemoteError(BuildError):
    """git ls-remote failed (wraps TranslatedGitError)."""

    def __init__(self, package: str, summary: str, hint: str):
        self.summary_text = summary
        self.hint = hint
        super().__init__(
            f"{summary} {hint}",
            package=package,
        )
