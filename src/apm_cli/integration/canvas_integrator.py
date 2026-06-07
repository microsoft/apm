"""Canvas extension integration for APM packages (experimental, Copilot-only).

A *canvas* is a GitHub Copilot CLI extension: a directory bundle whose
entry file is ``extension.mjs`` (executable Node.js) plus optional sibling
assets.  Authors place a canvas under ``.apm/extensions/<name>/`` and
``apm install`` deploys it verbatim to ``.github/extensions/<name>/`` so
Copilot CLI can discover it in the session.

Two independent gates protect this surface:

* The ``canvas`` experimental feature flag turns the primitive ON at all
  (feature availability -- NOT a security gate).
* A trust gate protects against arbitrary executable code: a canvas
  shipped by a *dependency* is blocked by default and requires the
  operator to pass ``--trust-canvas-extensions``.  The author's own
  first-party (root/local) ``.apm/extensions/`` deploys freely once the
  experimental flag is on.

The integrator is deliberately Copilot-only and project-scope-only for the
MVP: the canvas ``PrimitiveMapping`` lives solely on the ``copilot`` target
and user scope is unsupported.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.core.experimental import is_enabled
from apm_cli.install.cache_pin import MARKER_FILENAME
from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)
from apm_cli.utils.paths import portable_relpath

if TYPE_CHECKING:
    from apm_cli.integration.targets import TargetProfile

#: Entry file that marks a directory under ``.apm/extensions/`` as a canvas.
CANVAS_MARKER = "extension.mjs"

#: Permitted characters in a canvas directory name. The name becomes a
#: filesystem path segment and a Copilot extension id, so it is kept to a
#: conservative portable set.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: Windows reserved device names (case-insensitive). A canvas directory may
#: not use one because the name becomes a path segment.
_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)


def is_canvas_bundle_path(rel: str) -> bool:
    """Return True when a bundle-relative path belongs to a canvas extension.

    Used by the offline / local-bundle install and ``apm unpack`` code
    paths -- which copy bundle files verbatim and do NOT route through
    :class:`CanvasIntegrator` -- to detect executable canvas content so the
    trust gate can be enforced there too.  Without this a vendored bundle
    could smuggle an executable ``extension.mjs`` past the dependency trust
    gate.

    A path is a canvas path when an ``extensions`` segment appears either as
    the first path component (plugin-format bundle, e.g.
    ``extensions/<name>/extension.mjs``) or immediately under a client root
    dot-directory (legacy / direct deploy paths, e.g.
    ``.github/extensions/<name>/extension.mjs``).
    """
    parts = [seg for seg in rel.replace("\\", "/").split("/") if seg]
    for idx, seg in enumerate(parts):
        if seg != "extensions":
            continue
        if idx == 0:
            return True
        if parts[idx - 1].startswith("."):
            return True
    return False


class CanvasIntegrator(BaseIntegrator):
    """Deploys Copilot canvas extension bundles into ``.github/extensions/``."""

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @staticmethod
    def find_canvas_bundles(package_path: Path) -> list[Path]:
        """Return canvas bundle directories under ``.apm/extensions/``.

        A bundle is an *immediate* subdirectory of ``.apm/extensions/`` that
        contains an ``extension.mjs`` entry file.  Symlinked bundle
        directories are rejected for safety.
        """
        base = package_path / ".apm" / "extensions"
        if not base.is_dir():
            return []
        bundles: list[Path] = []
        for child in sorted(base.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            marker = child / CANVAS_MARKER
            if marker.is_file() and not marker.is_symlink():
                bundles.append(child)
        return bundles

    # ------------------------------------------------------------------
    # Name validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_canvas_name(name: str) -> None:
        """Raise ``PathTraversalError`` / ``ValueError`` for unsafe names."""
        validate_path_segments(name, context="canvas name")
        if not _NAME_RE.match(name):
            raise ValueError(
                f"Invalid canvas name '{name}': only letters, digits, '.', '_' and '-' are allowed"
            )
        if name.startswith(".") or name.endswith("."):
            raise ValueError(f"Invalid canvas name '{name}': must not start or end with '.'")
        if name.lower() in _RESERVED_NAMES:
            raise ValueError(f"Invalid canvas name '{name}': reserved device name")

    # ------------------------------------------------------------------
    # Target-driven API
    # ------------------------------------------------------------------

    def integrate_canvases_for_target(
        self,
        target: TargetProfile,
        package_info,
        project_root: Path,
        *,
        force: bool = False,
        managed_files: set[str] | None = None,
        diagnostics=None,
        scope=None,
        trust_canvas: bool = False,
        is_first_party: bool = False,
        package_name: str = "",
    ) -> IntegrationResult:
        """Deploy canvas bundles for a single *target* (copilot only).

        Returns an empty result (no-op) when the experimental flag is off,
        the target is not copilot, the mapping is absent, or the scope is
        user-level.  Dependency-provided canvases require *trust_canvas*;
        first-party canvases deploy whenever the flag is on.
        """
        empty = IntegrationResult(0, 0, 0, [])

        if not is_enabled("canvas"):
            return empty

        mapping = target.primitives.get("canvas")
        if mapping is None or target.name != "copilot":
            return empty

        from apm_cli.core.scope import InstallScope

        if scope is InstallScope.USER:
            # User-scope canvas deployment is unsupported in the MVP.  The
            # copilot profile already filters the mapping at user scope, so
            # this is belt-and-braces against any path that keeps it.
            return empty

        bundles = self.find_canvas_bundles(Path(package_info.install_path))
        if not bundles:
            return empty

        # Trust gate: a dependency's canvas is arbitrary executable Node
        # code.  Block it unless the operator opted in.  First-party
        # (root/local) canvases are the author's own and deploy freely.
        if not (is_first_party or trust_canvas):
            self._emit_trust_block(
                bundles, package_name, project_root, mapping, target, diagnostics
            )
            return empty

        managed = self.normalize_managed_files(managed_files) or set()
        effective_root = mapping.deploy_root or target.root_dir
        extensions_dir = project_root / effective_root / mapping.subdir

        files_integrated = 0
        files_skipped = 0
        files_adopted = 0
        target_paths: list[Path] = []

        for bundle in bundles:
            outcome = self._deploy_bundle(
                bundle,
                extensions_dir,
                project_root,
                managed=managed,
                force=force,
                diagnostics=diagnostics,
                package_name=package_name,
                target_paths=target_paths,
            )
            if outcome == "integrated":
                files_integrated += 1
            elif outcome == "adopted":
                files_adopted += 1
            elif outcome == "skipped":
                files_skipped += 1

        return IntegrationResult(
            files_integrated=files_integrated,
            files_updated=0,
            files_skipped=files_skipped,
            target_paths=target_paths,
            links_resolved=0,
            files_adopted=files_adopted,
        )

    def sync_for_target(
        self,
        target: TargetProfile,
        apm_package,
        project_root: Path,
        managed_files: set[str] | None = None,
    ) -> dict[str, int]:
        """Remove APM-managed canvas files for a single *target*.

        Not gated by the experimental flag: uninstall must always be able
        to remove previously-deployed canvases even after the flag is off.
        """
        mapping = target.primitives.get("canvas")
        if mapping is None:
            return {"files_removed": 0, "errors": 0}

        effective_root = mapping.deploy_root or target.root_dir
        prefix = f"{effective_root}/{mapping.subdir}/"
        stats = self.sync_remove_files(
            project_root,
            managed_files,
            prefix=prefix,
            targets=[target],
        )
        # Remove the now-empty .github/extensions/<name>/ directories left
        # behind once their files are gone.
        if managed_files:
            removed_paths = [
                project_root / rel
                for rel in managed_files
                if rel.replace("\\", "/").startswith(prefix)
            ]
            BaseIntegrator.cleanup_empty_parents(
                removed_paths, stop_at=project_root / effective_root
            )
        return stats

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _deploy_bundle(
        self,
        bundle: Path,
        extensions_dir: Path,
        project_root: Path,
        *,
        managed: set[str],
        force: bool,
        diagnostics,
        package_name: str,
        target_paths: list[Path],
    ) -> str:
        """Deploy one canvas bundle atomically.

        Returns one of ``"integrated"``, ``"adopted"`` or ``"skipped"``.
        The bundle is treated as a unit: all source files are planned and
        validated first, and any unmanaged collision skips the *whole*
        bundle (unless *force*) so a half-new/half-old executable extension
        is never produced.
        """
        name = bundle.name
        try:
            self._validate_canvas_name(name)
        except (PathTraversalError, ValueError) as exc:
            self._warn(diagnostics, f"Skipping canvas '{name}': {exc}", package_name)
            return "skipped"

        canvas_root = extensions_dir / name
        try:
            ensure_path_within(canvas_root.parent.resolve() / name, extensions_dir.resolve())
        except PathTraversalError as exc:
            self._warn(
                diagnostics, f"Rejected canvas target path for '{name}': {exc}", package_name
            )
            return "skipped"

        planned = self._plan_bundle_files(bundle, canvas_root, project_root, diagnostics, name)
        if planned is None:
            return "skipped"
        if not planned:
            # Bundle had only the marker filtered out / no copyable content.
            return "skipped"

        # A planned destination that already exists as a directory (or other
        # non-regular file) cannot be overwritten by ``shutil.copyfile`` --
        # even under ``--force``.  Treat it as an unsafe collision and skip
        # the whole bundle so we never crash mid-deploy and leave a
        # half-written executable extension behind.
        non_file = next(
            (rel for _src, dest, rel in planned if dest.exists() and not dest.is_file()),
            None,
        )
        if non_file is not None:
            self._warn(
                diagnostics,
                f"Skipping canvas '{name}' -- a directory exists at {non_file} "
                "where a file is expected; cannot overwrite safely.",
                package_name,
            )
            return "skipped"

        # Atomic collision pre-pass: a single unmanaged collision skips the
        # entire bundle unless force is set.
        collision = next(
            (
                rel
                for _src, dest, rel in planned
                if dest.exists() and rel not in managed and not force
            ),
            None,
        )
        if collision is not None:
            self._warn(
                diagnostics,
                f"Skipping canvas '{name}' -- local file exists at {collision} "
                "(not managed by APM). Use 'apm install --force' to overwrite.",
                package_name,
            )
            return "skipped"

        # Adopt when every planned file already exists byte-identical: keep
        # tracking the files in deployed_files without rewriting them.
        if all(self.is_content_identical_to_source(dest, src) for src, dest, _rel in planned):
            for _src, dest, _rel in planned:
                target_paths.append(dest)
            return "adopted"

        for src, dest, _rel in planned:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            target_paths.append(dest)
        return "integrated"

    def _plan_bundle_files(
        self,
        bundle: Path,
        canvas_root: Path,
        project_root: Path,
        diagnostics,
        name: str,
    ) -> list[tuple[Path, Path, str]] | None:
        """Walk *bundle* and return ``(src, dest, rel)`` triples to copy.

        Returns ``None`` when a containment / safety check fails (the whole
        bundle is then skipped by the caller).  Symlinks and the
        ``.apm-pin`` cache marker are excluded, mirroring
        ``security.gate.ignore_non_content``.
        """
        planned: list[tuple[Path, Path, str]] = []
        for src in sorted(bundle.rglob("*")):
            if src.is_symlink():
                continue
            if src.name == MARKER_FILENAME:
                continue
            if not src.is_file():
                continue
            rel_within = src.relative_to(bundle)
            dest = canvas_root / rel_within
            try:
                ensure_path_within(dest, canvas_root)
            except PathTraversalError as exc:
                self._warn(
                    diagnostics,
                    f"Rejected canvas file path in '{name}': {exc}",
                    "",
                )
                return None
            rel = portable_relpath(dest, project_root)
            planned.append((src, dest, rel))
        return planned

    def _emit_trust_block(
        self,
        bundles: list[Path],
        package_name: str,
        project_root: Path,
        mapping,
        target: TargetProfile,
        diagnostics,
    ) -> None:
        """Record a diagnostic explaining why dependency canvases were blocked."""
        if diagnostics is None:
            return
        effective_root = mapping.deploy_root or target.root_dir
        names = ", ".join(sorted(b.name for b in bundles))
        deploy_dir = f"{effective_root}/{mapping.subdir}/"
        pkg = package_name or "dependency"
        diagnostics.warn(
            message=(
                f"Blocked {len(bundles)} canvas extension(s) ({names}) from '{pkg}': "
                f"canvas extensions are executable {CANVAS_MARKER} code and are not "
                f"deployed from dependencies by default. Re-run with "
                f"'--trust-canvas-extensions' to deploy them to {deploy_dir}."
            ),
            package=pkg,
        )

    @staticmethod
    def _warn(diagnostics, message: str, package_name: str) -> None:
        """Emit a warning through diagnostics when available, else console."""
        if diagnostics is not None:
            diagnostics.warn(message=message, package=package_name or "")
        else:
            from apm_cli.utils.console import _rich_warning

            _rich_warning(message)
