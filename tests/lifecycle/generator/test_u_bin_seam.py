"""U-BIN pin seam (#4894): a changed pinned element reinstates project bin cells.

The generator hashes the U-BIN elements (generator/bin_pin.py); a digest that
no longer matches ``rules.U_BIN_PIN`` makes every project-scope bin cell a
must-cover obligation. The seam is the digest function, so this unit replaces
it and leaves ``rules.classify`` real.
"""

from __future__ import annotations

import pytest

from tests.lifecycle.generator import bin_pin
from tests.lifecycle.generator.rules import Verdict, classify

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-OWN-executable-item-admission"),
]

_PROJECT_BIN = {"PRIM": "bin", "SCOPE": "project"}


def test_a_matching_pin_keeps_project_bin_cells_plain_obligations() -> None:
    assert classify("pair", _PROJECT_BIN) == Verdict("obligation", ())


def test_a_changed_pin_makes_the_project_bin_cells_must_cover(monkeypatch) -> None:
    monkeypatch.setattr(bin_pin, "predicate_digest", lambda *_: "0" * 64)
    assert classify("pair", _PROJECT_BIN) == Verdict("obligation", ("U-BIN",), must_cover=True)


def test_no_target_routes_a_bundle_bin_file() -> None:
    """Tripwire of the U-BIN-BUNDLE rule (#4095 lane T-1c): a local bundle never
    deploys bin/, at any scope, for any target.

    ``integrate_local_bundle`` (install/services.py) routes each packed file through
    ``bundle_deploy_relative_path``, which keeps only primitive files a target deploys;
    no target declares a ``bin`` primitive, and the only bin/ deploy decisions
    (``exec_gate.plugin_bin_deployable``) are made by the package pipeline, which the
    bundle side door never calls. If this starts routing a bin/ path, the bundle-install
    x bin cells become real deploy cells and the rule must go.
    """
    from apm_cli.install.local_bundle_paths import (
        bundle_deploy_relative_path,
        known_bundle_deploy_prefixes,
        target_bundle_deploy_prefixes,
    )
    from apm_cli.integration.targets import KNOWN_TARGETS

    assert not [name for name, target in KNOWN_TARGETS.items() if "bin" in target.primitives]
    tool = "p-native-claude-bin"
    paths = (f".agents/plugins/{tool}/bin/{tool}", f"bin/{tool}", f".agents/bin/{tool}")
    routed = {
        (name, path): deployed
        for name, target in sorted(KNOWN_TARGETS.items())
        for path in (*paths, *(f"{target.root_dir.strip('/')}/bin/{tool}",))
        if (
            deployed := bundle_deploy_relative_path(
                path,
                target_bundle_deploy_prefixes(target),
                known_bundle_deploy_prefixes(),
                target=target,
                hooks_approved=True,
                canvas_approved=True,
            )
        )
        is not None
    }
    assert not routed, routed
    # Anti-vacuity: the same call routes a primitive the claude target does deploy.
    claude = KNOWN_TARGETS["claude"]
    skill = bundle_deploy_relative_path(
        f".claude/skills/{tool}/SKILL.md",
        target_bundle_deploy_prefixes(claude),
        known_bundle_deploy_prefixes(),
        target=claude,
        hooks_approved=True,
        canvas_approved=True,
    )
    assert skill is not None


def test_the_model_expects_bin_only_for_a_claude_target() -> None:
    """bin/ has a Claude surface only: with claude and copilot declared, the model
    expects the plugin's bin/ once, for claude (exec_gate.plugin_bin_deployable)."""
    from tests.lifecycle.model.expected import expected_deployed
    from tests.lifecycle.model.state import ModelState

    model = ModelState(
        declared=frozenset({"p"}), targets=frozenset({"claude", "copilot"}), exec_gate=False
    )
    deployed = expected_deployed(model, {"p": frozenset({"bin", "skill"})})
    assert {target for _pkg, kind, target in deployed if kind == "bin"} == {"claude"}
    assert {target for _pkg, kind, target in deployed if kind == "skill"} == {"claude", "copilot"}
