"""Static regression proofs for the Windows held-directory release guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-windows-held-directory-release"),
]

ROOT = Path(__file__).resolve().parents[2]
RELEASE = "transport-platform-held-directory-release"
REGISTRATION = "transport-platform-held-root-registration"


def _report(rule: str, path: str, old: str | None, new: str):
    source = (ROOT / path).read_text(encoding="utf-8")
    if old is None:
        mutated = source + new
    else:
        assert source.count(old) == 1, f"fragment drifted in {path}: {old!r}"
        mutated = source.replace(old, new, 1)
    report = run_selected_rules(ROOT, (rule,), source_overrides={path: mutated})
    assert report.failures == ()
    return report


def _expect_violation(report, rule: str) -> None:
    """The guard reports *rule* (the linter's user-visible failure)."""
    assert any(violation.rule_id == rule for violation in report.violations), report.violations


def _expect_clean(report) -> None:
    """The guard reports no failure and no violation."""
    assert report.failures == ()
    assert report.violations == ()


@pytest.mark.parametrize(
    ("rule", "path", "old", "new"),
    [
        pytest.param(
            RELEASE,
            "src/apm_cli/commands/prune.py",
            None,
            "\n\ndef _wipe(path):\n    import shutil as sh\n\n    sh.rmtree(path)\n",
            id="aliased-shutil-rmtree",
        ),
        pytest.param(
            RELEASE,
            "src/apm_cli/commands/update.py",
            None,
            "\n\ndef _move(a, b):\n    from os import rename as mv\n\n    mv(a, b)\n",
            id="from-os-import-rename",
        ),
        pytest.param(
            RELEASE,
            "src/apm_cli/commands/uninstall/local_reanchor.py",
            "            release_held(parent)\n",
            "",
            id="journal-parent-rmdir",
        ),
        pytest.param(
            RELEASE,
            "src/apm_cli/integration/write_seam.py",
            "    release_held(path)\n    shutil.rmtree(path, **kwargs)\n",
            "    shutil.rmtree(path, **kwargs)\n",
            id="write-seam-rmtree",
        ),
        pytest.param(
            RELEASE,
            "src/apm_cli/install/journal_modes.py",
            "            release_held(target)\n",
            "",
            id="unreleased-os-symlink",
        ),
        pytest.param(
            RELEASE,
            "src/apm_cli/commands/prune.py",
            None,
            "\n\ndef _link(a, b):\n    a.symlink_to(b)\n",
            id="unreleased-symlink-to",
        ),
        pytest.param(
            RELEASE,
            "src/apm_cli/utils/file_ops.py",
            "            os.rmdir(path)  # a directory symlink or junction on Windows\n",
            "            os.unlink(path)\n",
            id="stale-allowlist-entry",
        ),
        pytest.param(
            REGISTRATION,
            "src/apm_cli/commands/uninstall/engine.py",
            None,
            '\n\n_REGISTER = getattr(object, "register_held_root", None)\n',
            id="registration-by-name",
        ),
    ],
)
def test_held_directory_guards_reject_unreleased_mutations(
    rule: str, path: str, old: str | None, new: str
) -> None:
    """A raw mutation without a release, a stale allowlist entry or a side-door registrar fails."""
    _expect_violation(_report(rule, path, old, new), rule)


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param('\n\ndef _norm(name):\n    return name.replace("/", "_")\n', id="str-replace"),
        pytest.param("\n\ndef _naive(moment):\n    return moment.replace(tzinfo=None)\n", id="kw"),
    ],
)
def test_held_directory_release_ignores_non_directory_replace(extra: str) -> None:
    """``str.replace`` and keyword ``replace`` calls are not directory mutations."""
    _expect_clean(_report(RELEASE, "src/apm_cli/commands/update.py", None, extra))


def test_held_directory_guards_accept_current_sources() -> None:
    """Both rules are silent for production routing before mutation."""
    _expect_clean(run_selected_rules(ROOT, (RELEASE, REGISTRATION)))


LINK_COPY = "transport-platform-link-copy-release"
_PRUNE = "src/apm_cli/commands/prune.py"


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(
            "\n\ndef _copy(a, b):\n    import shutil\n\n    shutil.copytree(a, b, symlinks=True)\n",
            id="unreleased",
        ),
        pytest.param(
            "\n\ndef _copy(a, b, cond):\n    import shutil\n\n"
            "    if cond:\n        release_held(b)\n    shutil.copytree(a, b, symlinks=True)\n",
            id="conditional-release",
        ),
        pytest.param(
            "\n\ndef _copy(a, b, cond):\n    import shutil\n\n"
            "    if cond:\n        release_held(b)\n    else:\n"
            "        shutil.copytree(a, b, symlinks=True)\n",
            id="sibling-branch-release",
        ),
        pytest.param(
            "\n\ndef _copy(a, b):\n    from shutil import copytree as ct\n\n    ct(a, b, True)\n",
            id="aliased-positional-true",
        ),
    ],
)
def test_link_copy_rule_rejects_an_undominated_release(extra: str) -> None:
    """A tree copy with links needs a release on every path to it."""
    _expect_violation(_report(LINK_COPY, _PRUNE, None, extra), LINK_COPY)


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(
            "\n\ndef _copy(a, b):\n    import shutil\n\n"
            "    release_held(b)\n    shutil.copytree(a, b, symlinks=True)\n",
            id="dominating-release",
        ),
        pytest.param(
            "\n\ndef _copy(a, b):\n    import shutil\n\n    shutil.copytree(a, b, False)\n",
            id="positional-false",
        ),
        pytest.param(
            "\n\ndef _copy(a, b):\n    import shutil\n\n    shutil.copytree(a, b)\n",
            id="follows-links",
        ),
    ],
)
def test_link_copy_rule_accepts_a_dominating_release_or_no_links(extra: str) -> None:
    """A release that runs first, or a copy that creates no links, passes."""
    _expect_clean(_report(LINK_COPY, _PRUNE, None, extra))


def test_link_copy_rule_accepts_current_sources() -> None:
    """The link-copy rule is silent for production routing."""
    _expect_clean(run_selected_rules(ROOT, (LINK_COPY,)))
