"""User-scope hook commands anchor to home only where the harness shell expands it (#2821) (pure logic cases)."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from apm_cli.integration import targets as targets_mod
from apm_cli.integration.hook_command_paths import splice_references
from apm_cli.integration.targets import KNOWN_TARGETS, UserHookHomeReference

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise(
        "P-OWN-plugin-root-hook-command-vocabulary",
        "P-OWN-user-root-scoped-instruction-eligibility",
    ),
]

_VERIFIED = {
    "claude": ("command", "bash"),
    "copilot": ("bash",),
    "gemini": ("command",),
    "kiro": ("command", "bash"),
}
_UNVERIFIED_WITH_USER_HOOKS = ("codex", "cursor", "windsurf")


class TestCapabilityCatalog:
    def test_only_documented_shell_harnesses_enable_home_reference(self) -> None:
        enabled = {
            name: profile.user_hook_home_reference.shell_selectors
            for name, profile in KNOWN_TARGETS.items()
            if profile.user_hook_home_reference is not None
        }
        assert enabled == _VERIFIED
        for name in _UNVERIFIED_WITH_USER_HOOKS:
            assert KNOWN_TARGETS[name].supports_at_user_scope("hooks")
            assert KNOWN_TARGETS[name].user_hook_home_reference is None

    @pytest.mark.parametrize("name", sorted(_VERIFIED))
    def test_enabled_capability_cites_official_docs(self, name: str) -> None:
        reference = KNOWN_TARGETS[name].user_hook_home_reference
        assert reference is not None
        assert reference.reference == "$HOME"
        assert urlparse(reference.evidence).scheme == "https"
        assert KNOWN_TARGETS[name].for_scope(user_scope=True).user_hook_home_reference is reference

    def test_reference_requires_shell_field_quote_and_posix_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reference = UserHookHomeReference(shell_selectors=("bash",), evidence="https://x.test")
        monkeypatch.setattr(targets_mod, "_posix_shell_host", lambda: True)
        assert reference.reference_for("bash", quote='"') == "$HOME"
        assert reference.reference_for("bash", quote="") == "$HOME"
        assert reference.reference_for("bash", quote="'") is None
        assert reference.reference_for("powershell", quote="") is None
        assert reference.for_handler({"command": "./run.sh"}) is reference
        assert reference.for_handler({"command": "./run.sh", "args": []}) is None
        monkeypatch.setattr(targets_mod, "_posix_shell_host", lambda: False)
        assert reference.reference_for("bash", quote="") is None


def test_splice_references_rejects_overlapping_edits() -> None:
    assert splice_references("a b c", [(4, 5, "z"), (0, 1, "x")]) == "x b z"
    with pytest.raises(ValueError, match="overlapping"):
        splice_references("abcdef", [(0, 3, "x"), (2, 4, "y")])
