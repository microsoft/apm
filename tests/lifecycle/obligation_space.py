"""Generated obligation space of the lifecycle security tables and journeys.

Obligations come from the model, never from the tables that cover them:
``tests/quality/security_obligations.json`` is the reviewed export of the
security space (G gate removal, P policy outcomes, I integrity, E egress,
M marketplace origin, C credentials). Compact spaces are expanded here,
exactly as the export describes them. Journey obligations are one
``(journey, step, oracle)`` tuple per step oracle in
``tests/lifecycle/journeys/index.yaml``.

A table row references these exact ids; an id with no witness stays in the
ratchet baseline as uncovered.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from itertools import product
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
EXPORT_PATH = _ROOT / "tests" / "quality" / "security_obligations.json"
JOURNEY_INDEX_PATH = _ROOT / "tests" / "lifecycle" / "journeys" / "index.yaml"

EXPECTED_COUNTS = {"G": 244, "P": 224, "I": 116, "E": 281, "M": 38, "C": 22}

_CACHE_CLASSES = ("cache=cold", "cache=stale<7d", "cache=stale>7d-or-expired", "cache=warm-fresh")
_P_FULL_SITES = (
    "approve",
    "audit-ci",
    "audit-exec-replay(getattr)",
    "bundle-allow-map(getattr cache_only)",
    "exec-map(getattr: uninstall/hook rebuild/lsp)",
    "mcp-admission(trust.py)",
    "policy-status",
    "update-regate-shortcircuit",
)
_P_CACHE_SITES = (
    "bundle-install-preflight(cache_only)",
    "install-dry-run-preflight",
    "install-pipeline",
    "lifecycle-scripts",
    "mcp-install-preflight",
)
_P_CACHE_SITE_CLASSES = (
    *_CACHE_CLASSES,
    "cache_miss_fetch_fail/block",
    "cache_miss_fetch_fail/warn",
)

# Approved P collapse (steward decision): once the policy-outcome routing owner
# (H1) lands, per-site P ids map to owner outcome x enforcement plus one routing
# E2E row per site. Until then this stays False and every per-site id is its own
# obligation. P ids are never deleted, only mapped.
P_COLLAPSE_APPROVED_AFTER_H1 = False

_I_OPS5 = ("audit-ci", "frozen", "install", "uninstall-prune", "update")
_I_TAMPER4 = ("disk-or-cache", "lock-edited", "missing-hash", "upstream-or-proxy")

_E_CREDS = ("PAT", "az-bearer", "gh", "helper", "none")
_E_EGRESS = ("api-probe", "clone", "marketplace-fetch", "mcp-registry", "policy-fetch", "registry")
_E_HOSTS = ("ADO", "GHES", "GitLab", "generic", "ghe.com", "github")
_E_REDIRECT = ("cross-origin", "none", "same-origin")
_E_FOUR = ("api-probe", "clone", "marketplace-fetch", "policy-fetch")

_M_OPS = (
    "audit-ci",
    "dry-run",
    "install object-form",
    "install plugin@mkt",
    "outdated/view",
    "transitive marketplace dep",
    "update",
)
_M_EVENTS = (
    "alias-rebind",
    "catalog-unreachable",
    "confusion-sentinel(cross-repo bare name)",
    "duplicate-origin-registration",
    "lock-row-edited",
)


def load_export(path: Path = EXPORT_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _expand_p(owner_ids: Iterable[str]) -> list[str]:
    classes = [item.removeprefix("P|owner|") for item in owner_ids]
    rows = [f"P|{site}|{cls}" for site in _P_FULL_SITES for cls in (*classes, *_CACHE_CLASSES)]
    rows += [f"P|{site}|{cls}" for site in _P_CACHE_SITES for cls in _P_CACHE_SITE_CLASSES]
    return rows


def _expand_i() -> list[str]:
    rows = [
        f"I|approval-pins|{op}|{t}"
        for op in ("audit-ci", "bundle-install", "frozen", "install", "uninstall-prune", "update")
        for t in ("disk-or-cache", "lock-edited", "upstream-or-proxy")
    ]
    rows += [
        f"I|bundle-files|bundle-install|{t}"
        for t in ("lock-edited", "missing-hash", "upstream-or-proxy")
    ]
    for art in (
        "deployed_file_hashes",
        "git-content_hash",
        "marketplace-origin(source_url/digest)",
    ):
        rows += [f"I|{art}|{op}|{t}" for op in _I_OPS5 for t in _I_TAMPER4]
    rows += [
        f"I|path-containment|{op}|{t}"
        for op in ("bundle-install", "install", "uninstall-prune", "update")
        for t in ("disk-or-cache", "upstream-or-proxy")
    ]
    rows += [
        f"I|policy-hash-pin|{op}|{t}"
        for op in ("audit-ci", "bundle-install", "frozen", "install", "update")
        for t in ("disk-or-cache", "missing-hash", "upstream-or-proxy")
    ]
    rows += [
        f"I|registry-resolved_hash|{op}|{t}"
        for op in ("frozen", "install", "update")
        for t in _I_TAMPER4
    ]
    return rows


_E_CRED_EGRESS = {
    "PAT": ("api-probe", "clone", "marketplace-fetch", "policy-fetch", "registry"),
    "az-bearer": ("api-probe", "clone", "policy-fetch"),
    "gh": _E_FOUR,
    "helper": _E_FOUR,
    "none": ("api-probe", "clone", "marketplace-fetch", "mcp-registry", "policy-fetch", "registry"),
}
_E_EGRESS_HOSTS = {
    "api-probe": ("ADO", "GHES", "GitLab", "ghe.com", "github"),
    "policy-fetch": ("ADO", "GHES", "GitLab", "ghe.com", "github"),
    "clone": ("ADO", "GHES", "GitLab", "ghe.com", "github", "generic"),
    "marketplace-fetch": ("ADO", "GHES", "GitLab", "ghe.com", "github", "generic"),
    "mcp-registry": ("generic",),
    "registry": ("generic",),
}


def _expand_e_evidence() -> list[str]:
    e1 = [
        f"E1|CRED={c}|EGRESS={e}|REDIRECT={r}"
        for c, e, r in product(("PAT", "gh", "helper"), _E_FOUR, _E_REDIRECT)
    ]
    e1 += [f"E1|CRED=PAT|EGRESS=registry|REDIRECT={r}" for r in _E_REDIRECT]
    e1 += [
        f"E1|CRED=az-bearer|EGRESS={e}|REDIRECT={r}"
        for e, r in product(("api-probe", "clone", "policy-fetch"), _E_REDIRECT)
    ]
    e2: list[str] = []
    host_creds = {
        "ADO": ("PAT", "helper", "none"),
        "GHES": ("PAT", "gh", "helper", "none"),
        "GitLab": ("PAT", "helper", "none"),
        "ghe.com": ("PAT", "gh", "none"),
        "github": ("PAT", "gh", "none"),
    }
    for host, creds in host_creds.items():
        e2 += [f"E2|HOST={host}|CRED={c}|EGRESS={e}" for c, e in product(creds, _E_FOUR)]
    e2 += [f"E2|HOST=ADO|CRED=az-bearer|EGRESS={e}" for e in ("api-probe", "clone", "policy-fetch")]
    generic = {
        "PAT": ("clone", "marketplace-fetch", "registry"),
        "helper": ("clone", "marketplace-fetch"),
        "none": ("clone", "marketplace-fetch", "mcp-registry", "registry"),
    }
    e2 += [f"E2|HOST=generic|CRED={c}|EGRESS={e}" for c, egress in generic.items() for e in egress]
    ep = [f"EP|CRED={c}|EGRESS={e}" for c, egress in _E_CRED_EGRESS.items() for e in egress]
    ep += [f"EP|CRED={c}|PROXY_ONLY={p}" for c, p in product(_E_CREDS, ("0", "1"))]
    ep += [f"EP|CRED={c}|REDIRECT={r}" for c, r in product(_E_CREDS, _E_REDIRECT)]
    ep += [f"EP|EGRESS={e}|HOST={h}" for e, hosts in _E_EGRESS_HOSTS.items() for h in hosts]
    ep += [f"EP|EGRESS={e}|PROXY_ONLY={p}" for e, p in product(_E_EGRESS, ("0", "1"))]
    ep += [f"EP|EGRESS={e}|REDIRECT={r}" for e, r in product(_E_EGRESS, _E_REDIRECT)]
    ep += [f"EP|HOST={h}|PROXY_ONLY={p}" for h, p in product(_E_HOSTS, ("0", "1"))]
    ep += [f"EP|HOST={h}|REDIRECT={r}" for h, r in product(_E_HOSTS, _E_REDIRECT)]
    ep += [f"EP|PROXY_ONLY={p}|REDIRECT={r}" for p, r in product(("0", "1"), _E_REDIRECT)]
    return e1 + e2 + ep


def _expand_m() -> list[str]:
    rows = [f"M|{op}|{event}" for op, event in product(_M_OPS, _M_EVENTS)]
    rows += [
        f"M|frozen|{event}"
        for event in (
            "catalog-unreachable",
            "confusion-sentinel(cross-repo bare name)",
            "lock-row-edited",
        )
    ]
    return rows


JOURNEY_LANES = {"consumer": "T-8a", "producer": "T-8b", "enterprise": "T-8c"}
SPACE_LANES = {"G": "T-7a", "P": "T-7b", "I": "T-7c", "E": "T-7c", "M": "T-7c", "C": "T-7c"}


def security_obligations(export: dict | None = None) -> dict[str, dict[str, str]]:
    """Every security obligation id per space, mapped to its required strength."""
    ids = (export or load_export())["ids"]
    space: dict[str, dict[str, str]] = {
        "G": dict.fromkeys(ids["G"]["e2e-evidence"], "e2e-evidence"),
        "P": {
            **dict.fromkeys(ids["P"]["unit"], "unit"),
            **dict.fromkeys(_expand_p(ids["P"]["unit"]), "e2e"),
        },
        "I": dict.fromkeys(_expand_i(), "e2e"),
        "E": {
            **dict.fromkeys(ids["E"]["e2e"], "e2e"),
            **dict.fromkeys(_expand_e_evidence(), "e2e-evidence"),
        },
        "M": dict.fromkeys(_expand_m(), "e2e"),
        "C": dict.fromkeys(ids["C"]["e2e-evidence"], "e2e-evidence"),
    }
    return space


def journey_obligations(index_path: Path = JOURNEY_INDEX_PATH) -> list[str]:
    """One ``J|<journey>|<step>|<oracle>`` id per step oracle of the journey index.

    Empty until the journey index exists, so the core can land before it.
    """
    if not index_path.is_file():
        return []
    index = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    return [
        f"J|{journey['id']}|{step['n']}|{oracle}"
        for journey in index["journeys"]
        for step in journey["steps"]
        for oracle in step.get("oracles", ())
    ]


def journey_lanes(index_path: Path = JOURNEY_INDEX_PATH) -> dict[str, str]:
    """Journey id -> owning lane, from the persona of each index journey."""
    if not index_path.is_file():
        return {}
    index = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    return {journey["id"]: JOURNEY_LANES[journey["persona"]] for journey in index["journeys"]}


def generated_obligations() -> dict[str, tuple[str, str]]:
    """Every generated obligation id -> (required strength, owning lane)."""
    rows: dict[str, tuple[str, str]] = {}
    for space, ids in security_obligations().items():
        for key, strength in ids.items():
            rows[key] = (strength, SPACE_LANES[space])
    lanes = journey_lanes()
    for key in journey_obligations():
        rows[key] = ("e2e", lanes[key.split("|")[1]])
    return rows
