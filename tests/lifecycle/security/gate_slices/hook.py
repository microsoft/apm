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
_LOCAL = ("definition-changed", "local-deny", "revoke-approval")
_BUNDLE = tuple(change for change in _ALL if change != "policy-dep-deny")
_XFAIL: dict[str, str] = {}
_ANNOUNCE = {
    # A bundle approval pins the whole bundle digest: a changed definition reads
    # as a bundle that is not approved yet.
    "G|bundle-install|definition-changed|hook": r"not approved for this exact bundle",
    # Uninstall refreshes the changed local survivor from its source and says so,
    # then retires the redefined hook (#4446).
    "G|uninstall-prune-rebuild|definition-changed|hook": (
        r"(?s)(?=.*Refreshed local package \S+ from its source)"
        r"(?=.*hooks are no longer allowed)(?=.*definition changed since approval)"
    ),
}
_SMOKE = {"G|install|revoke-approval|hook", "G|frozen|local-deny|hook"}

# G path -> (changes with a row, surface files)
_MARKETPLACE = tuple(change for change in _ALL if change != "definition-changed")
# User scope discovers no org policy (docs/src/content/docs/enterprise/apm-policy.mdx;
# core/scope.policy_discovery_uses_git_remote); user-scope org policy is #3703.
_USER_UNGOVERNED = "user scope is not governed by org policy (#3703)"
_UNGOVERNED = {
    f"G|marketplace-install|{change}|hook": _USER_UNGOVERNED
    for change in ("org-deny", "org-enables-gate", "policy-dep-deny")
}

_PATHS = {
    "install": (_ALL, _PROJECT),
    "frozen": (_ALL, _PROJECT),
    "update": (_ALL, _PROJECT),
    "uninstall-prune-rebuild": (_ALL, _PROJECT),
    "audit-ci": (_ALL, _PROJECT),
    "dry-run": (("local-deny", "org-deny", "revoke-approval"), _PROJECT),
    "install-g": (_LOCAL, _USER),
    "frozen-g": (_LOCAL, _USER),
    # gate_paths/bundle_install.py: the hook of the local bundle p-exec:bundle.
    "bundle-install": (_BUNDLE, _PROJECT),
    # gate_paths/marketplace_install.py: p-exec@lifecycle-market at user scope. A
    # git-served package cannot be redefined in place: definition-changed is residual.
    "marketplace-install": (_MARKETPLACE, _USER),
}

SLICE = GateSlice(
    kind="hook",
    package="p-exec",
    target="copilot",
    rows=tuple(
        GateRow(
            f"G|{path}|{change}|hook",
            surface=surface,
            announce=_ANNOUNCE.get(f"G|{path}|{change}|hook"),
            smoke=f"G|{path}|{change}|hook" in _SMOKE,
            xfail=_XFAIL.get(f"G|{path}|{change}|hook", ""),
            ungoverned=_UNGOVERNED.get(f"G|{path}|{change}|hook", ""),
        )
        for path, (changes, surface) in _PATHS.items()
        for change in changes
    ),
)
