"""Gate removal rows for KIND hook: p-exec's SessionStart hook file (#4107)."""

from __future__ import annotations

from tests.lifecycle.security.gate_slices import GateRow, GateSlice

_PROJECT = {"project": (".github/hooks/p-exec-session.json",)}
_USER = {"home": (".copilot/hooks/p-exec-session.json",)}
_ALL = (
    "definition-changed",
    "local-deny",
    "org-deny",
    "org-enables-gate",
    "policy-dep-deny",
    "revoke-approval",
)
_UPDATE_REDEFINED = (
    "#0 apm update keeps a hook deployed after its definition changed since approval; "
    "apm install retires it"
)
_UPDATE_DEP_DENY = (
    "#0 apm update exits 0 and keeps the package deployed when the org policy denies a "
    "declared dependency; apm install refuses it"
)
_UNINSTALL_SIBLING = (
    "#0 apm uninstall of another package keeps a hook deployed that the gate no longer "
    "allows; the next apm install retires it"
)
_XFAIL = {
    "G|update|definition-changed|hook": _UPDATE_REDEFINED,
    "G|update|policy-dep-deny|hook": _UPDATE_DEP_DENY,
    **{f"G|uninstall-prune-rebuild|{change}|hook": _UNINSTALL_SIBLING for change in _ALL},
}
_SMOKE = {"G|install|revoke-approval|hook", "G|frozen|local-deny|hook"}

_PATHS = {
    "install": (_ALL, _PROJECT),
    "frozen": (_ALL, _PROJECT),
    "update": (_ALL, _PROJECT),
    "uninstall-prune-rebuild": (_ALL, _PROJECT),
    "audit-ci": (_ALL, _PROJECT),
    "dry-run": (("local-deny", "org-deny", "revoke-approval"), _PROJECT),
    "install-g": (("definition-changed", "local-deny", "revoke-approval"), _USER),
    "frozen-g": (("definition-changed", "local-deny", "revoke-approval"), _USER),
    # A packed bundle cannot change in place: no definition-changed row.
    "bundle-install": (
        ("local-deny", "org-deny", "org-enables-gate", "revoke-approval"),
        _PROJECT,
    ),
    # A published tag cannot change in place: no definition-changed row.
    "marketplace-install": (
        ("local-deny", "org-deny", "org-enables-gate", "policy-dep-deny", "revoke-approval"),
        _PROJECT,
    ),
}

SLICE = GateSlice(
    kind="hook",
    package="p-exec",
    target="copilot",
    rows=tuple(
        GateRow(
            f"G|{path}|{change}|hook",
            absent=absent,
            smoke=f"G|{path}|{change}|hook" in _SMOKE,
            xfail=_XFAIL.get(f"G|{path}|{change}|hook", ""),
        )
        for path, (changes, absent) in _PATHS.items()
        for change in changes
    ),
)
