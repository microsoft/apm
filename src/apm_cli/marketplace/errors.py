"""Marketplace-specific error hierarchy."""


class MarketplaceError(Exception):
    """Base class for marketplace errors."""

    pass


class MarketplaceNotFoundError(MarketplaceError):
    """Raised when a registered marketplace cannot be found."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(
            f"Marketplace '{name}' is not registered. "
            f"Run 'apm marketplace add OWNER/REPO' to register it, "
            f"or 'apm marketplace list' to see registered marketplaces."
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


class MarketplaceYmlError(MarketplaceError):
    """Raised when marketplace.yml validation or parsing fails."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class MarketplaceFetchError(MarketplaceError):
    """Raised when fetching marketplace data fails."""

    def __init__(self, name: str, reason: str = ""):
        self.name = name
        self.reason = reason
        detail = f": {reason}" if reason else ""
        super().__init__(
            f"Failed to fetch marketplace '{name}'{detail}. "
            f"Run 'apm marketplace update {name}' to retry."
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
            f"No tag matching version '{version_range}' found for "
            f"package '{package}'{extra}",
            package=package,
        )


class RefNotFoundError(BuildError):
    """An explicit ref (tag/branch/SHA) was not found on the remote."""

    def __init__(self, package: str, ref: str, remote: str):
        self.ref = ref
        self.remote = remote
        super().__init__(
            f"Ref '{ref}' not found on remote '{remote}' "
            f"for package '{package}'",
            package=package,
        )


class HeadNotAllowedError(BuildError):
    """Resolved ref is HEAD or a branch name and allow_head is False."""

    def __init__(self, package: str, ref: str):
        self.ref = ref
        super().__init__(
            f"Package '{package}' resolves to branch/HEAD ref '{ref}'. "
            f"Branch refs are mutable and not recommended for reproducible builds. "
            f"Pin to a tag or SHA, or pass --allow-head to override.",
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
