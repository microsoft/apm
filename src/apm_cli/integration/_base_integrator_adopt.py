"""Adopt / collision helpers extracted from BaseIntegrator.

Contains the three methods that form the "adopt" cluster:
- ``is_content_identical_to_source`` -- TOCTOU-safe byte comparison
- ``try_adopt_identical`` -- single-call adopt-or-skip helper
- ``_check_adopt_or_skip`` -- combined adopt + collision check

These live in a mixin (``_AdoptMixin``) so BaseIntegrator can inherit
them without the 200-line block pushing base_integrator.py over the
800-line hard gate.  The mixin holds no state of its own; all attributes
it references through ``self`` (``_LF_NORMALIZED_DEPLOY``,
``check_collision``) are expected to be provided by the concrete class.
"""

from __future__ import annotations

from pathlib import Path

from apm_cli.utils.atomic_io import normalize_crlf_to_lf

from .base_integrator_io import _read_bytes_no_follow, _SymlinkRaceError


class _AdoptMixin:
    """Private mixin providing the adopt/collision check cluster."""

    @staticmethod
    def is_content_identical_to_source(
        target_path: Path,
        source_path: Path,
        *,
        lf_normalized_deploy: bool = False,
    ) -> bool:
        """Return True if *target_path* matches what APM would deploy from *source_path*.

        Used by non-skill integrators to silently *adopt* a pre-existing
        on-disk file that already matches what APM would deploy.

        The comparison depends on *how the calling integrator deploys*, which
        the caller declares via ``lf_normalized_deploy``:

        * ``lf_normalized_deploy=False`` (default) -- *byte-preserving*
          deployers (the hook, kiro-hook, and canvas integrators copy the
          source verbatim with ``shutil.copy2`` / ``shutil.copyfile``). The
          deployed bytes equal the source bytes exactly, so "identical" is
          raw byte identity.
        * ``lf_normalized_deploy=True`` -- *LF-normalizing* deployers (the
          agent, instruction, prompt, and command integrators write through
          ``write_text_lf``). The deployed bytes equal the *LF-normalized*
          source, so the on-disk *target* is identical iff it equals what
          ``write_text_lf`` would write. Without this, a package whose source
          carries CRLF line endings (Windows text-mode checkout,
          ``core.autocrlf``, or a text-mode write) would never match the LF
          target, so every reinstall would needlessly re-integrate the file
          and report it as freshly installed (apm#1916).

        In the LF-normalizing mode only the *source* side is normalized and
        there is no raw-byte short-circuit: a stale CRLF *target* left by a
        pre-LF install -- even one whose raw bytes happen to equal a CRLF
        source on a ``core.autocrlf`` checkout -- still mismatches the LF
        bytes ``write_text_lf`` would emit and is rewritten to LF rather than
        adopted (apm#1889).

        Why this exists
        ---------------
        Without this short-circuit, the per-file loops in
        ``agent_integrator``, ``instruction_integrator``, ``prompt_integrator``
        and ``command_integrator`` would route the file straight into
        :meth:`check_collision`. When the path is missing from
        ``managed_files`` (e.g. lockfile was wiped, hand-edited, regenerated
        by an older APM build, or the user's previous install crashed before
        ``deployed_files`` was persisted) the file is treated as
        "user-authored", *skipped*, and never appended to ``target_paths``.

        That in turn leaves ``deployed_files`` empty in the new lockfile,
        which trips the ``required-packages-deployed`` policy check at the
        next install. Because ``policy_gate`` runs *before* ``integrate``
        in ``pipeline.py``, the install can never self-heal -- a permanent
        catch-22 lockout.

        ``skill_integrator`` already has an equivalent content-identity
        adopt at ``_promote_sub_skills`` (target.exists() +
        ``_dirs_equal``). This helper restores symmetry for non-skill
        primitives.

        Conservative by design
        ----------------------
        Only fires when the deployed bytes match the source content (modulo
        CRLF->LF normalization). Format-transforming targets
        (``codex_agent``, ``cursor_rules``, ``claude_rules``,
        ``windsurf_rules``, ``gemini_command``, ...) won't match -- they
        keep the existing skip behavior. This means we never silently
        adopt content that *might* have come from somewhere else; we only
        adopt files that are demonstrably the package's own content already
        on disk.

        TOCTOU hardening
        ----------------
        The classic ``is_symlink()`` -> ``read_bytes()`` sequence has a
        race window: a hostile co-tenant on the same machine could swap
        a regular file for a symlink between the two calls and cause
        ``read_bytes()`` to follow it to an attacker-controlled path,
        whose bytes might match source by construction. We close the
        race by reading through ``os.open(..., O_NOFOLLOW)`` so the
        kernel rejects the open atomically if the final component is a
        symlink. ``O_NOFOLLOW`` is a no-op constant on platforms that
        lack it (Windows), where the upfront ``is_symlink()`` check
        plus ``ensure_path_within`` at the call site provide adequate
        coverage (Windows also lacks the cheap unprivileged-symlink
        creation primitive that makes this race practical on POSIX).
        """
        try:
            if not target_path.exists() or not source_path.exists():
                return False
            # Cheap pre-check: reject obvious symlinks so we never even
            # attempt the open. Race-free verification follows below via
            # O_NOFOLLOW.
            if target_path.is_symlink() or source_path.is_symlink():
                return False
            try:
                target_bytes = _read_bytes_no_follow(target_path)
                source_bytes = _read_bytes_no_follow(source_path)
            except _SymlinkRaceError:
                # The path turned into a symlink between the pre-check
                # and the open() -- treat as non-identical so the caller
                # falls through to ``check_collision`` and the file is
                # NOT adopted. Silent (no diagnostic) because adopt is
                # an optimisation; the user-authored skip path is the
                # safe fallback.
                return False
            if not lf_normalized_deploy:
                # Byte-preserving deployer: deployed bytes equal source
                # bytes verbatim, so "identical" is raw byte identity.
                return target_bytes == source_bytes
            # LF-normalizing deployer: the target is identical iff it equals
            # what ``write_text_lf`` would write -- the LF-normalized source.
            # No raw-byte short-circuit: a stale CRLF target whose raw bytes
            # match a CRLF source must still be rewritten to LF, not adopted
            # (apm#1889).
            try:
                normalized_source = normalize_crlf_to_lf(source_bytes.decode("utf-8")).encode(
                    "utf-8"
                )
            except UnicodeDecodeError:
                # Non-UTF-8 source can't be line-normalized and
                # ``write_text_lf`` could not have produced it; fall back to
                # raw byte identity.
                return target_bytes == source_bytes
            return target_bytes == normalized_source
        except OSError:
            return False

    @staticmethod
    def try_adopt_identical(
        target_path: Path,
        source_path: Path,
        target_paths: list,
        *,
        lf_normalized_deploy: bool = False,
    ) -> bool:
        """Adopt *target_path* when it is identical to what would be deployed.

        Encapsulates the ``is_content_identical_to_source`` + append pattern
        so secondary call sites in agent/prompt/hook integrators share a
        single predicate call instead of repeating the three-line block.

        ``lf_normalized_deploy`` is forwarded to
        :meth:`is_content_identical_to_source`: LF-normalizing deployers
        (agent, prompt) pass ``True``; byte-preserving deployers (hook,
        kiro-hook) keep the ``False`` default.

        Returns ``True`` and appends *target_path* to *target_paths* when the
        files are identical; returns ``False`` and leaves *target_paths*
        unchanged otherwise.
        """
        if _AdoptMixin.is_content_identical_to_source(
            target_path, source_path, lf_normalized_deploy=lf_normalized_deploy
        ):
            target_paths.append(target_path)
            return True
        return False

    def _check_adopt_or_skip(
        self,
        target_path: Path,
        source_file: Path,
        rel_path: str,
        managed_files: set[str] | None,
        force: bool,
        diagnostics,
        target_paths: list,
    ) -> tuple[bool, bool]:
        """Check whether *target_path* should be adopted or skipped.

        Combines :meth:`is_content_identical_to_source` (adopt) and
        :meth:`check_collision` (skip) into a single call so integrators
        share the decision logic without code duplication.

        When adopting, *target_path* is appended to *target_paths* as a
        side effect so the caller's bookkeeping stays correct.

        The adopt comparison runs in this integrator's deploy mode
        (:attr:`_LF_NORMALIZED_DEPLOY`): LF-normalizing integrators compare
        the on-disk target against the LF-normalized source, while
        byte-preserving integrators compare raw bytes.

        Args:
            target_path: Destination path on disk.
            source_file: Source file to compare against; the file is adopted
                when the on-disk target already matches the deployed form of
                this source.
            rel_path: Relative path string used for collision detection and
                diagnostics.
            managed_files: Set of APM-managed relative paths; ``None`` means
                none managed.
            force: When ``True``, collisions are silently overwritten.
            diagnostics: Optional diagnostics collector; forwarded to
                :meth:`check_collision`.
            target_paths: Mutable list; *target_path* is appended on adopt.

        Returns:
            ``(skip, adopted)`` -- when ``skip`` is ``True`` the caller must
            ``continue`` (or otherwise skip writing this file); ``adopted``
            is ``True`` only when the existing file already matched the
            deployed content and has been silently adopted.
        """
        if self.is_content_identical_to_source(  # type: ignore[attr-defined]
            target_path,
            source_file,
            lf_normalized_deploy=self._LF_NORMALIZED_DEPLOY,  # type: ignore[attr-defined]
        ):
            target_paths.append(target_path)
            return True, True
        if self.check_collision(  # type: ignore[attr-defined]
            target_path, rel_path, managed_files, force, diagnostics=diagnostics
        ):
            return True, False
        return False, False
