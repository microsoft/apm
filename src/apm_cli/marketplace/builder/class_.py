"""MarketplaceBuilder: dataclasses and class for the marketplace build pipeline.

Pipeline steps (load → resolve → compose → write) are in ``compose``,
``metadata``, and ``resolve_helpers`` sibling modules.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...core.auth import HostInfo

from ...utils.github_host import default_host
from ...utils.path_security import ensure_path_within
from ..diagnostics import BuildDiagnostic
from ..errors import (
    BuildError,
)
from ..output_mappers import (
    MARKETPLACE_OUTPUT_MAPPERS,
    MapperResult,
)
from ..output_mappers import (
    _subtract_plugin_root as _mapper_subtract_plugin_root,
)
from ..output_profiles import (
    MarketplaceOutputProfile,
)
from ..ref_resolver import RefResolver
from ..yml_schema import MarketplaceYml, PackageEntry, load_marketplace_yml
from ._class_models import (
    BuildOptions,
    BuildReport,
    MarketplaceOutputReport,
    ResolvedPackage,
    ResolveResult,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BuildDiagnostic",
    "BuildOptions",
    "BuildReport",
    "MarketplaceBuilder",
    "ResolveResult",
    "ResolvedPackage",
]

# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _subtract_plugin_root(source: str, plugin_root: str) -> str:
    """Remove pluginRoot prefix from a local source path for emit."""
    return _mapper_subtract_plugin_root(source, plugin_root)


class MarketplaceBuilder:
    """Load marketplace.yml, resolve refs, compose and write marketplace.json.

    Parameters
    ----------
    marketplace_yml_path:
        Path to the ``marketplace.yml`` file.
    options:
        Build options.  Defaults to ``BuildOptions()`` if not provided.
    auth_resolver:
        Optional ``AuthResolver`` for authenticating requests to private
        GitHub repositories.  When ``None`` (default) a fresh resolver is
        created lazily the first time a token is needed.
    """

    def __init__(
        self,
        marketplace_yml_path: Path,
        options: BuildOptions | None = None,
        auth_resolver: object | None = None,
    ) -> None:
        self._yml_path = marketplace_yml_path
        self._project_root = marketplace_yml_path.parent
        self._options = options or BuildOptions()
        self._yml: MarketplaceYml | None = None
        self._resolver: RefResolver | None = None
        self._auth_resolver = auth_resolver
        # Resolved once per build, used by worker threads (read-only).
        self._github_token: str | None = None
        self._host: str = default_host() or "github.com"
        self._host_info: HostInfo | None = None
        self._auth_resolved: bool = False

    @classmethod
    def from_config(
        cls,
        config: MarketplaceYml,
        project_root: Path,
        options: BuildOptions | None = None,
        auth_resolver: object | None = None,
    ) -> MarketplaceBuilder:
        """Construct a builder from an already-loaded MarketplaceConfig.

        Use this when the caller has already chosen between apm.yml and
        the legacy ``marketplace.yml`` (typically via
        ``migration.load_marketplace_config``).  ``project_root`` is the
        directory output paths are resolved against.
        """
        # Use a synthetic path so legacy code paths that consult
        # ``self._yml_path.parent`` still resolve to the project root.
        synthetic_path = project_root / (
            config.source_path.name if config.source_path is not None else "apm.yml"
        )
        instance = cls(synthetic_path, options=options, auth_resolver=auth_resolver)
        instance._project_root = project_root
        instance._yml = config
        return instance

    # -- lazy loaders -------------------------------------------------------

    def _load_yml(self) -> MarketplaceYml:
        if self._yml is None:
            # Shape-aware load: when the configured path is an apm.yml
            # file, use the apm.yml loader; otherwise default to the
            # legacy marketplace.yml loader.  Callers that have already
            # loaded a config should use ``from_config`` to bypass this.
            from ..yml_schema import load_marketplace_from_apm_yml

            if self._yml_path.name == "apm.yml":
                self._yml = load_marketplace_from_apm_yml(self._yml_path)
            else:
                self._yml = load_marketplace_yml(self._yml_path)
        return self._yml

    def _get_resolver(self) -> RefResolver:
        if self._resolver is None:
            self._ensure_auth()
            self._resolver = RefResolver(
                timeout_seconds=self._options.timeout_seconds,
                offline=self._options.offline,
                host=self._host,
                token=self._github_token,
            )
        return self._resolver

    def _ensure_auth(self) -> None:
        """Lazily resolve host classification and GitHub token.

        Short-circuits when already resolved (even if no token was found)
        or when running in offline mode.  Offline mode is still marked as
        resolved so repeated calls remain idempotent.  Called by
        ``_get_resolver()`` so both ``resolve()`` and ``build()`` benefit
        from authenticated ``git ls-remote`` when available.
        """
        if self._auth_resolved:
            return
        if self._options.offline:
            self._auth_resolved = True
            return
        self._github_token = self._resolve_github_token()
        self._auth_resolved = True

    # -- output path --------------------------------------------------------

    def _output_path(self) -> Path:
        if self._options.marketplace_output is not None:
            return self._options.marketplace_output
        if self._options.output_override is not None:
            return self._options.output_override
        yml = self._load_yml()
        output_path = self._project_root / yml.claude.output
        # Containment guard -- reject output paths that escape the project root.
        ensure_path_within(output_path, self._project_root)
        return output_path

    def _mapper_for_profile(self, profile: MarketplaceOutputProfile):
        mapper = MARKETPLACE_OUTPUT_MAPPERS.get(profile.mapper)
        if mapper is None:
            raise BuildError(f"Unknown marketplace output mapper: {profile.mapper}")
        return mapper

    def remote_metadata_for_profile(
        self,
        profile: MarketplaceOutputProfile,
        resolved: tuple[ResolvedPackage, ...],
    ) -> dict[str, dict[str, Any]] | None:
        """Return remote metadata needed to compose this output, if any."""
        mapper = self._mapper_for_profile(profile)
        if not mapper.uses_remote_metadata:
            return None
        return self._prefetch_metadata(resolved)

    def _map_output(
        self,
        profile: MarketplaceOutputProfile,
        resolved: tuple[ResolvedPackage, ...],
        remote_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> MapperResult:
        """Map resolved packages into one marketplace output format."""
        mapper = self._mapper_for_profile(profile)
        return mapper.compose(
            config=self._load_yml(),
            resolved=resolved,
            remote_metadata=remote_metadata,
        )

    # -- single-entry resolution --------------------------------------------

    def _resolve_entry(self, entry: PackageEntry) -> ResolvedPackage:
        return _resolve_helpers._resolve_entry(self, entry)

    def _resolve_explicit_ref(
        self, entry: PackageEntry, resolver: RefResolver, owner_repo: str
    ) -> ResolvedPackage:
        return _resolve_helpers._resolve_explicit_ref(self, entry, resolver, owner_repo)

    def _resolve_version_range(
        self, entry: PackageEntry, resolver: RefResolver, owner_repo: str, yml: MarketplaceYml
    ) -> ResolvedPackage:
        return _resolve_helpers._resolve_version_range(self, entry, resolver, owner_repo, yml)

    # -- concurrent resolution ----------------------------------------------

    def resolve(self) -> ResolveResult:
        return _resolve_helpers.resolve(self)

    # -- remote description fetcher -----------------------------------------

    def _fetch_remote_metadata(self, pkg: ResolvedPackage) -> dict[str, str] | None:
        return _metadata._fetch_remote_metadata(self, pkg)

    def _resolve_github_token(self) -> str | None:
        return _metadata._resolve_github_token(self)

    def _prefetch_metadata(self, resolved: list[ResolvedPackage]) -> dict[str, dict[str, str]]:
        return _metadata._prefetch_metadata(self, resolved)

    # -- composition --------------------------------------------------------

    def compose_marketplace_json(self, resolved: list[ResolvedPackage]) -> dict[str, Any]:
        return _compose.compose_marketplace_json(self, resolved)

    def compose_codex_marketplace_json(
        self, resolved: list[ResolvedPackage]
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        return _compose.compose_codex_marketplace_json(self, resolved)

    def write_codex_marketplace_json(
        self, resolved: tuple[ResolvedPackage, ...]
    ) -> tuple[Path, tuple[str, ...]]:
        return _compose.write_codex_marketplace_json(self, resolved)

    def compose_output(
        self,
        profile: MarketplaceOutputProfile,
        resolved: tuple[ResolvedPackage, ...],
        remote_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], tuple[str, ...], tuple[BuildDiagnostic, ...]]:
        return _compose.compose_output(self, profile, resolved, remote_metadata)

    def write_output(
        self,
        profile: MarketplaceOutputProfile,
        resolved: tuple[ResolvedPackage, ...],
        output_path: Path,
        **kwargs,
    ) -> BuildReport:
        """Write marketplace output. Keyword Args: include_diff, remote_metadata, errors."""
        return _compose.write_output(
            self,
            profile,
            resolved,
            output_path,
            **kwargs,
        )

    # -- diff ---------------------------------------------------------------

    @staticmethod
    def _compute_diff(
        old_json: dict[str, Any] | None, new_json: dict[str, Any]
    ) -> tuple[int, int, int, int]:
        return _compose._compute_diff(old_json, new_json)

    # -- atomic write -------------------------------------------------------

    @staticmethod
    def _serialize_json(data: dict[str, Any]) -> str:
        return _compose._serialize_json(data)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        return _compose._atomic_write(path, content)

    def _load_existing_json(self, path: Path) -> dict[str, Any] | None:
        return _compose._load_existing_json(self, path)

    # -- full pipeline ------------------------------------------------------

    def build(self) -> BuildReport:
        return _compose.build(self)


def _strip_ref_prefix(refname: str) -> str:
    """Strip ``refs/tags/`` or ``refs/heads/`` prefix."""
    if refname.startswith("refs/tags/"):
        return refname[len("refs/tags/") :]
    if refname.startswith("refs/heads/"):
        return refname[len("refs/heads/") :]
    return refname


from . import compose as _compose
from . import metadata as _metadata
from . import resolve_helpers as _resolve_helpers
