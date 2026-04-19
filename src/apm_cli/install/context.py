"""Mutable state passed between install pipeline phases.

Each phase is a function ``def run(ctx: InstallContext) -> None`` that reads
the inputs already populated by earlier phases and writes its own outputs to
the context. Keeping shared state on a single typed object turns implicit
shared lexical scope (the legacy 1444-line `_install_apm_dependencies`) into
explicit data flow that is easy to audit and to test phase-by-phase.

Fields are added to this dataclass incrementally as phases are extracted from
the legacy entry point. A field belongs here if and only if it is read or
written by more than one phase. Phase-local state should stay local.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


@dataclass
class InstallContext:
    """State shared across install pipeline phases.

    Currently a stub. Fields are added by the phase extractions in P1 and P2
    of the install.py modularization refactor.

    Required-on-construction fields go above the ``field(default=...)``
    barrier; outputs accumulated by phases use ``field(default_factory=...)``.
    """

    project_root: Path
    apm_dir: Path

    dry_run: bool = False
    force: bool = False
    verbose: bool = False
    dev: bool = False
    only_packages: Optional[List[str]] = None

    intended_dep_keys: Set[str] = field(default_factory=set)
    package_deployed_files: Dict[str, List[str]] = field(default_factory=dict)
    package_types: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    package_hashes: Dict[str, Dict[str, str]] = field(default_factory=dict)
