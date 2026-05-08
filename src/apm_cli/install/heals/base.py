"""Base types for the install-time heal pipeline.

The heal pipeline runs INSIDE ``_resolve_download_strategy`` (see
``apm_cli.install.phases.heal.run_heal_chain``) and lets us encode each
discrete self-heal as a small, isolated, individually-testable class.

Pattern: **Chain of Responsibility** with a flat, explicitly-ordered
registry. Each heal declares an applicability predicate and a mutation;
heals sharing the same ``exclusive_group`` short-circuit each other so
ordering is the only authority for "which heal wins" within a group.

A heal MUST NOT read the mutable output slots on :class:`HealContext`
(``lockfile_match``, ``ref_changed``, ``bypass_keys``, ``messages``);
it reads only input slots. This makes the chain order safely
independent of write-order within a group.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from apm_cli.models.apm_package import GitReference


class HealMessageLevel(str, Enum):
    """Severity for a heal-emitted user-facing message.

    ``INFO``: silent re-download, no warn (e.g. a non-recovery branch
    drift -- normal behaviour for unpinned branch refs).
    ``WARN``: visible recovery -- the user should know something was
    repaired and may want to act (e.g. upgrade APM).
    """

    INFO = "info"
    WARN = "warn"


@dataclass(frozen=True)
class HealMessage:
    """User-facing message emitted by a heal.

    The dispatcher renders these via ``ctx.diagnostics`` /
    ``ctx.logger`` after the chain completes, NOT inside individual
    heals -- this keeps heals pure (testable without mocking
    diagnostics) and centralises the rendering convention.
    """

    level: HealMessageLevel
    text: str
    package_key: str | None = None


@dataclass
class HealContext:
    """Per-dep snapshot + output slots passed through the heal chain.

    Inputs (immutable in spirit -- heals must not write):
    - dep_ref / package_key: identity of the dependency
    - resolved_ref: freshly-resolved git ref (may be None when
      resolution was skipped or failed)
    - existing_lockfile: the previously-written ``apm.lock.yaml``, or
      None on first install
    - lockfile_match: whether the strategy resolver decided the lockfile
      content matches what's on disk (BEFORE heal pipeline)
    - lockfile_match_via_content_hash_only: True when ``lockfile_match``
      was satisfied by the content-hash fallback path, NOT by git HEAD
      verification (typical for virtual packages)
    - update_refs: --update flag

    Outputs (heals mutate via ``set_lockfile_match`` /
    ``set_ref_changed`` / ``add_bypass_key`` / ``emit``):
    - lockfile_match: post-heal value (heals only ever turn True->False)
    - ref_changed: post-heal value (heals only ever turn False->True)
    - bypass_keys: dep keys to add to ``ctx.expected_hash_change_deps``
    - messages: messages to render after the chain
    - fired_groups: set of exclusive_group names a heal has fired in
    """

    dep_ref: Any
    package_key: str
    resolved_ref: GitReference | None
    existing_lockfile: Any
    lockfile_match: bool
    lockfile_match_via_content_hash_only: bool
    update_refs: bool

    ref_changed: bool = False
    bypass_keys: set[str] = field(default_factory=set)
    messages: list[HealMessage] = field(default_factory=list)
    fired_groups: set[str] = field(default_factory=set)

    def add_bypass_key(self, key: str) -> None:
        self.bypass_keys.add(key)

    def emit(self, level: HealMessageLevel, text: str) -> None:
        self.messages.append(HealMessage(level=level, text=text, package_key=self.package_key))


class Heal(Protocol):
    """Protocol every heal class implements.

    Implementations live under ``apm_cli.install.heals.<name>`` -- one
    file per heal. They are registered (in order) by
    ``apm_cli.install.heals.__init__::HEAL_CHAIN``.

    Attributes:
        name: stable identifier used in logs/tests.
        order: chain position (lower runs first).
        exclusive_group: when set, the first heal in this group to fire
            short-circuits later heals sharing the same group.
    """

    name: str
    order: int
    exclusive_group: str | None

    def applies(self, hctx: HealContext) -> bool: ...

    def execute(self, hctx: HealContext) -> None: ...
