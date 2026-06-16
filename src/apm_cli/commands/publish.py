"""``apm publish`` command — upload a packed tarball to a registry.

Implements docs/proposals/registry-api.md §5.3:
``PUT /v1/packages/{owner}/{repo}/versions/{version}``

Gated behind the same ``registries`` experimental feature as the registry
resolver (``apm experimental enable registries``).
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import click

from ..core.command_logger import CommandLogger
from ..deps.registry.feature_gate import require_package_registry_enabled
from ..utils.paths import portable_relpath

_PUBLISH_HELP = """\
Publish a package to a registry.

Reads apm.yml for the package name/version, packs a flat registry zip archive
(``apm.yml`` + ``.apm/`` at the archive root — not ``apm pack`` plugin bundles),
or uses a pre-built zip via --zip, then uploads to the registry via
PUT /v1/packages/{owner}/{repo}/versions/{version}.

Requires the 'registries' experimental feature:
  apm experimental enable registries

Examples:

  # Auto-pack and publish to the only configured registry:
  apm publish

  # Choose a registry when multiple are configured:
  apm publish --registry corp-main

  # Publish a pre-built zip (skip the pack step):
  apm publish --zip ./build/my-package-1.0.0.zip

  # Preview what would be uploaded:
  apm publish --dry-run
"""


@click.command(name="publish", help=_PUBLISH_HELP)
@click.option(
    "--registry",
    "registry_name",
    default=None,
    help="Registry name (from apm.yml 'registries:' block). Required when multiple registries are configured.",
)
@click.option(
    "--package",
    "package_id",
    required=True,
    metavar="OWNER/REPO",
    help="Package identity to publish as (owner/repo, e.g. acme/my-skill).",
)
@click.option(
    "--zip",
    "zip_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Path to a pre-built .zip archive. Skips the pack step.",
)
@click.option("--dry-run", is_flag=True, default=False, help="Preview without uploading.")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output.")
@click.pass_context
def publish_cmd(ctx, registry_name, package_id, zip_path, dry_run, verbose):
    """Publish a package version to a registry."""
    require_package_registry_enabled("apm publish")

    logger = CommandLogger("publish", verbose=verbose, dry_run=dry_run)

    # ------------------------------------------------------------------ apm.yml
    project_root = Path.cwd()
    apm_yml_path = project_root / "apm.yml"
    if not apm_yml_path.exists():
        raise click.ClickException("apm.yml not found in the current directory.")

    from ..models.apm_package import APMPackage

    try:
        pkg = APMPackage.from_apm_yml(apm_yml_path)
    except Exception as exc:
        raise click.ClickException(f"Failed to read apm.yml: {exc}") from exc

    version = pkg.version
    if not version:
        raise click.ClickException("apm.yml must declare a 'version:' field to publish.")

    # Authoring-path nudge (#1777): warn when the author's own package declares
    # no license. Silent on the consuming path; never blocks publish.
    from ..export.authoring import warn_if_license_undeclared

    warn_if_license_undeclared(apm_yml_path, logger.warning)

    # ----------------------------------------------------------- owner/repo
    owner, repo = _resolve_package_id(package_id)

    # ----------------------------------------------------------- registry
    registries: dict[str, str] = pkg.registries or {}
    registry_name = _resolve_registry_name(registry_name, registries)
    base_url = registries[registry_name]

    # ----------------------------------------------------------- zip archive
    if zip_path:
        archive = Path(zip_path)
    else:
        archive = _pack_archive(project_root, apm_yml_path, pkg, logger, verbose)

    archive_size = archive.stat().st_size

    # ----------------------------------------------------------- dry-run
    if dry_run:
        logger.info(f"Would publish {owner}/{repo}@{version} to {registry_name} ({base_url})")
        logger.info(f"  archive : {archive}  ({archive_size:,} bytes)")
        logger.info("(dry-run — nothing uploaded)")
        return

    # ----------------------------------------------------------- upload
    from ..deps.registry.auth import make_auth_context
    from ..deps.registry.client import RegistryClient, RegistryError

    auth = make_auth_context(registry_name)
    client = RegistryClient(base_url, auth)

    logger.info(f"Publishing {owner}/{repo}@{version} to {registry_name} …")

    archive_bytes = archive.read_bytes()
    try:
        result = client.publish_version(owner, repo, version, archive_bytes)
    except RegistryError as exc:
        _handle_publish_error(exc, owner, repo, version, registry_name, base_url)

    logger.success(
        f"Published {result.package}@{result.version}\n"
        f"  digest      : {result.digest}\n"
        f"  published_at: {result.published_at or '(not returned by server)'}\n"
        f"  registry    : {base_url}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_package_id(package_id: str) -> tuple[str, str]:
    """Parse ``--package OWNER/REPO`` into ``(owner, repo)``.

    Accepts bare ``owner/repo`` or a full URL
    (``https://github.com/owner/repo``); strips the scheme+host prefix when
    present.  Raises ``UsageError`` when the value cannot be parsed as a
    two-segment identity.
    """
    raw = re.sub(r"^https?://[^/]+/", "", package_id).strip("/")
    parts = [p for p in raw.split("/") if p]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    raise click.UsageError(
        f"--package must be in owner/repo form (got {package_id!r}).\n"
        "Example: --package acme/my-skill"
    )


def _resolve_registry_name(name: str | None, registries: dict[str, str]) -> str:
    """Pick the registry name to publish to."""
    if not registries:
        raise click.ClickException(
            "No registries configured in apm.yml.\n"
            "Add a 'registries:' block — see 'apm experimental enable registries'."
        )
    if name:
        if name not in registries:
            available = ", ".join(sorted(registries))
            raise click.ClickException(
                f"Registry {name!r} not found in apm.yml. Available: {available}"
            )
        return name
    if len(registries) == 1:
        return next(iter(registries))
    available = ", ".join(sorted(registries))
    raise click.UsageError(
        f"Multiple registries configured ({available}). Use --registry NAME to choose one."
    )


def _pack_archive(project_root: Path, apm_yml_path: Path, pkg, logger, verbose: bool) -> Path:
    """Build a flat registry zip archive (``apm.yml`` + ``.apm/`` at archive root).

    Also includes ``README.md``, ``CHANGELOG.md``, and ``LICENSE`` (case-
    insensitive, no extension required for LICENSE) when present — matching
    npm's behaviour of bundling standard root-level documentation files.

    Registry servers and ``apm install`` expect the APM source layout at the
    archive root — not the ``apm pack --archive`` plugin bundle wrapper
    (``{name}-{version}/plugin.json``). See registry HTTP API §6.
    """
    apm_dir = project_root / ".apm"
    if not apm_dir.is_dir():
        raise click.ClickException(
            "Registry publish requires a flat APM package (.apm/ directory).\n"
            "Add .apm/ with your primitives (skills, instructions, etc.), "
            "or pass --zip with a pre-built flat archive."
        )

    archive_name = f"{pkg.name}-{pkg.version}.zip"
    dest = project_root / archive_name
    if dest.exists():
        dest.unlink()

    if verbose:
        logger.info(f"Packing flat registry archive -> {dest.name}")

    def _should_skip(path: Path) -> bool:
        # Skip macOS AppleDouble sidecars (._*) and .DS_Store that break registry validation.
        return any(part.startswith("._") for part in path.parts) or path.name == ".DS_Store"

    # Standard root-level doc files included when present (npm parity).
    # Matched case-insensitively; LICENSE has no required extension.
    _DOC_CANDIDATES = ("README.md", "CHANGELOG.md", "LICENSE", "LICENCE")

    with zipfile.ZipFile(dest, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        if not _should_skip(apm_yml_path):
            zf.write(apm_yml_path, arcname="apm.yml")
        for file in sorted(apm_dir.rglob("*")):
            if file.is_file() and not file.is_symlink() and not _should_skip(file):
                zf.write(file, arcname=portable_relpath(file, project_root))
        for candidate in _DOC_CANDIDATES:
            # Case-insensitive match against actual filenames in project root.
            match = next(
                (
                    f
                    for f in project_root.iterdir()
                    if f.is_file() and not f.is_symlink() and f.name.lower() == candidate.lower()
                ),
                None,
            )
            if match and not _should_skip(match):
                zf.write(match, arcname=match.name)
                if verbose:
                    logger.info(f"  bundling {match.name}")

    return dest


def _handle_publish_error(
    exc,
    owner: str,
    repo: str,
    version: str,
    registry_name: str,
    base_url: str,
) -> None:
    """Translate RegistryError HTTP statuses into user-friendly messages."""
    status = exc.status
    detail = exc.problem.get("detail", "") if exc.problem else ""

    if status == 409:
        raise click.ClickException(
            f"Version {version!r} of {owner}/{repo} already exists in "
            f"{registry_name!r} and is immutable. Bump the version in apm.yml."
        )
    if status == 422:
        msg = "Registry rejected the package (validation failed)"
        if detail:
            msg += f": {detail}"
        raise click.ClickException(msg)
    if status == 403:
        from ..deps.registry.auth import registry_token_env_var

        raise click.ClickException(
            f"Forbidden — your token does not have publish permission for "
            f"{owner}/{repo} in {registry_name!r}.\n"
            f"Check the token configured via {registry_token_env_var(registry_name)}."
        )
    if status == 401:
        from ..deps.registry.auth import remediation_message

        raise click.ClickException(remediation_message(base_url))

    raise click.ClickException(str(exc))
