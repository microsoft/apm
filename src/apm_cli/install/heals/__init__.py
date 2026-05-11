"""Install-time heal registry.

To add a new heal:
1. Create ``apm_cli/install/heals/<name>.py`` with a class implementing
   :class:`apm_cli.install.heals.base.Heal` (``name``, ``order``,
   ``exclusive_group``, ``applies``, ``execute``).
2. Import it below and add an instance to ``HEAL_CHAIN`` at the
   correct ``order`` position.
3. Add a unit test in ``tests/unit/install/heals/test_<name>.py``.

Ordering convention: lower ``order`` runs first. Heals sharing an
``exclusive_group`` short-circuit each other (first to fire wins).
"""

from __future__ import annotations

from .base import Heal, HealContext, HealMessage, HealMessageLevel
from .branch_ref_drift import BranchRefDriftHeal
from .buggy_lockfile_recovery import BuggyLockfileRecoveryHeal

# Explicit, ordered tuple. Tests import this directly.
HEAL_CHAIN: tuple[Heal, ...] = (
    BranchRefDriftHeal(),
    BuggyLockfileRecoveryHeal(),
)

__all__ = [
    "HEAL_CHAIN",
    "Heal",
    "HealContext",
    "HealMessage",
    "HealMessageLevel",
]
