"""Every CLI-visible deprecation keeps its text, stream and visibility (#4019).

The deprecation catalog (``core/deprecations.py``) owns the words; the domain
owners decide when a warning fires and the output owners render it. These
scenarios drive the installed ``apm`` and pin, for each deprecation the CLI
can reach, the exact text users see today, the stream it lands on, and that it
prints exactly once. The expected strings are literals on purpose: they were
captured before the text moved into the catalog, so a catalog edit that
changes user-visible wording fails here.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-user-facing-output-diagnostics"),
]

_MANIFEST = "name: p\nversion: 1.0.0\ndescription: d\n"
_SKILL = "---\nname: s\ndescription: s\n---\n# s\n"
_LOCAL_DEP = {
    "pkg/apm.yml": _MANIFEST,
    "pkg/.apm/skills/s/SKILL.md": _SKILL,
}


@dataclass(frozen=True)
class Scenario:
    """One command, the deprecation text it prints, and where it prints it."""

    files: dict[str, str]
    argv: tuple[str, ...]
    stream: str
    text: str
    env: dict[str, str] = field(default_factory=dict)


SCENARIOS: dict[str, Scenario] = {
    "config-set-auto-integrate": Scenario(
        {},
        ("config", "set", "auto-integrate", "true"),
        "stdout",
        "[!] 'auto-integrate' is deprecated and has no effect: no command reads it. "
        "It will be removed in a future release.",
    ),
    "config-get-auto-integrate": Scenario(
        {},
        ("config", "get", "auto-integrate"),
        "stdout",
        "[!] 'auto-integrate' is deprecated and has no effect: no command reads it. "
        "It will be removed in a future release.",
    ),
    "target-flag-vscode": Scenario(
        {"apm.yml": _MANIFEST},
        ("compile", "-t", "vscode", "--dry-run"),
        "stderr",
        "[!] --target: replace `vscode` with `copilot`; `vscode` is deprecated and "
        "will be removed in a future release.",
    ),
    "manifest-targets-vscode": Scenario(
        {"apm.yml": _MANIFEST + "targets: [vscode]\n"},
        ("compile", "--dry-run"),
        "stderr",
        "[!] apm.yml: replace `vscode` with `copilot` in 'targets:'; `vscode` is "
        "deprecated and will be removed in a future release.",
    ),
    "manifest-target-key": Scenario(
        {"apm.yml": _MANIFEST + "target: claude\n"},
        ("compile", "--dry-run"),
        "stderr",
        "[!] apm.yml uses the deprecated 'target:' key; 'target:' will be removed in a "
        "future release in favor of 'targets:'. Replace 'target: claude' with "
        "'targets: [claude]'.",
    ),
    "manifest-compilation-target": Scenario(
        {"apm.yml": _MANIFEST + "targets: [claude]\ncompilation:\n  target: claude\n"},
        ("compile", "--dry-run"),
        "stderr",
        "[!] apm.yml sets 'compilation.target', which APM ignores; targets come from "
        "'targets:' or auto-detection. Delete it; a future release will reject it.",
    ),
    "manifest-targets-all": Scenario(
        {"apm.yml": _MANIFEST + "targets: [all]\n", ".claude/.keep": ""},
        ("compile", "--dry-run"),
        "stdout",
        "[!] 'all' in apm.yml targets is deprecated and will become a hard error in a "
        "future release. APM is treating the field as omitted so the current command's "
        "default target selection applies. If you maintain this package, remove 'all' "
        "from the manifest.",
    ),
    "compile-target-all": Scenario(
        {"apm.yml": _MANIFEST},
        ("compile", "--target", "all", "--dry-run"),
        "stdout",
        "[!] '--target all' is deprecated; use '--all' instead.",
    ),
    "install-allow-executables": Scenario(
        {
            "apm.yml": _MANIFEST
            + "targets: [claude]\nallowExecutables:\n  acme/pkg:\n    hooks: true\n"
            + "dependencies:\n  apm:\n    - ./pkg\n",
            **_LOCAL_DEP,
        },
        ("install",),
        "stdout",
        "[!] 'allowExecutables' in apm.yml is deprecated; it now maps to "
        "'executables.allow'. It will migrate automatically on your next "
        "'apm approve'/'apm deny'.",
    ),
    "approve-allow-executables": Scenario(
        {"apm.yml": _MANIFEST + "allowExecutables:\n  acme/pkg:\n    hooks: true\n"},
        ("approve", "acme/pkg"),
        "stdout",
        "'allowExecutables' in apm.yml is deprecated; it now maps to "
        "'executables.allow'. It will migrate automatically on your next "
        "'apm approve'/'apm deny'.",
    ),
    "pack-target": Scenario(
        {
            "apm.yml": _MANIFEST + "targets: [claude]\n",
            ".apm/skills/s/SKILL.md": _SKILL,
        },
        ("pack", "--target", "claude"),
        "stdout",
        "[!] --target is deprecated and will be removed in a future release. Bundles are "
        "target-agnostic; the value is recorded as informational pack.target metadata "
        "only and is ignored by 'apm install'.",
    ),
    "unpack": Scenario(
        {"bundle.tar.gz": "not a bundle"},
        ("unpack", "bundle.tar.gz"),
        "stdout",
        "[!] 'apm unpack' is deprecated and will be removed in a future release. "
        "Repack with the default plugin format, then run 'apm install <bundle-path>' "
        "to install the bundle.",
    ),
    "deps-update": Scenario(
        {"apm.yml": _MANIFEST + "targets: [claude]\n"},
        ("deps", "update"),
        "stdout",
        "[!] 'apm deps update' is deprecated. Run 'apm update' to refresh dependencies "
        "with the supported command. 'apm update' now supports -g/--global, [PACKAGES]..., "
        "--force, --parallel-downloads, --target, and --legacy-skill-paths, plus an "
        "interactive plan, --dry-run, and --yes.",
    ),
    # --check keeps both self-updater shims read-only: they forward to
    # 'apm self-update --check', never to an install of a new binary.
    "update-outside-project": Scenario(
        {},
        ("update", "--check"),
        "stdout",
        "[!] 'apm update' refreshes APM dependencies. Run 'apm self-update' to update "
        "the APM CLI binary. Forwarding for back-compat (deprecated).",
    ),
    "update-check-in-project": Scenario(
        {"apm.yml": _MANIFEST},
        ("update", "--check"),
        "stdout",
        "[!] 'apm update --check' is the deprecated self-updater shim. Use 'apm update "
        "--dry-run' to preview dependency changes, or 'apm self-update --check' to check "
        "for a new CLI binary. Forwarding for back-compat (deprecated).",
    ),
    "marketplace-yml": Scenario(
        {
            "marketplace.yml": (
                "name: m\ndescription: d\nversion: 1.0.0\nowner:\n  name: o\npackages: []\n"
            )
        },
        ("marketplace", "check"),
        "stdout",
        "[!] marketplace.yml is deprecated. Run 'apm marketplace migrate' to fold "
        "marketplace.yml into apm.yml.",
    ),
    "target-agents": Scenario(
        {
            "apm.yml": _MANIFEST + "dependencies:\n  apm:\n    - ./pkg\n",
            ".github/.keep": "",
            **_LOCAL_DEP,
        },
        ("install", "--target", "agents"),
        "stdout",
        "[!] '--target agents' is deprecated -- it maps to 'copilot' (.github/), not "
        "'.agents/'. Use '--target copilot' or '--target agent-skills' "
        "(.agents/skills/). Removal in v1.0.",
    ),
    "mcp-angle-var": Scenario(
        {
            "apm.yml": _MANIFEST
            + "targets: [copilot]\ndependencies:\n  mcp:\n    - name: srv\n"
            + "      registry: false\n      transport: stdio\n      command: echo\n"
            + '      env:\n        TOK: "<TOKEN>"\n',
            ".github/.keep": "",
        },
        ("install",),
        "stdout",
        "[!] Deprecated: <VAR> placeholder syntax used in 1 server (srv). Migrate to "
        "${VAR} in apm.yml. <VAR> support will be removed in v1.0.",
    ),
}


def _collapse(text: str) -> str:
    """Rich wraps at the terminal width; compare wording, not line breaks."""
    return " ".join(text.split())


def _run(
    tmp_path: Path,
    apm_binary_path: Path,
    files: dict[str, str],
    argv: tuple[str, ...],
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    project = isolated.work_root / "proj"
    project.mkdir()
    for relative, text in files.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return subprocess.run(
        [str(apm_binary_path), *argv],
        cwd=project,
        env=isolated.subprocess_env(overrides=env or {}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_deprecation_warning_text_stream_and_visibility(
    name: str, tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = SCENARIOS[name]
    result = _run(tmp_path, apm_binary_path, scenario.files, scenario.argv, scenario.env)
    streams = {"stdout": result.stdout, "stderr": result.stderr}
    other = "stderr" if scenario.stream == "stdout" else "stdout"
    expected = _collapse(scenario.text)
    evidence = f"stdout={result.stdout!r}\nstderr={result.stderr!r}"

    assert _collapse(streams[scenario.stream]).count(expected) == 1, evidence
    assert expected not in _collapse(streams[other]), evidence


def test_approve_renders_allow_executables_without_a_symbol(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The approve path warns through the bare console helper, so no ``[!]``."""
    scenario = SCENARIOS["approve-allow-executables"]
    result = _run(tmp_path, apm_binary_path, scenario.files, scenario.argv)

    assert result.stdout.startswith("'allowExecutables' in apm.yml is deprecated"), result.stdout


def test_policy_bin_deploy_warning_is_reported_as_policy_data(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    files = {
        "apm.yml": _MANIFEST,
        "policy.yml": "name: p\nversion: 1.0.0\nbin_deploy:\n  deny_all: true\n",
    }
    result = _run(
        tmp_path,
        apm_binary_path,
        files,
        ("policy", "status", "--policy-source", "policy.yml", "--json"),
    )

    report = json.loads(result.stdout)
    assert report["warnings"] == [
        "'bin_deploy' is deprecated; use 'executables' (deny_all/deny) instead. "
        "bin_deploy is still honored as a bin-scoped deny alias for one minor cycle."
    ]
    assert "bin_deploy" not in result.stderr


_MARKETPLACE = _MANIFEST + "marketplace:\n  owner:\n    name: o\n  packages: []\n"
_OUTPUTS_LIST = _MARKETPLACE + "  outputs: [claude]\n"
_OUTPUTS_LIST_WARNING = "[!] outputs: [claude] is deprecated; use the map form: outputs: claude: {}"
# The legacy warning's wording is pinned by the "marketplace-yml" scenario.
_LEGACY_YML_WARNING = _collapse(SCENARIOS["marketplace-yml"].text.removeprefix("[!] "))
_LEGACY_YML_FILES = {
    "apm.yml": _MANIFEST,
    "marketplace.yml": "name: m\ndescription: d\nversion: 1.0.0\nowner:\n  name: o\npackages: []\n",
}


def _both(result: subprocess.CompletedProcess[str]) -> str:
    return _collapse(result.stdout + " " + result.stderr)


@pytest.mark.parametrize(
    "argv",
    [("pack",), ("pack", "--check-versions"), ("marketplace", "check")],
    ids=["pack", "pack-gate-reload", "marketplace-check"],
)
def test_marketplace_outputs_list_warning_prints_once(
    argv: tuple[str, ...], tmp_path: Path, apm_binary_path: Path
) -> None:
    """The collected list-form deprecation reaches the user exactly once (#4046)."""
    result = _run(tmp_path, apm_binary_path, {"apm.yml": _OUTPUTS_LIST}, argv)
    evidence = f"stdout={result.stdout!r}\nstderr={result.stderr!r}"

    assert result.returncode == 0, evidence
    assert _collapse(result.stdout).count(_OUTPUTS_LIST_WARNING) == 1, evidence
    assert _both(result).count("use the map form") == 1, evidence
    assert "v0.15" not in _both(result), evidence


def test_marketplace_outputs_map_form_prints_no_deprecation(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    manifest = _MARKETPLACE + "  outputs:\n    claude: {}\n"
    result = _run(tmp_path, apm_binary_path, {"apm.yml": manifest}, ("pack",))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "is deprecated" not in _both(result)


def test_marketplace_outputs_path_conflict_warning_prints_once(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    manifest = (
        _MARKETPLACE
        + "  outputs:\n    claude:\n      path: a/marketplace.json\n"
        + "  claude:\n    output: b/marketplace.json\n"
    )
    result = _run(tmp_path, apm_binary_path, {"apm.yml": manifest}, ("pack", "--dry-run"))
    expected = (
        "marketplace.outputs.claude.path ('a/marketplace.json') conflicts with "
        "marketplace.claude.output ('b/marketplace.json')."
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _both(result).count(expected) == 1, result.stdout + result.stderr


def test_legacy_marketplace_yml_warning_prints_once_when_pack_reloads(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The release gate's second config load repeats no load warning."""
    result = _run(tmp_path, apm_binary_path, _LEGACY_YML_FILES, ("pack", "--check-versions"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert _both(result).count(_LEGACY_YML_WARNING) == 1, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("files", "warning"),
    [
        ({"apm.yml": _OUTPUTS_LIST}, "outputs: [claude] is deprecated; use the map form"),
        (_LEGACY_YML_FILES, _LEGACY_YML_WARNING),
    ],
    ids=["outputs-list", "legacy-marketplace-yml"],
)
@pytest.mark.parametrize(
    "argv",
    [("pack", "--check-clean"), ("pack", "--check-clean", "--dry-run")],
    ids=["implicit-dry-run", "explicit-dry-run"],
)
def test_check_clean_prints_each_config_load_warning_once(
    argv: tuple[str, ...],
    files: dict[str, str],
    warning: str,
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """--check-clean alone skips producer rendering; load warnings still print once."""
    result = _run(tmp_path, apm_binary_path, files, argv)
    evidence = f"stdout={result.stdout!r}\nstderr={result.stderr!r}"

    # No marketplace.json exists yet, so the drift gate fails with exit 4.
    assert result.returncode == 4, evidence
    assert _both(result).count(warning) == 1, evidence


def test_pack_json_carries_the_outputs_list_warning_without_corrupting_stdout(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    result = _run(tmp_path, apm_binary_path, {"apm.yml": _OUTPUTS_LIST}, ("pack", "--json"))

    assert result.returncode == 0, result.stdout + result.stderr
    envelope = json.loads(result.stdout)
    matches = [w for w in envelope["warnings"] if "use the map form" in w]
    assert len(matches) == 1, envelope["warnings"]
    assert matches[0].startswith("outputs: [claude] is deprecated; use the map form:")
    assert "use the map form" not in result.stderr
