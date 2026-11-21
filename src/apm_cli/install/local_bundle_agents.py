"""Render packed bundle agents for targets whose native agent format is not Markdown.

A bundle carries plugin-native Markdown agents. ``integrate_local_bundle``
copies them as packed for Markdown-agent targets; for a ``codex_agent``
mapping this module renders the agent with the agent integrator's Codex
renderer and deploys it through the integrator's render-then-adopt write
contract, so the comparison, the preview, the written bytes and the recorded
hash are all the same rendered text (#3714).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from ..utils.diagnostics import DiagnosticCollector

_RENDER_ERRORS = (OSError, UnicodeDecodeError, ValueError, TypeError)


def deploy_bundle_codex_agent(
    src: Path,
    dest: Path,
    *,
    record: str,
    deployed_files: list[str],
    deployed_hashes: dict[str, str],
    written_files: list[str],
    **kwargs: Any,
) -> int:
    """Deploy one packed agent as Codex TOML and record its rendered-hash claim.

    *kwargs* are the keyword-only arguments of :func:`render_and_deploy_codex_agent`.
    A real write also lands in *written_files*, the bundle's receipt evidence;
    an adopted file or a dry run does not. Returns the number of skipped files
    (``0`` or ``1``); a skipped agent records no claim.
    """
    deployed = render_and_deploy_codex_agent(src, dest, record=record, **kwargs)
    if deployed is None:
        return 1
    claim, wrote = deployed
    deployed_files.append(record)
    deployed_hashes[record] = claim
    if wrote:
        written_files.append(record)
    return 0


def render_and_deploy_codex_agent(
    src: Path,
    dest: Path,
    *,
    rel_path: str,
    record: str,
    collision_roots: tuple[Path, ...],
    expected_hash: str,
    target_name: str,
    slug: str,
    force: bool,
    dry_run: bool,
    diagnostics: DiagnosticCollector | None,
    logger: Any,
) -> tuple[str, bool] | None:
    """Render one packed Markdown agent to Codex TOML and deploy it.

    The packed bytes are checked against their ``pack.bundle_files`` digest,
    normalized as bundle admission normalizes it, and exactly those verified
    bytes are rendered; the source is never read again. The agent is rendered
    once, before any adopt or collision decision. A dry run previews the
    rendered hash and writes nothing.

    *record* is the lockfile claim (absolute at user scope). The collision
    check takes *dest* relative to the first of *collision_roots* that
    contains it, since the integrator resolves that key against *dest*.

    Returns:
        The ``sha256:<hex>`` hash of the rendered text and whether this call
        wrote the file, when the agent is written, adopted unchanged, or
        previewed; ``None`` when it is skipped
        (digest mismatch, render failure, a native field Codex rejects,
        unsafe destination or collision).
        A skipped agent writes nothing and must not be recorded as deployed.
    """
    from ..bundle.local_bundle import _normalize_hash
    from ..integration.agent_integrator import _SKIPPED, AgentIntegrator, CodexAgentFieldError
    from ..utils.atomic_io import normalize_crlf_to_lf
    from ..utils.diagnostics import DiagnosticCollector
    from ..utils.path_security import PathTraversalError

    def _warn(message: str) -> None:
        if diagnostics is not None:
            diagnostics.warn(message=message, package=slug)
        elif logger is not None:
            logger.warning(message)

    try:
        packed = src.read_bytes()
    except OSError as exc:
        _warn(f"Skipped packed bundle agent {rel_path!r}: it could not be read ({exc}).")
        return None
    try:
        expected_digest = _normalize_hash(str(expected_hash))
    except ValueError:
        expected_digest = None
    if hashlib.sha256(packed).hexdigest() != expected_digest:
        _warn(
            f"Skipped packed bundle agent {rel_path!r}: its bytes do not match "
            "pack.bundle_files. Repack the bundle, then rerun the install."
        )
        return None

    integrator = AgentIntegrator()
    collector = diagnostics if diagnostics is not None else DiagnosticCollector()
    try:
        try:
            rendered, _links = integrator._render_codex_agent(
                src,
                dest,
                diagnostics=collector,
                package_name=slug,
                resolve_links=integrator.resolve_links,
                content=_verified_text(packed),
                strict=True,
            )
        except CodexAgentFieldError as exc:
            _warn(
                f"Skipped packed bundle agent {rel_path!r} for target {target_name}: {exc}. "
                "Fix the agent source, repack the bundle, then rerun the install."
            )
            return None
        except _RENDER_ERRORS as exc:
            _warn(
                f"Skipped packed bundle agent {rel_path!r} for target {target_name}: "
                f"it could not be rendered to Codex TOML ({type(exc).__name__}). "
                "Fix the agent source, repack the bundle, then rerun the install."
            )
            return None
        rendered_hash = hashlib.sha256(normalize_crlf_to_lf(rendered).encode("utf-8")).hexdigest()
        if dry_run:
            if logger is not None:
                logger.verbose_detail(f"[dry-run] would deploy {record}")
            return f"sha256:{rendered_hash}", False
        try:
            outcome = integrator._deploy_rendered_agent(
                dest,
                src,
                _collision_key(dest, collision_roots),
                rendered,
                managed_files=None,
                force=force,
                diagnostics=collector,
                target_paths=[],
            )
        except (PathTraversalError, ValueError) as exc:
            _warn(f"Skipped unsafe bundle entry {rel_path!r}: {exc}")
            return None
        if outcome == _SKIPPED:
            return None
        if logger is not None:
            logger.verbose_detail(f"deployed {record}")
        return f"sha256:{rendered_hash}", bool(integrator.take_rendered_writes())
    finally:
        if collector is not diagnostics:
            _relay(collector, record, logger)


def _verified_text(packed: bytes) -> str:
    """Decode verified packed bytes as the renderer's own text read would."""
    return packed.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _collision_key(dest: Path, roots: tuple[Path, ...]) -> str:
    """Return *dest* relative to the first root that contains it."""
    from ..utils.paths import portable_relpath

    for root in roots:
        if dest.is_relative_to(root):
            return portable_relpath(dest, root)
    raise ValueError(f"bundle agent destination {dest} is outside its deploy roots")


def _relay(collector: DiagnosticCollector, record: str, logger: Any) -> None:
    """Surface integrator diagnostics through the bundle install logger."""
    from ..utils.diagnostics import CATEGORY_COLLISION

    if logger is None:
        return
    for category, items in collector.by_category().items():
        for item in items:
            if category == CATEGORY_COLLISION:
                logger.warning(
                    f"Skipped {record}: file exists with different content. "
                    "Re-run with --force to overwrite."
                )
            else:
                logger.warning(f"{item.message} {item.detail}".strip())
