"""Gate 9 call sites render their next step from the remediation catalog (#4090).

Each migrated site prints the whole catalog sentence, in the user's scope. These
assert the exact rendered text so a wording change in core/remediation.py shows
up here, next to the call site that prints it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.core.scope import InstallScope

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-remediation-advice")]


@pytest.mark.parametrize(
    ("user_scope", "reinstall", "remove"),
    [
        (
            False,
            "Move aside any listed file that holds your edits, then run 'apm install' to "
            "re-establish deployment evidence (once, with access to the package source), "
            "then rerun 'apm uninstall'.",
            "If the source is unreachable, move each listed file aside or delete it, then run "
            "'apm uninstall' to finish the uninstall.",
        ),
        (
            True,
            "Move aside any listed file that holds your edits, then run 'apm install -g' to "
            "re-establish deployment evidence (once, with access to the package source), "
            "then rerun 'apm uninstall -g'.",
            "If the source is unreachable, move each listed file aside or delete it, then run "
            "'apm uninstall -g' to finish the uninstall.",
        ),
    ],
)
def test_uninstall_evidence_remedies_keep_scope(user_scope, reinstall, remove) -> None:
    from apm_cli.integration.cleanup import uninstall_evidence_remedies

    assert uninstall_evidence_remedies(user_scope=user_scope) == (reinstall, remove)


@pytest.mark.parametrize(
    ("scope", "command"),
    [
        (None, "apm install <newer-bundle-file>"),
        (InstallScope.USER, "apm install -g <newer-bundle-file>"),
    ],
)
def test_local_bundle_update_names_the_bundle_install(scope, command: str) -> None:
    from apm_cli.install.outcome import local_bundle_update_next_step

    assert local_bundle_update_next_step(["p-exec"], 0, scope) == (
        "Local bundle p-exec not updated: 'apm update' refreshes apm.yml dependencies only. "
        f"Run '{command}' to update a bundle."
    )


def test_catalog_origin_next_step_offers_both_records() -> None:
    from apm_cli.marketplace.catalog_origin import _NEXT_STEP

    assert _NEXT_STEP == (
        "Nothing was changed. Run 'apm marketplace add <source>' to re-add the marketplace, "
        "or 'apm install' to record it again."
    )


def test_ref_swap_refusal_names_the_update_and_the_install_that_accept_it() -> None:
    from apm_cli.marketplace.version_pins import RefPinMismatchError

    assert str(RefPinMismatchError("p", "mkt", "a", "b", "p@mkt#1")).endswith(
        "refusing to install. If the publisher moved this version on purpose, then run "
        "'apm update' to accept the new ref (pin 'p@mkt#1'), or "
        "'apm install <package> --accept-ref-change' to accept it while adding the "
        "package that brings it."
    )


def test_pending_bundle_names_the_allow_step(capsys) -> None:
    from apm_cli.commands.approve import _show_pending_bundles

    bundle = SimpleNamespace(slug="b", exec_types=("hooks",), approval_key="k")
    _show_pending_bundles(Path("apm.yml"), [bundle])
    out = " ".join(capsys.readouterr().out.split())
    assert (
        "Review each bundle, run its approve command (the same grant as its key under "
        "executables.allow in apm.yml), then run 'apm install <bundle>' to deploy its "
        "executables."
    ) in out


def test_update_undeclared_package_and_lock_path_remedies_render() -> None:
    from apm_cli.core.remediation import (
        CLAUDE_NATIVE_PLUGIN_DRIFT,
        LOCKFILE_UNMANAGED_PATHS,
        UPDATE_UNDECLARED_PACKAGE,
    )

    assert UPDATE_UNDECLARED_PACKAGE.bind(package="typo-pkg").sentence() == (
        "Run 'apm install typo-pkg' to add it."
    )
    assert LOCKFILE_UNMANAGED_PATHS.clause() == "run 'apm install' to repair it, then retry"
    assert CLAUDE_NATIVE_PLUGIN_DRIFT.clause() == "run 'apm install --global' to restore it"
