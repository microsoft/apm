"""Per-HOST data tables of the egress security table (#4109).

Each module here defines ``SLICE``: one host of the E space (``ADO``,
``GHES``, ``GitLab``, ``generic``, ``ghe.com``, ``github``) as the lifecycle
world serves it, and its rows. ``tests/lifecycle/security/test_egress.py``
discovers every module and runs every row; add a module or a row, never
shared code.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EgressRow:
    # Exact obligation id, for example "EP|CRED=PAT|HOST=github" or
    # "E2|HOST=GHES|CRED=gh|EGRESS=clone".
    id: str
    cred: str  # PAT, gh, helper or none (test_egress.CREDENTIALS)
    egress: str  # the egress operation the row runs (test_egress.OPERATIONS)
    # True when the credential must reach the host (proof it was in play);
    # False when the host is served anonymously and APM may not send it.
    presented: bool = True
    smoke: bool = False
    # Strict expected failure: "S-pending (neutral issue to follow): <phrase>".
    xfail: str = ""
    # Exit(s) the decisive command may end with (a refused anonymous request is 1).
    exit: int | tuple[int, ...] = 0
    # The host refuses anonymous requests (Azure DevOps: ``require_ado_auth``).
    auth: bool = False


@dataclass(frozen=True)
class HostSlice:
    host: str  # the E-space HOST value
    git_host: str  # the host that serves its repositories (and the helper answers for)
    # Hosts a credential of this host belongs to (git and API origins).
    family: tuple[str, ...]
    base: str  # URL prefix of world repositories on this host, without a trailing slash
    spec: str  # dependency spec of p-skill on this host
    pat_env: str = ""  # the env variable APM reads a PAT for this host from
    env: Mapping[str, str] = field(default_factory=dict)  # extra env, e.g. GITHUB_HOST
    # Password the ``helper`` credential answers with ("" = a sentinel the host rejects).
    helper_password: str = ""
    # api-probe: the spec whose install reaches the host's REST API ("" = ``spec``),
    # and a setup that authors and serves it in the world (receives the World).
    api_spec: str = ""
    api_setup: Callable[[Any], None] | None = None
    rows: tuple[EgressRow, ...] = ()


def slices() -> tuple[HostSlice, ...]:
    """Every ``SLICE`` of this package, in module order."""
    found = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda item: item.name):
        found.append(importlib.import_module(f"{__name__}.{info.name}").SLICE)
    return tuple(found)
