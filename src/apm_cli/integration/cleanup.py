"""Shared cleanup helper for stale deployed files.

Used by the post-install cleanup blocks in :mod:`apm_cli.commands.install`
to remove files previously deployed for a still-present package that the
current install no longer produces (e.g. after a rename or removal inside
the package). Centralises the safety gates so both the local-package and
remote-package cleanup paths apply the same rules.

Safety gates, in order:

1. **Path validation** -- :meth:`BaseIntegrator.validate_deploy_path` rejects
   path traversal and any path not under a known integration prefix.
2. **Directory handling** -- APM-managed primitives are file-keyed
   (``SKILL.md`` for skills, individual ``.prompt.md`` / ``.instructions.md``
   files elsewhere). Directory entries that match a known skill directory
   pattern (``<prefix>/skills/<name>``) are deferred to a second pass so
   individual files are removed first; if the directory is then empty (or
   only contains APM-tracked files with matching hashes) it is safely
   removed. Non-skill directory entries are still rejected as untrusted.
3. **Deployment evidence** -- a file is deleted only when
   :class:`DeploymentEvidence` proves APM deployed its current bytes: a
   matching local deployment receipt, or byte equality with what the
   locked package versions render. A lockfile hash alone never authorizes
   deletion (#3663, #3701): it proves content, not that APM wrote it. A
   file that differs from its evidence is user-edited and kept; a file
   without evidence is kept with a warning naming it and the manual fix.
   ``cowork://`` rows follow the same rule. The one exception is explicit
   per-invocation consent (``apm uninstall --trust-lockfile``, #4295, the
   req-lk-024 consent exception): a file with no receipt and no producible
   render may be deleted when its bytes equal the single hash apm.lock.yaml
   records for it; it never overrides evidence that a file differs.

The helper records cleanup diagnostics via *diagnostics* (collect-then-
render) and returns a :class:`CleanupResult` summarizing deleted, failed,
and skipped paths. Callers remain responsible for any informational,
progress, or warning logging based on that result -- the helper itself
takes no logger.
"""

from __future__ import annotations

import contextlib
import copy
import errno
import io
import os
import posixpath
import shlex
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.core.remediation import (
    COWORK_SKILLS_DIR_UNDETECTED,
    KEPT_DELETE_MANUALLY,
    UNINSTALL_EVIDENCE_REINSTALL,
    UNINSTALL_EVIDENCE_REMOVE,
    UNINSTALL_EVIDENCE_TRUST_LOCKFILE,
    kept_note,
)
from apm_cli.integration import write_seam
from apm_cli.security.gate import is_python_bytecode_cache_path
from apm_cli.utils.paths import SpellingAliasIndex

from .base_integrator import BaseIntegrator

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.targets import TargetProfile

AUTHORIZED = "authorized"
EDITED = "edited"
UNPROVEN = "unproven"
UNREADABLE = "unreadable"
# A member of a claimed directory replaced by a symlink: cleanup keeps the directory (#4451).
SYMLINKED = "symlinked"

# Reasons a path-safety gate refuses a path at cleanup time (#4815).
REJECTED_TRAVERSAL = "its path contains a '..' component"
REJECTED_PREFIX = "it is not under a known integration prefix"
REJECTED_UNSAFE_PATH = (
    "it failed the path safety check: it escapes the deploy root, is not under "
    "a managed prefix, or is reached through a symbolic link"
)
REJECTED_DIRECTORY = "it is a directory entry, and APM only deletes individual files"
REJECTED_REPLACED = "it was replaced or moved since APM checked it"


class DeploymentEvidence:
    """Proof that APM deployed the bytes a lockfile-driven deletion removes (#3701).

    Deletion is authorized by a local deployment receipt whose hash matches
    the file, or by byte equality (the canonical hash domain of req-lk-012)
    with what the locked package versions render. A stale receipt does not
    block the render comparison. The render replays the pre-transition lock
    once, with the targets it recorded; when that fails, each owning package
    renders alone, so one unmaterializable package costs only its own
    evidence. Lockfile hashes are never consulted: on a clone they prove
    content, not deployment.
    """

    def __init__(
        self,
        *,
        deploy_root: Path,
        receipts: Mapping[str, str],
        render: Callable[[str | None], Path | None] | None,
        owners: Mapping[str, tuple[str, ...]] | None = None,
        default_owners: Mapping[str, tuple[str, ...]] | None = None,
        user_scope: bool = False,
        kept: Iterable[str],
        recorded_hashes: Mapping[str, frozenset[str]] | None = None,
        receipts_readable: bool = True,
        locked: LockFile | None = None,
        owned_receipts: Callable[[str], Mapping[str, str]] | None = None,
    ) -> None:
        self._deploy_root = deploy_root
        self._roots = tuple(dict.fromkeys((deploy_root, deploy_root.resolve())))
        self._locked = locked
        self._legacy_claude_rules: dict[Path, dict[str, str]] = {}
        self._owned_receipts = owned_receipts
        self.user_scope = user_scope
        self._kept: frozenset[str] = frozenset()
        self._kept_parents: frozenset[str] = frozenset()
        self.retain(kept)
        self._receipts = dict(receipts)
        self._render = render
        # The no-flag render lookup stays the first claimed owner up the tree;
        # only consent eligibility pools every owner (#4295).
        self._default_owners = dict((owners if default_owners is None else default_owners) or {})
        # One spelling per path, as claims are looked up; spellings of one
        # path pool their owners so none is dropped (#4295).
        self._owners: dict[str, tuple[str, ...]] = {}
        for path, keys in (owners or {}).items():
            key = self._key(path)
            self._owners[key] = tuple(dict.fromkeys((*self._owners.get(key, ()), *keys)))
        self._renders: dict[str | None, Path | None] = {}
        self._rendered_now: dict[str, str] = {}
        self._recorded_hashes: dict[str, frozenset[str]] = {}
        for path, values in (recorded_hashes or {}).items():
            key = self._key(path)
            self._recorded_hashes[key] = self._recorded_hashes.get(key, frozenset()) | values
        self._receipts_readable = receipts_readable
        self._lockfile_consent = False
        self._consented: dict[str, None] = {}
        self._spellings: SpellingAliasIndex | None = None
        # Cowork claims by their path under the resolved Cowork root (#4815).
        self._cowork_spellings: tuple[SpellingAliasIndex, dict[str, str], Path] | None = None
        self._receipt_spellings: SpellingAliasIndex | None = None
        self._consent_refused: dict[str, None] = {}
        self._reported: set[str] = set()

    @classmethod
    def none(cls, deploy_root: Path) -> DeploymentEvidence:
        """Evidence that authorizes nothing: every lockfile-listed file is kept."""
        return cls(deploy_root=deploy_root, receipts={}, render=None, kept=())

    @classmethod
    def collect(
        cls,
        deploy_root: Path,
        *,
        user_scope: bool,
        lockfile: LockFile | None,
        lockfile_root: Path,
        diagnostics,
        kept: Iterable[str],
    ) -> DeploymentEvidence:
        """Load this installation's receipts and bind a lazy render of *lockfile*.

        *lockfile* must be the lock before this transition (the versions whose
        files may be deleted); *lockfile_root* is the directory holding it.
        Invalid receipt state authorizes nothing and is reported. *kept* names
        the members this install declared but left on disk; they are retained.
        """
        from apm_cli.core.deployment_ledger import (
            DeploymentLedgerCodec,
            DeploymentMaterializationSession,
        )
        from apm_cli.core.scope import InstallScope

        scope = InstallScope.USER if user_scope else InstallScope.PROJECT
        receipts_readable = True
        owned_receipts = None
        try:
            session = DeploymentMaterializationSession(deploy_root, scope, None)
            receipts = session.receipt_hashes()
            owned_receipts = session.receipt_hashes
            # A store no installation marker binds went unread: never empty (#4295).
            receipts_readable = session.identity_path.exists() or not session.state_path.exists()
        except (OSError, ValueError) as exc:
            diagnostics.warn(f"Ignored local deployment receipts: {exc}")
            receipts = {}
            receipts_readable = False
        if lockfile is None or not lockfile.dependencies:
            return cls(
                deploy_root=deploy_root,
                receipts=receipts,
                render=None,
                user_scope=user_scope,
                kept=kept,
                receipts_readable=receipts_readable,
                owned_receipts=owned_receipts,
            )
        # Freeze the pre-transition lock: callers mutate theirs before rendering.
        locked = copy.deepcopy(lockfile)
        owners: dict[str, list[str]] = {}
        for key, dependency in locked.dependencies.items():
            for path in dependency.deployed_files:
                owners.setdefault(path.rstrip("/"), []).append(key)
        default_owners = {path: tuple(keys) for path, keys in owners.items()}
        # Canonical deployment rows name owners the legacy lists may omit.
        for record in DeploymentLedgerCodec.from_lockfile(locked).records.values():
            for key in record.owners:
                if key in locked.dependencies:
                    owners.setdefault(record.locator.value, []).append(key)

        def render(owner: str | None) -> Path | None:
            return _render_locked(locked, owner, lockfile_root, user_scope, diagnostics)

        return cls(
            deploy_root=deploy_root,
            receipts=receipts,
            render=render,
            owners={path: tuple(keys) for path, keys in owners.items()},
            default_owners=default_owners,
            user_scope=user_scope,
            kept=kept,
            recorded_hashes=_recorded_owner_hashes(locked),
            receipts_readable=receipts_readable,
            locked=locked,
            owned_receipts=owned_receipts,
        )

    def receipted_claims(self, owner: str, targets: Iterable[TargetProfile]) -> dict[str, str]:
        """Map each lock value this installation receipted for *owner* to the hash APM wrote.

        Stands in for a package's prior ``deployed_files`` and hashes when the
        lock has no record of it (for example after the lockfile was lost), so
        stale-file cleanup still retires what the package no longer deploys.
        Each receipt is spelled by the canonical deployment-path codec for
        *targets* (``cowork://`` for an external root), as current claims are;
        a receipt no target can spell is left to the lock. Deletion stays
        gated by :meth:`verdict`.
        """
        from apm_cli.install.deployed_paths import deployed_path_entry
        from apm_cli.utils.path_security import PathTraversalError

        if self._owned_receipts is None:
            return {}
        targets = list(targets)
        claims: dict[str, str] = {}
        for physical, content_hash in sorted(self._owned_receipts(owner).items()):
            try:
                value = deployed_path_entry(Path(physical), self._deploy_root, targets)
            except (PathTraversalError, RuntimeError, ValueError):
                continue
            claims[value] = content_hash
        return claims

    def add_rendered(self, rendered: Mapping[Path, str]) -> None:
        """Add hashes this install rendered from the source it holds (#4360).

        A local bundle has no locked source to replay; the bytes this install
        just deployed from it are its render, so a file equal to them is proven.
        """
        self._rendered_now.update(
            {path.resolve().as_posix(): content_hash for path, content_hash in rendered.items()}
        )

    def _rendered_now_match(self, physical: Path, candidates: Iterable[str | None]) -> bool:
        from apm_cli.utils.content_hash import compute_file_hash

        for candidate in candidates:
            path = self._physical(candidate) if candidate else None
            rendered = self._rendered_now.get(path.resolve().as_posix()) if path else None
            if rendered is not None and compute_file_hash(physical) == rendered:
                return True
        return False

    def retain(self, kept: Iterable[str]) -> None:
        """Adopt this run's skip set; evidence built before integration learns it late."""
        values = frozenset(value.replace("\\", "/").rstrip("/") for value in kept)
        if values == self._kept:
            return
        self._kept = values
        # Every ancestor directory of a kept member, so each lookup stays O(1).
        self._kept_parents = frozenset(
            "/".join(parts[:index])
            for parts in (value.split("/") for value in values)
            for index in range(1, len(parts))
        )

    def kept(self, value: str) -> bool:
        """Return whether this install kept *value*, or a member under that directory."""
        value = value.replace("\\", "/").rstrip("/")
        return value in self._kept or value in self._kept_parents

    @property
    def receipt_hashes(self) -> dict[str, str]:
        """Map each receipted resolved physical path to the hash APM wrote there."""
        return dict(self._receipts)

    def prepare(self, claims: Iterable[str]) -> None:
        """Render now when any existing candidate file lacks a matching receipt.

        Uninstall and prune call this before removing package sources, so the
        locked render can still read them.
        """
        if self._render is None:
            return
        unproven: list[str] = []
        for claim in claims:
            physical = self._physical(claim)
            if physical is None or physical.is_symlink() or not physical.is_file():
                continue
            try:
                if self._receipt_match(physical) is True:
                    continue
            except OSError:
                continue
            unproven.append(claim)
        for claim in unproven:
            self._rendered_roots(claim, pooled=True)

    def refusals(self, claims: Iterable[str]) -> list[tuple[str, str]]:
        """Return ``(claim, verdict)`` for each claimed file deletion would keep.

        Lets a command refuse before it changes anything when a file it would
        delete has no deployment evidence (#3701). Missing files, directories
        and final symlinks are skipped; their own rules apply.
        """
        claims = list(dict.fromkeys(claims))
        directories = [
            claim.rstrip("/")
            for claim in claims
            if (physical := self._physical(claim)) is not None
            and not physical.is_symlink()
            and physical.is_dir()
        ]
        refused: list[tuple[str, str]] = []
        for claim in claims:
            physical = self._physical(claim)
            if physical is None:
                continue
            if physical.is_symlink():
                # Only a member of a claimed directory: managed final symlinks
                # elsewhere keep their own unlink rule (#4451). Under consent the
                # exception never covers a link, so it is kept and named (#4295).
                if self._lockfile_consent or any(
                    claim.startswith(f"{directory}/") for directory in directories
                ):
                    refused.append((claim, SYMLINKED))
                    if self._lockfile_consent:
                        self._consent_refused[_claim_key(claim)] = None
                continue
            if not physical.is_file():
                continue
            verdict = self.verdict(claim, physical)
            if verdict != AUTHORIZED:
                refused.append((claim, verdict))
        return refused

    def _physical(self, claim: str) -> Path | None:
        from .copilot_cowork_paths import (
            COWORK_URI_SCHEME,
            cowork_locator,
            resolve_copilot_cowork_skills_dir,
        )

        if claim.startswith(COWORK_URI_SCHEME):
            try:
                root = resolve_copilot_cowork_skills_dir()
                # The locator itself: a final symlink is judged as a link.
                return cowork_locator(claim, root) if root is not None else None
            except Exception:
                return None
        if "://" in claim:
            return None
        path = Path(claim)
        return path if path.is_absolute() else self._deploy_root / path

    def _render_root(self, owner: str | None) -> Path | None:
        if owner not in self._renders:
            self._renders[owner] = self._render(owner) if self._render is not None else None
        return self._renders[owner]

    def _key(self, path: str) -> str:
        """The consent lookup key for *path*: one spelling, root-relative (#4295).

        A user-scope lock may spell a file relative to the deploy root or as
        an absolute path under it; both name one file and share one key.
        """
        from .copilot_cowork_paths import COWORK_URI_SCHEME

        key = _claim_key(path)
        if key.startswith(COWORK_URI_SCHEME):
            # A Cowork root inside the deploy root names the same files twice.
            physical = self._physical(key)
            if physical is None:
                return key
            candidate = physical
        elif "://" in key or not Path(key).is_absolute():
            return key
        else:
            candidate = Path(key)
        for root in self._roots:
            try:
                return _claim_key(candidate.relative_to(root).as_posix())
            except ValueError:
                continue
        return key

    def _equivalent_keys(self, key: str) -> tuple[str, ...]:
        """*key* and every recorded key naming the same file on this filesystem (#4295).

        Case- or normalization-insensitive filesystems make ``A.md`` and
        ``a.md`` one file; case-sensitive ones never alias.
        """
        from .copilot_cowork_paths import COWORK_URI_SCHEME

        if key.startswith(COWORK_URI_SCHEME):
            return (key, *self._cowork_aliases(key))
        if self._spellings is None:
            self._spellings = SpellingAliasIndex({*self._recorded_hashes, *self._owners})
        return (key, *self._spellings.aliases(key, self._deploy_root))

    def _cowork_aliases(self, key: str) -> tuple[str, ...]:
        """Recorded ``cowork://`` keys naming the same file as *key* under the Cowork root."""
        from .copilot_cowork_paths import COWORK_URI_SCHEME, resolve_copilot_cowork_skills_dir

        if self._cowork_spellings is None:
            try:
                root = resolve_copilot_cowork_skills_dir()
            except Exception:
                root = None
            if root is None:
                return ()
            claims = {
                claim[len(COWORK_URI_SCHEME) :].removeprefix("skills/"): claim
                for claim in {*self._recorded_hashes, *self._owners}
                if claim.startswith(COWORK_URI_SCHEME)
            }
            self._cowork_spellings = (SpellingAliasIndex(claims), claims, root)
        index, claims, root = self._cowork_spellings
        relative = key[len(COWORK_URI_SCHEME) :].removeprefix("skills/")
        return tuple(claims[alias] for alias in index.aliases(relative, root))

    def _owners_of(self, claim: str) -> tuple[str, ...]:
        """Return the locked owners of *claim* and of every claimed enclosing directory.

        A member claim never hides its skill directory's owners: each render
        that can show the file is checked before consent falls back (#4295).
        """
        owners: dict[str, None] = {}
        path = self._key(claim)
        while path:
            for key in self._equivalent_keys(path):
                owners.update(dict.fromkeys(self._owners.get(key, ())))
            path = path.rpartition("/")[0]
        return tuple(owners)

    def _default_owners_of(self, claim: str) -> tuple[str, ...]:
        """Return the locked owners of *claim* or of its claimed skill directory."""
        path = claim.rstrip("/")
        while path:
            if path in self._default_owners:
                return self._default_owners[path]
            path = path.rpartition("/")[0]
        return ()

    def _rendered_roots(self, claim: str, *, pooled: bool = False) -> list[Path]:
        """Return the render trees that can show *claim*: the whole lock, else its owners.

        The deletion verdict reads the first claimed owner up the tree;
        *pooled* (consent eligibility) reads every owner of every spelling.
        """
        if self._render is None:
            return []
        whole = self._render_root(None)
        if whole is not None:
            return [whole]
        owners = self._owners_of(claim) if pooled else self._default_owners_of(claim)
        roots = (self._render_root(owner) for owner in owners)
        return [root for root in roots if root is not None]

    def _has_receipt(self, physical: Path) -> bool:
        """Whether a receipt names *physical* under any spelling of it (#4295)."""
        key = physical.resolve().as_posix()
        if key in self._receipts:
            return True
        if self._receipt_spellings is None:
            self._receipt_spellings = SpellingAliasIndex(self._receipts)
        # Receipt keys are absolute, so the join root is ignored.
        return bool(self._receipt_spellings.aliases(key, Path("/")))

    def _receipt_match(self, physical: Path) -> bool | None:
        from apm_cli.utils.content_hash import compute_file_hash

        recorded = self._receipts.get(physical.resolve().as_posix())
        if recorded is None:
            return None
        return compute_file_hash(physical) == recorded

    def _rendered_paths(self, claim: str, owner_claim: str) -> list[Path]:
        relative: Path | None
        if "://" in claim:
            from apm_cli.install.audit_target_roots import external_replay_root
            from apm_cli.integration.targets import KNOWN_TARGETS, target_name_for_locator

            name = target_name_for_locator(claim)
            if name is None:
                return []
            return [
                external_replay_root(root, KNOWN_TARGETS[name]) / claim.split("://", 1)[1]
                for root in self._rendered_roots(owner_claim)
            ]
        relative = Path(claim)
        if relative.is_absolute():
            try:
                relative = relative.relative_to(self._deploy_root)
            except ValueError:
                return []
        return [root / relative for root in self._rendered_roots(owner_claim)]

    def consent_to_lockfile_hashes(self) -> None:
        """Accept, for this uninstall only, the req-lk-024 consent exception (#4295)."""
        self._lockfile_consent = True

    @property
    def lockfile_consent(self) -> bool:
        """Whether this uninstall carries ``--trust-lockfile`` consent (#4295)."""
        return self._lockfile_consent

    def consented_deletions(self) -> list[str]:
        """Claims ``--trust-lockfile`` consent authorized that are now gone from disk."""
        return [
            claim
            for claim in self._consented
            if (physical := self._physical(claim)) is not None and not os.path.lexists(physical)
        ]

    def take_consented_deletions(self) -> list[str]:
        """Return the consented deletions not yet reported, and mark them reported."""
        fresh = [claim for claim in self.consented_deletions() if claim not in self._reported]
        self._reported.update(fresh)
        return fresh

    def consent_kept_link(self, claim: str, physical: Path) -> bool:
        """Whether consent keeps *physical* because it is now a symbolic link (#4295).

        Judged on the current file kind, whatever the earlier verdict: a file
        replaced by a link after preflight is kept too (#4815), and recorded as
        kept so the uninstall does not report it as a failure.
        """
        if not _consent_keeps_link(physical, lockfile_consent=self._lockfile_consent):
            return False
        self._consent_refused[_claim_key(claim)] = None
        return True

    def consent_keep_noncanonical(self, claim: str) -> bool:
        """Whether consent keeps a claim recorded with a ``.`` segment (#5096).

        Nothing is written or removed through a non-canonical spelling, so
        under ``--trust-lockfile`` the file stays and is recorded as kept.
        """
        if not self._lockfile_consent:
            return False
        self._consent_refused[_claim_key(claim)] = None
        return True

    def kept_under_consent(self) -> frozenset[str]:
        """Claims a deletion decision kept, despite ``--trust-lockfile`` consent, for their evidence."""
        return frozenset(self._consent_refused)

    def lockfile_consentable(self, claim: str, physical: Path) -> bool:
        """Return whether ``--trust-lockfile`` consent may delete *physical* (#4295).

        The single consent decision: receipt state was read without error and
        holds no receipt for the file, no render of its owning locked package
        could be produced, and its bytes equal the one distinct hash
        apm.lock.yaml records for *claim* across its owners (bare hex reads as
        ``sha256``, req-lk-016). Any contrary or missing evidence (a differing
        receipt or render, a render lacking the path, no or conflicting
        recorded hashes, unreadable, not a regular file, symlinked) keeps it.
        """
        from apm_cli.utils.content_hash import compute_file_hash

        recorded = {
            _hash_envelope(value)
            for key in self._equivalent_keys(self._key(claim))
            for value in self._recorded_hashes.get(key, frozenset())
        }
        if not self._receipts_readable or len(recorded) != 1:
            return False
        try:
            if physical.is_symlink() or not physical.is_file():
                return False
            if self._has_receipt(physical) or self._rendered_roots(claim, pooled=True):
                return False
            return compute_file_hash(physical) in recorded
        except OSError:
            return False

    def consent_preview(self, claims: Iterable[str]) -> frozenset[str]:
        """Return the *claims* ``--trust-lockfile`` consent would delete (#4295)."""
        return frozenset(
            claim
            for claim in claims
            if (physical := self._physical(claim)) is not None
            and self.lockfile_consentable(claim, physical)
        )

    def verdict(self, claim: str, physical: Path, *, rendered_as: str | None = None) -> str:
        """Classify deleting *physical* (lockfile *claim*) as authorized or kept.

        *rendered_as* names where the locked render may place the same member
        instead (skill path migration renders the converged layout). Under
        ``--trust-lockfile`` consent every kept claim is recorded, so the
        command fails only for deletions that were authorized but failed.
        """
        verdict = self._verdict(claim, physical, rendered_as=rendered_as)
        if verdict != AUTHORIZED and self._lockfile_consent:
            self._consent_refused[_claim_key(claim)] = None
        return verdict

    def _verdict(self, claim: str, physical: Path, *, rendered_as: str | None) -> str:
        try:
            receipt = self._receipt_match(physical)
            if receipt is True:
                return AUTHORIZED
        except OSError:
            return UNREADABLE
        # A stale receipt (the file was later adopted unchanged from a newer
        # render) does not block the render comparison: receipt OR render.
        rendered = self.render_verdict(claim, physical, rendered_as=rendered_as)
        if rendered == UNPROVEN and receipt is False:
            return EDITED
        # Rechecked at each decision, so the bytes removed are the bytes consented.
        if (
            rendered == UNPROVEN
            and self._lockfile_consent
            and self.lockfile_consentable(claim, physical)
        ):
            self._consented[self._key(claim)] = None
            return AUTHORIZED
        return rendered

    def render_verdict(self, claim: str, physical: Path, *, rendered_as: str | None) -> str:
        """Classify *physical* against what this install and the locked versions render.

        Receipts are not consulted: an overwrite takes receipts only from the
        lock-bound session (#4360).
        """
        from apm_cli.utils.content_hash import compute_file_hash

        try:
            if self._rendered_now_match(physical, (claim, rendered_as)):
                return AUTHORIZED
            claim = claim.replace("\\", "/")
            rendered = [
                path
                for candidate in (claim, rendered_as)
                if candidate
                for path in self._rendered_paths(candidate, claim)
                if not path.is_symlink() and path.is_file()
            ]
            if not rendered:
                return self._legacy_claude_rule_verdict(claim, physical)
            actual = compute_file_hash(physical)
            same = any(compute_file_hash(path) == actual for path in rendered)
        except OSError:
            return UNREADABLE
        return AUTHORIZED if same else EDITED

    def _legacy_claude_rule_verdict(self, claim: str, physical: Path) -> str:
        """Prove a ``.claude/rules/`` file a pre-#3939 install deployed.

        The locked render routes always-on and directory instructions to
        AGENTS.md, so it never writes their old rule files; render those
        through the legacy projection of the same locked source instead.
        """
        if self.user_scope or self._locked is None or not claim.startswith(".claude/rules/"):
            return UNPROVEN
        expected = [
            text
            for root in self._rendered_roots(claim)
            for rendered in self._legacy_claude_rule_renders(root).values()
            if (text := rendered.get(claim)) is not None
        ]
        if not expected:
            return UNPROVEN
        actual = physical.read_bytes()
        return AUTHORIZED if any(text.encode("utf-8") == actual for text in expected) else EDITED

    def _legacy_claude_rule_renders(self, root: Path) -> dict[str, dict[str, str]]:
        if root not in self._legacy_claude_rules:
            from apm_cli.integration.instruction_integrator import routed_claude_rule_renders

            self._legacy_claude_rules[root] = routed_claude_rule_renders(
                self._locked, self._deploy_root, root / "apm_modules", include_local=True
            )
        return self._legacy_claude_rules[root]

    def authorizes(self, claim: str, physical: Path, *, rendered_as: str | None = None) -> bool:
        """Return whether every file at *physical*, a file or a tree, is proven APM's."""
        if physical.is_symlink():
            return False
        if physical.is_file():
            return self.verdict(claim, physical, rendered_as=rendered_as) == AUTHORIZED
        for child in physical.rglob("*"):
            relative = child.relative_to(physical)
            if child.is_symlink():
                return False
            if child.is_dir() or is_python_bytecode_cache_path(relative):
                continue
            member = relative.as_posix()
            alias = f"{rendered_as}/{member}" if rendered_as else None
            if self.verdict(f"{claim}/{member}", child, rendered_as=alias) != AUTHORIZED:
                return False
        return True


def unconsented(retained: Iterable[str], evidence: DeploymentEvidence | None) -> list[str]:
    """Drop from *retained* what --trust-lockfile consent kept for its evidence (#4295).

    A file consent keeps is not a cleanup failure; every other retained path is.
    Callers must not pass a path-safety rejection here: see :func:`consent_outcome`.
    """
    kept = evidence.kept_under_consent() if evidence is not None else frozenset()
    return [path for path in retained if _claim_key(path) not in kept]


def consent_outcome(
    cleanup: CleanupResult, evidence: DeploymentEvidence | None
) -> tuple[list[str], list[str]]:
    """Split *cleanup* into its failures and its path-safety refusals (#4295).

    Consent waives only evidence-based retention: a deletion that was
    authorized but failed stays a failure even for a claim consent kept
    earlier (for example in the uninstall preflight), and a file consent kept
    for its evidence is not a path-safety refusal. A path this cleanup's
    safety gates rejected is always both, whatever consent kept earlier (#4815).
    """
    rejected = list(cleanup.rejected)
    failures = [*cleanup.failed, *rejected, *unconsented(cleanup.retained, evidence)]
    unsafe = [*rejected, *unconsented(cleanup.skipped_unmanaged, evidence)]
    return list(dict.fromkeys(failures)), list(dict.fromkeys(unsafe))


def _hash_envelope(value: str) -> str:
    """Return *value* as an ``<algo>:<hex>`` envelope; bare hex reads as sha256 (req-lk-016)."""
    value = value.strip().lower()
    return value if ":" in value else f"sha256:{value}"


def _claim_key(path: str) -> str:
    """One spelling per claimed file for consent's hash, owner and refusal keys (#4295).

    Backslashes become ``/`` and ``.`` segments and trailing slashes go, so
    every spelling the cleanup validation accepts for a file shares one key.
    """
    scheme, sep, rest = path.replace("\\", "/").rpartition("://")
    normalized = posixpath.normpath(rest) if rest else rest
    return f"{scheme}{sep}{'' if normalized == '.' else normalized}"


def _recorded_owner_hashes(lockfile: LockFile) -> dict[str, frozenset[str]]:
    """Map each owned path to every hash apm.lock.yaml records for it under its owners."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    owners = set(lockfile.dependencies)
    hashes: dict[str, set[str]] = {}
    for record in DeploymentLedgerCodec.from_lockfile(lockfile).records.values():
        if record.content_hash and owners.intersection(record.owners):
            hashes.setdefault(_claim_key(record.locator.value), set()).add(record.content_hash)
    for dependency in lockfile.dependencies.values():
        for path, content_hash in dependency.deployed_file_hashes.items():
            if content_hash:
                # One spelling per file, as cleanup looks it up (#4295).
                hashes.setdefault(_claim_key(path), set()).add(content_hash)
    return {path: frozenset(values) for path, values in hashes.items()}


def _locked_subset(lockfile: LockFile, owner: str) -> LockFile:
    """Return *lockfile* narrowed to *owner* and the local parents anchoring it."""
    subset = copy.deepcopy(lockfile)
    keep = {owner}
    by_url = {dependency.repo_url: key for key, dependency in subset.dependencies.items()}
    parent = subset.dependencies[owner].resolved_by if owner in subset.dependencies else None
    while parent and by_url.get(parent) not in (None, *keep):
        keep.add(by_url[parent])
        parent = subset.dependencies[by_url[parent]].resolved_by
    for key in list(subset.dependencies):
        if key not in keep:
            del subset.dependencies[key]
    return subset


def _render_locked(
    lockfile: LockFile,
    owner: str | None,
    lockfile_root: Path,
    user_scope: bool,
    diagnostics,
    *,
    cache_only: bool = False,
    on_package=None,
    records: bool = True,
) -> Path | None:
    """Render the locked packages (or only *owner*) into a private scratch tree.

    The replay uses every target the lock recorded as well as the current
    manifest's, so files of a target the manifest just dropped stay provable.
    *cache_only* never fetches; *on_package* is the replay's per-package hook,
    which needs *records* off so every package renders live.
    """
    from apm_cli.install.drift import (
        CheckLogger,
        ReplayConfig,
        _make_scratch_root,
        run_replay,
    )

    if owner is not None and owner not in lockfile.dependencies:
        return None
    # The scratch tree is private and removed at exit; the snapshot sits
    # under a name no target deploys to.
    scratch = _make_scratch_root(lockfile_root)
    snapshot = scratch / ".apm-locked-render" / "apm.lock.yaml"
    config = ReplayConfig(
        project_root=lockfile_root,
        lockfile_path=snapshot,
        cache_only=cache_only,
        records=records,
        on_package=on_package,
        scratch_root=scratch,
        modules_root=scratch / "apm_modules",
        user_scope=user_scope,
        include_recorded_targets=True,
    )
    try:
        snapshot.parent.mkdir()  # write-seam-exempt: private scratch tree
        from apm_cli.deps.lockfile_write_authority import lockfile_scratch_writes

        with lockfile_scratch_writes():
            (lockfile if owner is None else _locked_subset(lockfile, owner)).write(snapshot)
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()), write_seam.private_scratch():
                return run_replay(config, CheckLogger(verbose=False))
    except Exception as exc:
        if owner is not None:
            diagnostics.warn(
                f"Could not render the locked version of {owner} to verify cleanup "
                f"({exc}). Its files without a matching local deployment receipt are kept."
            )
        return None


@dataclass
class CleanupResult:
    """Outcome of a stale-file cleanup pass for a single package."""

    deleted: list[str] = field(default_factory=list)
    """Workspace-relative paths actually removed from disk."""

    failed: list[str] = field(default_factory=list)
    """Paths that raised during ``unlink``/``rmtree`` and should be
    retained in ``deployed_files`` for retry on the next install."""

    kept: list[str] = field(default_factory=list)
    """Paths this install still declares but left on disk unwritten (a refused
    or skipped member); never stale, so their lockfile entries carry forward."""

    skipped_user_edit: list[str] = field(default_factory=list)
    """Paths skipped because the on-disk content no longer matches the
    hash APM recorded at deploy time -- treated as user-edited."""

    skipped_unmanaged: list[str] = field(default_factory=list)
    """Paths refused by the safety gates (validation failure, directory
    entry, etc.). Callers retain the prior ownership row when it exists."""

    rejected: dict[str, str] = field(default_factory=dict)
    """Paths a path-safety gate refused during this cleanup, mapped to the
    reason; also listed in ``skipped_unmanaged``. A current safety rejection
    is always a failure: consent never waives it (#4815)."""

    unproven: list[str] = field(default_factory=list)
    """Files kept because no deployment evidence proves APM wrote them (#3701);
    also listed in ``skipped_unmanaged``. Callers name them to the user."""

    released: list[str] = field(default_factory=list)
    """Skill directories of a departing owner (orphan cleanup) kept on disk
    because only user content remains in them. Every APM-owned member is
    gone and the owner leaves the lockfile, so ownership is released rather
    than retained. A still-declared owner keeps the directory claim so a
    later install can remove the directory once the user content is gone."""

    deleted_targets: list[Path] = field(default_factory=list)
    """Absolute paths of deleted entries -- input to
    :meth:`BaseIntegrator.cleanup_empty_parents`."""

    aliased: list[str] = field(default_factory=list)
    """Stale spellings that name the same physical entry as a live path on a
    case- or normalization-insensitive filesystem; renamed to the live
    spelling instead of deleted, and no longer owned."""

    deleted_directories: list[str] = field(default_factory=list)
    """Entries of ``deleted`` that were whole skill directories."""

    user_scope: bool = False
    """Whether paths are relative to the user's home (shown with ``~/``)."""

    def reject(self, rel_path: str, reason: str) -> None:
        """Record that a path-safety gate refused *rel_path* for *reason*."""
        self.skipped_unmanaged.append(rel_path)
        self.rejected[rel_path] = reason

    @property
    def announced_paths(self) -> list[str]:
        """Return the deleted entries a user is told about, each named once.

        A deleted directory is named with a trailing ``/`` and stands for the
        members deleted with it, so the count matches the listed paths.
        """
        directories = {path.rstrip("/") for path in self.deleted_directories}
        prefix = "~/" if self.user_scope else ""
        announced: list[str] = []
        for path in sorted(dict.fromkeys(path.rstrip("/") for path in self.deleted)):
            # Check each ancestor against the set: cost scales with path depth.
            parts = path.split("/")
            if any("/".join(parts[:depth]) in directories for depth in range(1, len(parts))):
                continue
            shown = f"{path}/" if path in directories else path
            announced.append(shown if "://" in shown else prefix + shown)
        return announced

    @property
    def retained(self) -> list[str]:
        """Return unique paths that were deliberately left on disk.

        Does not include ``released`` paths, whose ownership was dropped.
        """
        return list(
            dict.fromkeys(
                [
                    *self.kept,
                    *self.failed,
                    *self.skipped_user_edit,
                    *self.skipped_unmanaged,
                ]
            )
        )


def _is_skill_directory_entry(rel_path: str) -> bool:
    """Return True when *rel_path* matches a skill directory pattern.

    Skill directories are deployed under ``<skills root>/<name>`` where the
    skills root is a target's ``<root>/<subdir>`` (e.g. ``.github/skills``,
    ``agent-data/workflows``).  The path must have exactly one component after
    the skills root (the skill name) to qualify -- deeper entries or the
    skills root itself do not match.
    """
    from apm_cli.integration.targets import skills_root_prefixes

    # Legacy ``deployed_skills`` migrations record directories with a trailing slash.
    normalized = rel_path.replace("\\", "/").rstrip("/")
    parent, _, name = normalized.rpartition("/")
    if not name:
        return False
    if parent in skills_root_prefixes():
        return True
    # Legacy/foreign roots (cowork URIs, bespoke prefixes): ``<prefix>/skills/<name>``.
    parts = Path(rel_path).parts
    if len(parts) < 3:
        return False
    try:
        skills_idx = parts.index("skills")
    except ValueError:
        return False
    return skills_idx == len(parts) - 2 and skills_idx >= 1


def _safe_remove_skill_directory(
    skill_dir: Path,
    rel_path: str,
    recorded_hashes: dict[str, str],
    all_stale: set[str],
    evidence: DeploymentEvidence,
    result: CleanupResult,
    diagnostics,
    dep_key: str,
    failed_path_retained: bool,
    protected: Callable[[str], bool] = lambda _rel: False,
    path_gate: Callable[[str, bool], bool] = lambda _rel, _final_link: False,
) -> None:
    """Attempt safe removal of an APM-deployed skill directory.

    Called in the deferred second pass after individual files have been
    deleted.  The directory is removed when:

    - It no longer exists (already cleaned or never created).
    - It is empty (all files already deleted in the first pass).
    - Every remaining file is tracked by the lockfile and *evidence*
      authorizes its bytes (APM-deployed, unmodified).

    When the directory contains user-created or user-edited files, removal
    is skipped with a diagnostic listing the blocking entries. A member in
    *protected* (a surviving dependency still deploys it, in any spelling
    naming the same file) is never removed:
    the directory's other proven members are removed one by one and the
    directory stays (#4750).
    """
    if not skill_dir.exists():
        return

    if not skill_dir.is_dir() or skill_dir.is_symlink():
        return

    # Check if directory is empty -- safe to rmdir.
    try:
        remaining = list(skill_dir.iterdir())
    except OSError:
        result.failed.append(rel_path)
        return

    if not remaining:
        try:
            write_seam.rmdir(skill_dir)
            result.deleted.append(rel_path)
            result.deleted_targets.append(skill_dir)
            result.deleted_directories.append(rel_path)
        except OSError as exc:
            result.failed.append(rel_path)
            _emit_dir_failure(diagnostics, rel_path, exc, dep_key, failed_path_retained)
        return

    # Non-empty: every remaining file must be APM-tracked AND carry deployment
    # evidence (#3701). Recurse into subdirectories (skills can contain
    # scripts/, assets/, references/ subdirectories). Member claims extend the
    # directory claim, so project paths and cowork:// URIs share one rule.
    blocking: list[str] = []
    proven: list[tuple[str, Path]] = []
    shared: list[str] = []
    # True while every blocking entry is user content (untracked and never
    # deployed); then nothing APM-owned remains and ownership is released.
    user_content_only = True

    for child in skill_dir.rglob("*"):
        relative_child = child.relative_to(skill_dir)
        child_rel = f"{rel_path.rstrip('/')}/{relative_child.as_posix()}"
        # Symlinks are treated as unmanaged -- APM does not deploy
        # symlinks, so any symlink is user-created content.
        if child.is_symlink():
            blocking.append(child_rel)
            continue
        if is_python_bytecode_cache_path(relative_child):
            continue
        if child.is_dir():
            continue
        if protected(child_rel):
            # A surviving dependency still deploys it: never this removal's to delete.
            shared.append(child_rel)
            continue
        if child_rel not in recorded_hashes and child_rel not in all_stale:
            # Never tracked by APM: user content.
            blocking.append(child_rel)
            continue
        # Tracked but still present: deletion needs the member's own evidence.
        if evidence.verdict(child_rel, child) != AUTHORIZED:
            blocking.append(child_rel)
            user_content_only = False
        else:
            proven.append((child_rel, child))

    if blocking and evidence.lockfile_consent:
        # Consent decides per file (#4295): proven members go, the rest stay named.
        _remove_proven_members(skill_dir, proven, evidence, result, diagnostics, dep_key, path_gate)
    if blocking:
        # Show up to 5 blocking paths so the user knows which files
        # prevented cleanup (reviewer feedback on PR #1767).
        # Under consent every kept member is named: it decides per file (#4815).
        cap = len(blocking) if evidence.lockfile_consent else 5
        preview = blocking[:cap]
        suffix = f" (and {len(blocking) - cap} more)" if len(blocking) > cap else ""
        file_list = ", ".join(preview) + suffix
        release = user_content_only and not failed_path_retained
        if release:
            from apm_cli.core.remediation import kept_note

            message = kept_note(
                f"skill directory {rel_path} for {len(blocking)} file(s) not owned by APM",
                file_list,
                "APM no longer manages it; remove it manually if no longer needed.",
            )
        else:
            message = (
                f"Skipped removing skill directory {rel_path}: "
                f"{len(blocking)} file(s) not owned by APM or "
                f"modified since deployment: {file_list}. "
                "Remove manually if no longer needed."
            )
        diagnostics.warn(message, package=dep_key)
        (result.released if release else result.skipped_unmanaged).append(rel_path)
        return

    if shared:
        # The survivor's members stay; this owner's proven members go one by one.
        if _remove_proven_members(
            skill_dir, proven, evidence, result, diagnostics, dep_key, path_gate
        ):
            result.released.append(rel_path)
        else:
            result.skipped_unmanaged.append(rel_path)
        return

    if evidence.lockfile_consent:
        _remove_consented_skill_directory(
            skill_dir,
            rel_path,
            proven,
            (evidence, result, diagnostics),
            dep_key=dep_key,
            failed_path_retained=failed_path_retained,
            path_gate=path_gate,
        )
        return

    # All remaining files are APM-tracked with matching hashes -- safe
    # to remove the entire directory tree.
    try:
        write_seam.rmtree(skill_dir)
        result.deleted.append(rel_path)
        result.deleted_targets.append(skill_dir)
        result.deleted_directories.append(rel_path)
    except OSError as exc:
        result.failed.append(rel_path)
        _emit_dir_failure(diagnostics, rel_path, exc, dep_key, failed_path_retained)


def _remove_consented_skill_directory(
    skill_dir: Path,
    rel_path: str,
    proven: list[tuple[str, Path]],
    context: tuple[DeploymentEvidence, CleanupResult, object],
    *,
    dep_key: str,
    failed_path_retained: bool,
    path_gate: Callable[[str, bool], bool] = lambda _rel, _final_link: False,
) -> None:
    """Consent decides per file (#4295).

    Each member is rechecked right before its unlink, and only a directory
    left holding no file is removed, bottom-up and never recursively, so a
    file that appears after the scan keeps its directory.
    """
    evidence, result, diagnostics = context
    if not _remove_proven_members(
        skill_dir, proven, evidence, result, diagnostics, dep_key, path_gate
    ) or any(
        child.is_symlink()
        or (child.is_file() and not is_python_bytecode_cache_path(child.relative_to(skill_dir)))
        for child in skill_dir.rglob("*")
    ):
        result.skipped_unmanaged.append(rel_path)
        return
    try:
        emptied = _remove_emptied_tree(skill_dir)
    except OSError as exc:
        result.failed.append(rel_path)
        _emit_dir_failure(diagnostics, rel_path, exc, dep_key, failed_path_retained)
        return
    if not emptied:
        result.skipped_unmanaged.append(rel_path)
        return
    result.deleted.append(rel_path)
    result.deleted_targets.append(skill_dir)
    result.deleted_directories.append(rel_path)


def _remove_emptied_tree(root: Path) -> bool:
    """Remove *root* bottom-up, deleting only bytecode caches and empty directories.

    Returns False, leaving the rest in place, at the first entry that is a
    symlink, another file, or a directory that is no longer empty.
    """
    for current, directories, files in os.walk(root, topdown=False):
        base = Path(current)
        for name in files:
            path = base / name
            if path.is_symlink() or not is_python_bytecode_cache_path(path.relative_to(root)):
                return False
            write_seam.unlink(path)
        for name in directories:
            if not _remove_empty_directory(base / name):
                return False
    return _remove_empty_directory(root)


def _remove_empty_directory(path: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        write_seam.rmdir(path)
    except OSError as exc:
        if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
            return False
        raise
    return True


def _keep_consent_link(result: CleanupResult, diagnostics, stale_path: str, dep_key: str) -> None:
    """Keep a final symbolic link under ``--trust-lockfile`` and name it (#4295)."""
    result.skipped_unmanaged.append(stale_path)
    diagnostics.warn(
        kept_note(
            stale_path,
            "it is a symbolic link, which --trust-lockfile never removes",
            KEPT_DELETE_MANUALLY,
        ),
        package=dep_key,
    )


def _consent_kept_noncanonical(
    stale_path: str, project_root: Path, evidence: DeploymentEvidence, *, targets, user_scope: bool
) -> bool:
    """Whether a path the gate refused only for a ``.`` segment is consent-kept (#5096).

    Its canonical spelling must pass the same gate; anything else stays a
    safety rejection, and nothing is ever removed through the claim.
    """
    segments = stale_path.replace("\\", "/").split("/")
    # Only a pure "." spelling qualifies: ".." is traversal and is never kept.
    if not evidence.lockfile_consent or "." not in segments or ".." in segments:
        return False
    return BaseIntegrator.validate_deploy_path(
        _claim_key(stale_path), project_root, targets=targets, user_scope=user_scope
    ) and evidence.consent_keep_noncanonical(stale_path)


def _keep_noncanonical(result: CleanupResult, diagnostics, stale_path: str, dep_key: str) -> None:
    """Keep a claim recorded with a ``.`` segment under ``--trust-lockfile`` and name it."""
    result.skipped_unmanaged.append(stale_path)
    diagnostics.warn(
        kept_note(
            stale_path,
            "it is recorded with a '.' path segment, which APM never removes through",
            KEPT_DELETE_MANUALLY,
        ),
        package=dep_key,
    )


def _consent_kept_final_link(
    stale_path: str, project_root: Path, evidence: DeploymentEvidence, *, targets, user_scope: bool
) -> bool:
    """Whether a path that failed the strict gate is only a consent-kept final link (#4815).

    Its parent must pass the same gate with a final link allowed: a symlinked
    ancestor stays a safety rejection.
    """
    return (
        evidence.lockfile_consent
        and BaseIntegrator.validate_deploy_path(
            stale_path,
            project_root,
            targets=targets,
            user_scope=user_scope,
            allow_final_symlink=True,
        )
        and evidence.consent_kept_link(stale_path, project_root / stale_path)
    )


def _remove_proven_members(
    skill_dir: Path,
    proven: list[tuple[str, Path]],
    evidence: DeploymentEvidence,
    result: CleanupResult,
    diagnostics,
    dep_key: str,
    path_gate: Callable[[str, bool], bool] = lambda _rel, _final_link: False,
) -> bool:
    """Remove each proven member of a skill directory that must itself stay.

    The deletion gates run again immediately before each unlink, so a file
    changed since the directory scan is kept and named, never removed: the
    member must still be a regular file, reached through no symlinked
    directory from the deploy root (*path_gate*, the canonical path gate) and
    contained in *skill_dir*, and its evidence must still
    authorize its bytes. Returns whether every member was removed.
    """
    complete = True
    for member_rel, member in proven:
        if not (_member_still_safe(skill_dir, member) and path_gate(member_rel, False)):
            if (
                _member_parents_safe(skill_dir, member)
                and path_gate(member_rel, True)
                and evidence.consent_kept_link(member_rel, member)
            ):
                # A final link whose every ancestor is safe is consent retention (#4815).
                _keep_consent_link(result, diagnostics, member_rel, dep_key)
                complete = False
                continue
            result.reject(member_rel, REJECTED_REPLACED)
            diagnostics.warn(
                f"Skipped removing {member_rel}: it was replaced or moved since APM "
                "checked it. Inspect it and delete it manually if no longer needed.",
                package=dep_key,
            )
            complete = False
            continue
        verdict = evidence.verdict(member_rel, member)
        if verdict != AUTHORIZED:
            _keep_without_evidence(result, diagnostics, member_rel, verdict, dep_key)
            complete = False
            continue
        try:
            write_seam.unlink(member)
            result.deleted.append(member_rel)
            result.deleted_targets.append(member)
        except OSError as exc:
            complete = False
            result.failed.append(member_rel)
            diagnostics.warn(
                f"Could not remove {member_rel}: {exc}. Fix the file, then retry.",
                package=dep_key,
            )
    return complete


def _member_still_safe(skill_dir: Path, member: Path) -> bool:
    """Return whether *member* is still a regular file inside *skill_dir*, via no link."""
    from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

    try:
        if member.is_symlink() or not member.is_file():
            return False
        if not _member_parents_safe(skill_dir, member):
            return False
        ensure_path_within(member, skill_dir)
    except (OSError, PathTraversalError):
        return False
    return True


def _member_parents_safe(skill_dir: Path, member: Path) -> bool:
    """Return whether *member* is reached from *skill_dir* through no symlinked directory."""
    try:
        if skill_dir.is_symlink():
            return False
        parent = member.parent
        while parent != skill_dir:
            if parent.is_symlink() or parent == parent.parent:
                return False
            parent = parent.parent
    except OSError:
        return False
    return True


def _emit_dir_failure(diagnostics, rel_path, exc, dep_key, failed_path_retained):
    """Emit a diagnostic for a failed directory removal."""
    if failed_path_retained:
        diagnostics.warn(
            (
                f"Could not remove skill directory {rel_path}: {exc}. "
                "Path retained in lockfile; will retry on next "
                "'apm install'."
            ),
            package=dep_key,
        )
    else:
        diagnostics.warn(
            (
                f"Could not remove skill directory {rel_path}: {exc}. "
                "The owning package is no longer in apm.yml -- "
                "remove the directory manually."
            ),
            package=dep_key,
        )


_REFUSAL_REASONS = {
    UNPROVEN: "no local deployment receipt or render of the locked package version",
    EDITED: "edited since APM deployed it",
    UNREADABLE: "its content could not be read to verify it",
    SYMLINKED: "replaced by a symbolic link, so its directory cannot be removed",
}


def refusal_reason(verdict: str) -> str:
    """Name why deployment evidence does not authorize deleting a file."""
    return _REFUSAL_REASONS.get(verdict, _REFUSAL_REASONS[UNPROVEN])


def uninstall_evidence_remedies(
    *, user_scope: bool, trust_lockfile: bool = False, packages: Iterable[str] = ()
) -> tuple[str, ...]:
    """Return the runnable remedies when uninstall lacks deletion evidence.

    The ``--trust-lockfile`` remedy is offered only when *trust_lockfile*: at
    least one refused file is deletable under that consent (#4295). It reruns
    the uninstall of *packages*, the identifiers the user passed.
    """
    remove = UNINSTALL_EVIDENCE_REMOVE
    reinstall = UNINSTALL_EVIDENCE_REINSTALL
    trust = UNINSTALL_EVIDENCE_TRUST_LOCKFILE
    if user_scope:
        reinstall = reinstall.with_command("apm install -g")
        remove = remove.with_command("apm uninstall -g")
        trust = trust.with_command("apm uninstall -g {packages} --trust-lockfile")
    trust = trust.bind(packages=" ".join(shlex.quote(package) for package in packages))
    reinstall = reinstall.bind(uninstall=remove.command)
    remedies = (reinstall.sentence(), remove.sentence())
    return (*remedies, trust.sentence()) if trust_lockfile else remedies


def _keep_without_evidence(
    result: CleanupResult, diagnostics, stale_path: str, verdict: str, dep_key: str
) -> None:
    """Keep a file whose deployment evidence does not authorize deletion."""
    if verdict == UNPROVEN:
        result.skipped_unmanaged.append(stale_path)
        result.unproven.append(stale_path)
        from apm_cli.core.remediation import (
            KEPT_DELETE_MANUALLY,
            KEPT_UNPROVEN_REASON,
            kept_note,
        )

        message = kept_note(stale_path, KEPT_UNPROVEN_REASON, KEPT_DELETE_MANUALLY)
    elif verdict == UNREADABLE:
        result.skipped_user_edit.append(stale_path)
        message = (
            f"Skipped removing {stale_path}: could not verify file content. "
            "Inspect the file and delete it manually if no longer needed."
        )
    else:
        result.skipped_user_edit.append(stale_path)
        message = (
            f"Skipped removing {stale_path}: file has been edited since APM "
            "deployed it. Delete it manually if you no longer need it, or "
            "ignore this warning to keep your changes."
        )
    diagnostics.warn(message, package=dep_key)


def remove_stale_deployed_files(
    stale_paths: Iterable[str],
    project_root: Path,
    *,
    dep_key: str,
    targets,
    diagnostics,
    evidence: DeploymentEvidence,
    recorded_hashes: dict[str, str] | None = None,
    failed_path_retained: bool = True,
    user_scope: bool = False,
    allow_final_symlink: bool = False,
    receipt_retirements: list[Path] | None = None,
    live_paths: Iterable[str] = (),
) -> CleanupResult:
    """Remove APM-deployed files that are no longer produced by *dep_key*.

    Args:
        stale_paths: Workspace-relative paths flagged as stale by
            :func:`apm_cli.drift.detect_stale_files` (intra-package
            renames/removals) or the orphan owner (``install/orphans.py``)
            (whole package removed from the manifest).
        project_root: Project root the deletion is scoped within.
        dep_key: Unique key of the package these paths belong to (used
            for diagnostic attribution).
        targets: Resolved target profiles for this install (passed
            through to :meth:`BaseIntegrator.validate_deploy_path`).
        diagnostics: ``DiagnosticCollector`` -- recoverable warnings
            (user-edit skip, unlink failure, refused directory entry)
            are pushed here.
        evidence: Required :class:`DeploymentEvidence` for this transition.
            A file is deleted only when it authorizes the file's bytes; there
            is deliberately no default, so no caller can fail open.
        recorded_hashes: Mapping from rel-path to ``"sha256:<hex>"`` as
            stored on the previous ``LockedDependency``. Never deletion
            evidence; it only marks which members a skill directory tracked
            and which final symlinks replaced a hashed file.
        failed_path_retained: When ``True`` (default, intra-package
            stale cleanup) the failure diagnostic tells the user APM
            will retry on the next install -- the caller is expected
            to re-insert ``result.failed`` into the new
            ``deployed_files``. When ``False`` (orphan cleanup) the
            owning package is being removed from the lockfile so a
            failed path cannot be retained; the diagnostic instructs
            the user to remove the file manually instead.
        user_scope: Include registered user-root prefixes such as
            ``.copilot/`` when validating legacy deployed-file paths.
        receipt_retirements: Optional batch accumulator for confirmed physical
            removals or absence. The caller must retire it through the receipt
            owner in a finally block; omitted callers retire immediately.
        live_paths: Paths still deployed after this run. A stale path that is
            only another spelling of one of them on this filesystem is renamed
            to the live spelling (``aliased``) instead of deleted. A skill
            directory member among them (for example one a surviving
            dependency still deploys) is never removed (#4750).

    Returns:
        :class:`CleanupResult` describing what happened. The caller is
        responsible for any post-deletion bookkeeping (extending the
        new ``deployed_files`` list with ``failed`` so they are retried,
        invoking :meth:`BaseIntegrator.cleanup_empty_parents` on
        ``deleted_targets``, calling
        :meth:`InstallLogger.cleanup_skipped_user_edit` for each entry
        in ``skipped_user_edit`` so the inline yellow warning renders,
        and naming ``announced_paths`` to the user via
        :meth:`CommandLogger.stale_cleanup` /
        :meth:`InstallLogger.orphan_cleanup`).
    """
    result = CleanupResult(user_scope=user_scope)
    # Callers may key hashes with native Windows separators; lookups are POSIX.
    recorded_hashes = {
        path.replace("\\", "/"): content_hash
        for path, content_hash in (recorded_hashes or {}).items()
    }

    # Materialise stale_paths so we can iterate twice: once for the main
    # file-deletion loop and once as a lookup set for the deferred
    # directory pass.
    _stale_list = sorted(stale_paths)
    _stale_set: set[str] = set(_stale_list)

    # Skill directory entries are deferred to a second pass so individual
    # files are removed first. After files are deleted the directory is
    # either empty (safe rmdir) or contains only verified APM-tracked
    # files (safe rmtree).
    _deferred_dirs: list[tuple[str, Path]] = []
    absent_targets: list[Path] = []
    live_paths = list(live_paths)
    live_index = SpellingAliasIndex(live_paths)
    live_set = frozenset(path.replace("\\", "/").rstrip("/") for path in live_paths)

    def path_gate(rel_path: str, final_link: bool) -> bool:
        # The canonical gate from the deploy root, rechecked before each member unlink (#4815).
        return BaseIntegrator.validate_deploy_path(
            rel_path,
            project_root,
            targets=targets,
            user_scope=user_scope,
            allow_final_symlink=final_link,
        )

    def protected(rel_path: str) -> bool:
        # A survivor's claim may spell the same file differently on this filesystem.
        return rel_path in live_set or live_index.alias(rel_path, project_root) is not None

    # Lazy-resolve cowork root at most once per invocation (same
    # pattern as sync_remove_files in base_integrator.py -- PR #926 P4).
    _cowork_root_resolved: bool = False
    _cowork_root_cached: Path | None = None
    _cowork_orphans_skipped: int = 0
    _cowork_resolve_errors: int = 0

    for stale_path in _stale_list:
        # A member this install declares but did not rewrite is not stale.
        if evidence.kept(stale_path):
            result.kept.append(stale_path)
            continue
        # -- Cowork:// paths ---------------------------------------
        # Handled BEFORE validate_deploy_path because that method
        # hard-rejects cowork:// when the OneDrive root is unavailable
        # (returning False ⇒ skipped_unmanaged).  For cleanup we want
        # the gentler "retain in *failed* for retry" behaviour, so we
        # do equivalent security checks (no traversal, known prefix,
        # containment via from_lockfile_path) ourselves.
        from .copilot_cowork_paths import COWORK_URI_SCHEME

        if stale_path.startswith(COWORK_URI_SCHEME):
            # Basic security: reject path-traversal components.
            if ".." in stale_path:
                result.reject(stale_path, REJECTED_TRAVERSAL)
                continue
            # Verify the path starts with a known integration prefix.
            from .targets import get_integration_prefixes

            if not stale_path.startswith(get_integration_prefixes(targets=targets)):
                result.reject(stale_path, REJECTED_PREFIX)
                continue
            # Resolve the cowork:// URI to a real filesystem path.
            try:
                if not _cowork_root_resolved:
                    from .copilot_cowork_paths import (
                        resolve_copilot_cowork_skills_dir,
                    )

                    _cowork_root_cached = resolve_copilot_cowork_skills_dir()
                    _cowork_root_resolved = True
                if _cowork_root_cached is None:
                    # OneDrive unavailable -- retain lockfile entry so a
                    # later install with a configured root can clean up.
                    _cowork_orphans_skipped += 1
                    result.failed.append(stale_path)
                    continue
                from .copilot_cowork_paths import cowork_locator

                # Never follow a final symlink to the file it points at.
                stale_target = cowork_locator(stale_path, _cowork_root_cached)
            except Exception:
                # Containment violation or malformed path -- retain in
                # lockfile for manual inspection.
                _cowork_resolve_errors += 1
                result.failed.append(stale_path)
                continue
        else:
            # ── Non-cowork paths ─────────────────────────────────────
            # Gate 1: path validation (traversal, allowed prefix, in-tree).
            if not _stale_gate_passes(
                stale_path,
                project_root,
                targets=targets,
                user_scope=user_scope,
                allow_final_symlink=allow_final_symlink,
            ):
                if not allow_final_symlink and _consent_kept_final_link(
                    stale_path, project_root, evidence, targets=targets, user_scope=user_scope
                ):
                    _keep_consent_link(result, diagnostics, stale_path, dep_key)
                    continue
                if _consent_kept_noncanonical(
                    stale_path, project_root, evidence, targets=targets, user_scope=user_scope
                ):
                    _keep_noncanonical(result, diagnostics, stale_path, dep_key)
                    continue
                result.reject(stale_path, REJECTED_UNSAFE_PATH)
                continue
            stale_target = project_root / stale_path

        if _stale_target_absent(stale_target, allow_final_symlink=allow_final_symlink):
            # File already gone -- treat as cleaned (no-op success).
            absent_targets.append(stale_target)
            continue

        # A case/normalization-only rename upstream makes the old and new
        # spellings one entry on macOS and Windows: adopt the live spelling
        # on disk instead of deleting the file this install just wrote.
        live_alias = live_index.alias(stale_path, project_root)
        if live_alias is not None:
            try:
                write_seam.rename(stale_target, project_root / live_alias)
                result.aliased.append(stale_path)
                absent_targets.append(stale_target)
            except OSError as exc:
                result.failed.append(stale_path)
                diagnostics.warn(
                    f"Could not rename {stale_path} to {live_alias}: {exc}.",
                    package=dep_key,
                )
            continue

        # Gate 2: directory handling. APM-managed primitives are
        # file-keyed; a directory entry under an integration prefix is
        # normally treated as untrusted. However, skill directories are
        # legitimately tracked by APM (services.py records the skill dir
        # entry alongside its contained files). Skill directories are
        # deferred to a second pass so individual files are deleted first;
        # non-skill directory entries are still rejected immediately.
        if stale_target.is_dir() and not stale_target.is_symlink():
            if _is_skill_directory_entry(stale_path):
                _deferred_dirs.append((stale_path, stale_target))
            else:
                result.reject(stale_path, REJECTED_DIRECTORY)
                diagnostics.warn(
                    (
                        f"Refused to remove directory entry {stale_path}: APM "
                        "only deletes individual files. If this entry was added "
                        "by a malicious or corrupt lockfile, remove it manually "
                        "from apm.lock.yaml."
                    ),
                    package=dep_key,
                )
            continue

        # Gate 3: deployment evidence (#3701). Only a matching local receipt
        # or byte equality with the locked render proves APM deployed these
        # bytes; a lockfile hash alone never authorizes deletion. Fails
        # CLOSED: edited, unproven or unreadable files are kept with a warning.
        if evidence.consent_kept_link(stale_path, stale_target):
            _keep_consent_link(result, diagnostics, stale_path, dep_key)
            continue
        # Unlinking a managed final symlink removes no file content, so it
        # keeps its own rule (callers opt in via allow_final_symlink).
        if allow_final_symlink and stale_target.is_symlink():
            if not _managed_link_unlinkable(recorded_hashes.get(stale_path.replace("\\", "/"))):
                result.skipped_user_edit.append(stale_path)
                diagnostics.warn(
                    (
                        f"Skipped removing {stale_path}: the deployed file was "
                        "replaced by a symlink. Inspect the link and delete it "
                        "manually if no longer needed."
                    ),
                    package=dep_key,
                )
                continue
        else:
            verdict = evidence.verdict(stale_path, stale_target)
            if verdict != AUTHORIZED:
                _keep_without_evidence(result, diagnostics, stale_path, verdict, dep_key)
                continue

        # All gates passed -- safe to delete.
        try:
            write_seam.unlink(stale_target)
            result.deleted.append(stale_path)
            result.deleted_targets.append(stale_target)
        except Exception as exc:
            result.failed.append(stale_path)
            if failed_path_retained:
                diagnostics.warn(
                    (
                        f"Could not remove stale file {stale_path}: {exc}. "
                        "Path retained in lockfile; will retry on next "
                        "'apm install'."
                    ),
                    package=dep_key,
                )
            else:
                diagnostics.warn(
                    (
                        f"Could not remove orphaned file {stale_path}: {exc}. "
                        "The owning package is no longer in apm.yml -- "
                        "delete the file manually."
                    ),
                    package=dep_key,
                )

    # -- Second pass: deferred skill directories -------------------
    # Individual files have been deleted above. Now attempt safe removal
    # of skill directories that APM itself created.
    for _dir_path, _dir_target in _deferred_dirs:
        _safe_remove_skill_directory(
            _dir_target,
            _dir_path,
            recorded_hashes,
            _stale_set,
            evidence,
            result,
            diagnostics,
            dep_key,
            failed_path_retained,
            protected,
            path_gate,
        )

    # One-time warnings for cowork edge cases (mirrors sync_remove_files).
    if _cowork_orphans_skipped > 0:
        diagnostics.warn(
            (
                f"Cowork: skipping {_cowork_orphans_skipped} stale lockfile "
                f"{'entry' if _cowork_orphans_skipped == 1 else 'entries'}"
                f" -- OneDrive path not detected.\n{COWORK_SKILLS_DIR_UNDETECTED.sentence()} "
                "Alternatively, set APM_COPILOT_COWORK_SKILLS_DIR."
            ),
            package=dep_key,
        )
    if _cowork_resolve_errors > 0:
        diagnostics.warn(
            (
                f"Cowork: {_cowork_resolve_errors} lockfile "
                f"{'entry' if _cowork_resolve_errors == 1 else 'entries'}"
                " failed path resolution (containment violation or "
                "malformed path). Paths retained for manual inspection."
            ),
            package=dep_key,
        )

    from apm_cli.core.deployment_ledger import DeploymentMaterializationSession

    retired = [*result.deleted_targets, *absent_targets]
    if receipt_retirements is None:
        DeploymentMaterializationSession.retire_removed(
            project_root, retired, user_scope=user_scope
        )
    else:
        receipt_retirements.extend(retired)
    return result


def remove_synced_files(
    managed_files: Iterable[str],
    project_root: Path,
    *,
    prefixes: tuple[str, ...],
    dep_key: str,
    targets,
    evidence: DeploymentEvidence | None,
    recorded_hashes: dict[str, str] | None = None,
    allow_final_symlink: bool = False,
    receipt_retirements: list[Path] | None = None,
) -> CleanupResult:
    """Remove lockfile-listed files under *prefixes* for an integrator sync (#3701).

    Integrator syncs (uninstall Phase 1 and survivor refresh) delete through
    :func:`remove_stale_deployed_files`, so they need the same deployment
    evidence. *evidence* is required; ``None`` authorizes nothing and keeps
    every file with a warning. Diagnostics are rendered before returning.
    """
    from apm_cli.utils.diagnostics import DiagnosticCollector

    paths: set[str] = set()
    for rel_path in managed_files:
        normalized = rel_path.replace("\\", "/")
        if normalized.startswith(prefixes) and ".." not in Path(normalized).parts:
            paths.add(rel_path if Path(rel_path).is_absolute() else normalized)
    diagnostics = DiagnosticCollector()
    result = remove_stale_deployed_files(
        paths,
        project_root,
        dep_key=dep_key,
        targets=targets,
        diagnostics=diagnostics,
        evidence=evidence or DeploymentEvidence.none(project_root),
        recorded_hashes=recorded_hashes,
        failed_path_retained=False,
        # Evidence is scope-bound: user-scope syncs validate and retire receipts there.
        user_scope=evidence is not None and evidence.user_scope,
        allow_final_symlink=allow_final_symlink,
        receipt_retirements=receipt_retirements,
    )
    diagnostics.render_summary()
    return result


def _stale_gate_passes(
    stale_path: str, project_root: Path, *, targets, user_scope: bool, allow_final_symlink: bool
) -> bool:
    """Stale cleanup's path gate (traversal, allowed prefix, in-tree)."""
    return BaseIntegrator.validate_deploy_path(
        stale_path,
        project_root,
        targets=targets,
        user_scope=user_scope,
        allow_final_symlink=allow_final_symlink,
    )


def _stale_target_absent(stale_target: Path, *, allow_final_symlink: bool) -> bool:
    """Whether stale cleanup counts a gated path as already gone."""
    return not stale_target.exists() and not (allow_final_symlink and stale_target.is_symlink())


def _managed_link_unlinkable(recorded_hash: str | None) -> bool:
    """Whether a gated managed final symlink is unlinked: only one that replaced no hashed file."""
    return not recorded_hash


def managed_hook_prefixes(targets=None) -> tuple[str, ...]:
    """Deployed-path prefixes whose files uninstall's hook sync removes (#5035)."""
    from .targets import KNOWN_TARGETS

    return tuple(
        dict.fromkeys(
            f"{(t.primitives['hooks'].deploy_root or t.root_dir)}/hooks/".replace("\\", "/")
            for t in (*KNOWN_TARGETS.values(), *(targets or ()))
            if t.supports("hooks")
        )
    )


def _consent_keeps_link(physical: Path, *, lockfile_consent: bool) -> bool:
    """Whether ``--trust-lockfile`` consent keeps *physical* because it is a link (#4295, #5139)."""
    return lockfile_consent and physical.is_symlink()


def hook_sync_unlinks(
    rel_path: str,
    project_root: Path,
    *,
    recorded_hash: str | None,
    lockfile_consent: bool = False,
) -> bool:
    """Whether uninstall's hook sync removes a managed hook final symlink (#5035).

    Read-only, through the same prefix, gate, consent and link decisions the
    hook sync applies with ``allow_final_symlink=True``. Audit's advice offers
    an uninstall without ``--trust-lockfile``, so it passes no consent (#5139).
    """
    from .targets import KNOWN_TARGETS

    return (
        rel_path.replace("\\", "/").startswith(managed_hook_prefixes())
        and _stale_gate_passes(
            rel_path,
            project_root,
            targets=list(KNOWN_TARGETS.values()),
            user_scope=False,
            allow_final_symlink=True,
        )
        and (project_root / rel_path).is_symlink()
        and not _consent_keeps_link(project_root / rel_path, lockfile_consent=lockfile_consent)
        and _managed_link_unlinkable(recorded_hash)
    )


def final_link_handled(
    rel_path: str,
    project_root: Path,
    *,
    targets=None,
    user_scope: bool = False,
    allow_final_symlink: bool = False,
) -> bool:
    """Whether stale cleanup finishes a final-symlink claim instead of keeping it (#5035).

    Read-only, through the same gate and absence decisions as
    :func:`remove_stale_deployed_files`: a dangling link the path gate accepts
    counts as already gone. Any other final link is rejected or kept, so the
    uninstall stops. The defaults match uninstall's selected-owner cleanup.
    """
    return _stale_gate_passes(
        rel_path,
        project_root,
        targets=targets,
        user_scope=user_scope,
        allow_final_symlink=allow_final_symlink,
    ) and _stale_target_absent(project_root / rel_path, allow_final_symlink=allow_final_symlink)
