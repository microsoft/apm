"""Regression tests for VS Code and MCP Registry v0.1 container run arguments.

Issue #2377: ``apm install --target vscode`` prompted for a Docker server's
``workdir`` variable, reported success, and then wrote a ``.vscode/mcp.json``
launcher with every registry-supplied run option missing -- including the bind
mount the user had just filled in.

``_extract_package_args`` walked ``runtime_arguments`` looking only at
``value_hint``. The v0.1 spec spells the payload ``value`` (beside a ``type``),
so every entry was skipped and the docker branch fell back to a synthesized
``run -i --rm <image>``.

That extractor is shared with the npm, pypi, and generic branches, which read
an empty extraction as "synthesize the command from the package name" -- so it
cannot simply learn the v0.1 shape without stripping the package name out of
those launchers. Container arguments are assembled by ``_docker_run_args``
instead, and the tests below pin both halves of that contract.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import pytest

from apm_cli.adapters.client.vscode import VSCodeClientAdapter

pytestmark = pytest.mark.unit

IMAGE = "myregistry.example.com/team/mcp-server:1.4.0"
WORKDIR = "/home/dev/project"
RUNTIME_VARS = {"workdir": WORKDIR}

_WORKDIR_VAR = {
    "workdir": {
        "description": "Working directory to mount into the container.",
        "isRequired": True,
        "format": "filepath",
    }
}

# The reporting registry's shape: `value` + `type`, image only in `name`.
V01_VALUE_PACKAGE = {
    "name": IMAGE,
    "registry_name": "oci",
    "runtime_hint": "docker",
    "runtime_arguments": [
        {"value": "run", "type": "positional"},
        {"value": "--rm", "type": "positional"},
        {"value": "-i", "type": "positional"},
        {"value": "-v", "type": "positional"},
        {"value": "{workdir}:{workdir}", "type": "positional", "variables": _WORKDIR_VAR},
        {"value": "-w", "type": "positional"},
        {"value": "{workdir}", "type": "positional", "variables": _WORKDIR_VAR},
    ],
}

EXPECTED_ARGS = ["run", "--rm", "-i", "-v", f"{WORKDIR}:{WORKDIR}", "-w", WORKDIR, IMAGE]

FALLBACK_ARGS = ["run", "-i", "--rm", IMAGE]


def _make_vscode() -> VSCodeClientAdapter:
    with (
        patch("apm_cli.adapters.client.vscode.SimpleRegistryClient"),
        patch("apm_cli.adapters.client.vscode.RegistryIntegration"),
    ):
        return VSCodeClientAdapter(project_root="/tmp/workspace")


def _docker_package(*runtime_arguments, name=IMAGE):
    return {
        "name": name,
        "registry_name": "docker",
        "runtime_hint": "docker",
        "runtime_arguments": list(runtime_arguments),
    }


def _config(package, **kwargs):
    config, _inputs = _make_vscode()._format_server_config(
        {"id": "team/mcp-server", "name": "team/mcp-server", "packages": [package]}, **kwargs
    )
    return config


# ---------------------------------------------------------------------------
# _docker_run_args
# ---------------------------------------------------------------------------


class TestDockerRunArgs(unittest.TestCase):
    """v0.1 ``value`` entries become run options; the image is always named."""

    def test_value_entries_become_run_options(self):
        args = VSCodeClientAdapter._docker_run_args(V01_VALUE_PACKAGE, RUNTIME_VARS)
        self.assertEqual(args, EXPECTED_ARGS)

    def test_collected_variable_reaches_the_mount_argument(self):
        """The value APM prompted for must not be silently discarded (#2377)."""
        args = VSCodeClientAdapter._docker_run_args(V01_VALUE_PACKAGE, RUNTIME_VARS)
        self.assertIn(f"{WORKDIR}:{WORKDIR}", args)

    def test_typed_entries_read_value_not_the_display_hint(self):
        """`value_hint` is the schema's display hint for a typed argument.

        Real registry data carries both keys -- see the GitHub MCP fixture in
        tests/test_codex_docker_args_fix.py -- so preferring the hint renders
        placeholder words like `env_var_name` as CLI arguments.
        """
        package = _docker_package(
            {"type": "positional", "value": "run", "value_hint": "docker_cmd"},
            {"type": "positional", "value": "-i", "value_hint": "interactive_flag"},
            {"type": "named", "name": "-e", "value": "GITHUB_TOKEN", "value_hint": "env_var_name"},
        )
        self.assertEqual(
            VSCodeClientAdapter._docker_run_args(package),
            ["run", "--rm", "-i", "-e", "GITHUB_TOKEN", IMAGE],
        )

    def test_valueless_named_flags_survive(self):
        """Dropping a bare `--read-only` weakens the container's posture."""
        package = _docker_package(
            {"type": "positional", "value": "run"},
            {"type": "named", "name": "--read-only"},
            {"type": "named", "name": "--network", "value": "none"},
        )
        self.assertEqual(
            VSCodeClientAdapter._docker_run_args(package),
            ["run", "-i", "--rm", "--read-only", "--network", "none", IMAGE],
        )

    def test_required_value_taking_option_without_value_declines(self):
        package = _docker_package(
            {"type": "positional", "value": "run"},
            {"type": "named", "name": "--user"},
        )
        self.assertIsNone(VSCodeClientAdapter._docker_run_args(package))

    def test_optional_value_taking_option_without_value_is_dropped(self):
        package = _docker_package(
            {"type": "positional", "value": "run"},
            {
                "type": "named",
                "name": "--network",
                "is_required": False,
                "variables": {},
            },
        )
        self.assertEqual(
            VSCodeClientAdapter._docker_run_args(package),
            ["run", "-i", "--rm", IMAGE],
        )

    def test_default_is_used_when_value_is_absent(self):
        package = _docker_package(
            {"type": "positional", "value": "run"},
            {"type": "positional", "default": "--fromdefault"},
        )
        self.assertIn("--fromdefault", VSCodeClientAdapter._docker_run_args(package))

    def test_declines_when_the_run_verb_is_not_first(self):
        """`docker -v run …` is not a run invocation."""
        package = _docker_package(
            {"type": "positional", "value": "-v"},
            {"type": "positional", "value": "run"},
        )
        self.assertIsNone(VSCodeClientAdapter._docker_run_args(package))

    def test_placeholder_absent_from_the_template_does_not_decline(self):
        package = _docker_package(
            {"type": "positional", "value": "run"},
            {
                "type": "positional",
                "value": "--flag",
                "variables": {"unused": {"description": "x"}},
            },
        )
        self.assertEqual(
            VSCodeClientAdapter._docker_run_args(package), ["run", "-i", "--rm", "--flag", IMAGE]
        )

    def test_a_collected_zero_resolves_rather_than_declining(self):
        """`0` is a supplied value; only an unset or empty one is unresolved."""
        package = _docker_package(
            {"type": "positional", "value": "run"},
            {"type": "positional", "value": "--port={port}", "variables": {"port": {}}},
        )
        self.assertIn("--port=0", VSCodeClientAdapter._docker_run_args(package, {"port": 0}))

    def test_legacy_full_argv_in_package_arguments_is_not_appended_twice(self):
        """Registry data that repeats the docker argv there is a duplicate, not
        container arguments -- appending it hands docker a second `run`."""
        package = _docker_package(
            {"type": "positional", "value": "run"},
            {"type": "named", "name": "-e", "value": "TOK"},
        )
        package["package_arguments"] = [
            {"type": "positional", "value": "run"},
            {"type": "positional", "value": IMAGE},
        ]
        self.assertEqual(
            VSCodeClientAdapter._docker_run_args(package),
            ["run", "-i", "--rm", "-e", "TOK", IMAGE],
        )

    def test_pinned_image_in_package_arguments_is_not_appended_as_container_argv(self):
        package = _docker_package({"type": "positional", "value": "run"})
        package["package_arguments"] = [
            {"type": "positional", "value": "myregistry.example.com/team/mcp-server:latest"}
        ]
        self.assertEqual(
            VSCodeClientAdapter._docker_run_args(package),
            ["run", "-i", "--rm", IMAGE],
        )

    def test_named_entries_keep_their_flag(self):
        """A named argument contributes the flag as well as its value."""
        package = _docker_package(
            {"type": "positional", "value": "run"},
            {"type": "named", "name": "-e", "value": "GITHUB_TOKEN"},
            {"type": "named", "name": "--mount", "value": "type=bind,src=/w,dst=/w"},
        )
        self.assertEqual(
            VSCodeClientAdapter._docker_run_args(package),
            [
                "run",
                "-i",
                "--rm",
                "-e",
                "GITHUB_TOKEN",
                "--mount",
                "type=bind,src=/w,dst=/w",
                IMAGE,
            ],
        )

    def test_required_flags_are_normalized_in(self):
        """A template omitting -i/--rm still yields an interactive container."""
        package = _docker_package(
            {"value": "run", "type": "positional"},
            {"value": "-v", "type": "positional"},
            {"value": "/w:/w", "type": "positional"},
        )
        self.assertEqual(
            VSCodeClientAdapter._docker_run_args(package),
            ["run", "-i", "--rm", "-v", "/w:/w", IMAGE],
        )

    def test_image_already_in_runtime_args_is_not_duplicated(self):
        package = _docker_package(
            {"value": "run", "type": "positional"},
            {"value": "-i", "type": "positional"},
            {"value": "--rm", "type": "positional"},
            {"value": IMAGE, "type": "positional"},
        )
        self.assertEqual(VSCodeClientAdapter._docker_run_args(package).count(IMAGE), 1)

    def test_workspace_folder_uses_the_vscode_native_token(self):
        package = _docker_package(
            {"value": "run", "type": "positional"},
            {
                "value": "{workspaceFolder}:/workspace",
                "type": "positional",
                "variables": {"workspaceFolder": {"description": "ws"}},
            },
        )
        self.assertIn(
            "${workspaceFolder}:/workspace", VSCodeClientAdapter._docker_run_args(package)
        )

    def test_optional_entry_with_a_collected_value_is_kept(self):
        """An optional mount whose variable the user filled in must survive --
        discarding user-supplied configuration is the #2377 failure mode."""
        package = _docker_package(
            {"value": "run", "type": "positional"},
            {"value": "-v", "type": "positional"},
            {
                "value": "{cacheDir}:/cache",
                "type": "positional",
                "is_required": False,
                "variables": {"cacheDir": {"description": "cache"}},
            },
        )
        self.assertEqual(
            VSCodeClientAdapter._docker_run_args(package, {"cacheDir": "/home/dev/.cache"}),
            ["run", "-i", "--rm", "-v", "/home/dev/.cache:/cache", IMAGE],
        )

    def test_optional_entry_with_an_unresolved_variable_is_dropped_alone(self):
        """A registry-declared optional mount must not take the whole command
        down with it -- only the unresolvable entry is skipped."""
        package = _docker_package(
            {"value": "run", "type": "positional"},
            {"value": "-w", "type": "positional"},
            {"value": "{workdir}", "type": "positional", "variables": _WORKDIR_VAR},
            {
                "value": "{cacheDir}:/cache",
                "type": "positional",
                "is_required": False,
                "variables": {"cacheDir": {"description": "cache"}},
            },
        )
        self.assertEqual(
            VSCodeClientAdapter._docker_run_args(package, {"workdir": WORKDIR}),
            ["run", "-i", "--rm", "-w", WORKDIR, IMAGE],
        )

    def test_optional_named_entry_drops_its_flag_with_unresolved_value(self):
        package = _docker_package(
            {"value": "run", "type": "positional"},
            {
                "name": "--mount",
                "value": "type=bind,src={cacheDir},dst=/cache",
                "type": "named",
                "is_required": False,
                "variables": {"cacheDir": {"description": "cache"}},
            },
        )
        self.assertEqual(
            VSCodeClientAdapter._docker_run_args(package),
            ["run", "-i", "--rm", IMAGE],
        )

    def test_optional_entry_is_skipped_in_either_spelling(self):
        """Argument-level keys are not camelCase-normalized, so accept both."""
        for key in ("is_required", "isRequired"):
            package = _docker_package(
                {"value": "run", "type": "positional"},
                {"value": "--verbose", "type": "positional", key: False},
            )
            self.assertEqual(
                VSCodeClientAdapter._docker_run_args(package),
                ["run", "-i", "--rm", IMAGE],
                f"{key}: False must not become a CLI argument",
            )

    def test_legacy_value_hint_entries_still_supported(self):
        package = _docker_package(
            {"value_hint": "run"},
            {"value_hint": "-i"},
            {"value_hint": "--rm"},
            {"value_hint": IMAGE},
        )
        self.assertEqual(VSCodeClientAdapter._docker_run_args(package), FALLBACK_ARGS)

    def test_package_arguments_follow_the_image_as_container_argv(self):
        """docker run [OPTIONS] IMAGE [ARG...] -- container args come last."""
        package = _docker_package({"value": "run", "type": "positional"})
        package["package_arguments"] = [
            {"value": "--transport", "type": "named"},
            {"value": "stdio", "type": "positional"},
        ]
        self.assertEqual(
            VSCodeClientAdapter._docker_run_args(package),
            ["run", "-i", "--rm", IMAGE, "--transport", "stdio"],
        )

    def test_unresolvable_package_argument_also_declines(self):
        package = _docker_package({"value": "run", "type": "positional"})
        package["package_arguments"] = [
            {
                "value": "{configPath}",
                "type": "positional",
                "variables": {"configPath": {"description": "cfg"}},
            }
        ]
        self.assertIsNone(VSCodeClientAdapter._docker_run_args(package))

    def test_required_runtime_argument_cannot_be_replaced_by_legacy_package_argv(self):
        package = _docker_package(
            {"value": "run", "type": "positional"},
            {
                "value": "--mount={requiredPath}",
                "type": "positional",
                "variables": {"requiredPath": {"description": "required"}},
            },
        )
        package["package_arguments"] = [
            {"value": "run", "type": "positional"},
            {"value": IMAGE, "type": "positional"},
        ]
        self.assertIsNone(VSCodeClientAdapter._docker_run_args(package))

    # -- declining cases: the caller keeps its previous launcher --------------

    def test_declines_when_the_run_verb_is_absent(self):
        package = _docker_package(
            {"value": "-v", "type": "positional"},
            {"value": "/w:/w", "type": "positional"},
        )
        self.assertIsNone(VSCodeClientAdapter._docker_run_args(package))

    def test_declines_when_a_variable_has_no_collected_value(self):
        """Writing a literal ${workdir} into a mount path would be worse."""
        self.assertIsNone(VSCodeClientAdapter._docker_run_args(V01_VALUE_PACKAGE))

    def test_declines_when_a_variable_resolves_to_an_empty_string(self):
        """Non-interactive installs surface empty values; ':' is not a mount."""
        self.assertIsNone(VSCodeClientAdapter._docker_run_args(V01_VALUE_PACKAGE, {"workdir": ""}))

    def test_declines_for_a_package_without_runtime_arguments(self):
        self.assertIsNone(VSCodeClientAdapter._docker_run_args({"name": IMAGE}))

    # -- malformed registry payloads must not abort the install --------------

    def test_non_dict_variables_do_not_raise(self):
        package = _docker_package(
            {"value": "run", "type": "positional"},
            {"value": "-v", "variables": None, "type": "positional"},
        )
        self.assertIsNone(VSCodeClientAdapter._docker_run_args(package))

    def test_non_string_values_are_coerced_rather_than_dropped(self):
        package = _docker_package(
            {"value": "run", "type": "positional"},
            {"value": "-p", "type": "positional"},
            {"value": 8080, "type": "positional"},
        )
        self.assertEqual(
            VSCodeClientAdapter._docker_run_args(package),
            ["run", "-i", "--rm", "-p", "8080", IMAGE],
        )

    def test_non_dict_entries_are_ignored(self):
        package = _docker_package({"value": "run", "type": "positional"}, "not-a-dict")
        self.assertEqual(VSCodeClientAdapter._docker_run_args(package), FALLBACK_ARGS)


# ---------------------------------------------------------------------------
# The shared extractor must stay untouched
# ---------------------------------------------------------------------------


class TestSharedExtractorUnaffected(unittest.TestCase):
    """Other registry branches read an empty extraction as "use the name"."""

    def test_extractor_still_ignores_value_shaped_runtime_args(self):
        self.assertEqual(
            VSCodeClientAdapter._extract_package_args(V01_VALUE_PACKAGE, runtime_vars=RUNTIME_VARS),
            [],
        )

    def test_pypi_launcher_keeps_the_package_name(self):
        config = _config(
            {
                "name": "mcp-server-fetch",
                "registry_name": "pypi",
                "runtime_hint": "uvx",
                "runtime_arguments": [
                    {"value": "--python", "type": "named"},
                    {"value": "3.12", "type": "positional"},
                ],
            }
        )
        self.assertEqual(config.get("command"), "uvx")
        self.assertEqual(config.get("args"), ["mcp-server-fetch"])

    def test_generic_runtime_launcher_keeps_the_package_name(self):
        config = _config(
            {
                "name": "Azure.Mcp",
                "registry_name": "nuget",
                "runtime_hint": "dnx",
                "runtime_arguments": [{"value": "--yes"}],
            }
        )
        self.assertEqual(config.get("command"), "dnx")
        self.assertEqual(config.get("args"), ["Azure.Mcp"])

    def test_npm_launcher_keeps_the_package_name(self):
        config = _config(
            {
                "name": "@scope/mcp",
                "registry_name": "npm",
                "runtime_hint": "npx",
                "runtime_arguments": [{"value": "-y"}, {"value": "@scope/mcp"}],
            }
        )
        self.assertEqual(config.get("command"), "npx")
        self.assertEqual(config.get("args"), ["-y", "@scope/mcp"])


# ---------------------------------------------------------------------------
# Rendered .vscode/mcp.json entry
# ---------------------------------------------------------------------------


class TestRenderedDockerLauncher(unittest.TestCase):
    def test_launcher_carries_the_run_options_and_the_image(self):
        config = _config(V01_VALUE_PACKAGE, runtime_vars=RUNTIME_VARS)
        self.assertEqual(config.get("command"), "docker")
        self.assertEqual(config.get("args"), EXPECTED_ARGS)

    def test_launcher_is_no_longer_the_bare_fallback(self):
        config = _config(V01_VALUE_PACKAGE, runtime_vars=RUNTIME_VARS)
        self.assertNotEqual(config.get("args"), FALLBACK_ARGS)

    def test_unresolvable_package_keeps_the_previous_launcher(self):
        with patch("apm_cli.adapters.client.vscode._rich_warning") as warning:
            config = _config(V01_VALUE_PACKAGE)
        self.assertEqual(config.get("args"), FALLBACK_ARGS)
        warning.assert_called_once_with(
            "Could not resolve container run options for "
            f"'{IMAGE}'; using the default launcher. "
            "Set the required registry runtime variables and rerun 'apm install'."
        )

    def test_package_without_runtime_args_keeps_the_synthesized_launcher(self):
        with patch("apm_cli.adapters.client.vscode._rich_warning") as warning:
            config = _config({"name": IMAGE, "registry_name": "oci", "runtime_hint": "docker"})
        self.assertEqual(config.get("args"), FALLBACK_ARGS)
        warning.assert_not_called()

    def test_legacy_full_argv_in_package_arguments_stays_verbatim(self):
        """Some registry data authors the complete docker argv in
        package_arguments; without a run verb in runtime_arguments the builder
        declines and the pre-change verbatim path still applies."""
        config = _config(
            {
                "name": IMAGE,
                "registry_name": "docker",
                "runtime_hint": "docker",
                "package_arguments": [
                    {"value": "run", "type": "positional"},
                    {"value": "--read-only", "type": "positional"},
                    {"value": IMAGE, "type": "positional"},
                    {"value": "--rm", "type": "positional"},
                    {"value": "--transport", "type": "positional"},
                    {"value": "stdio", "type": "positional"},
                ],
            }
        )
        self.assertEqual(
            config.get("args"),
            [
                "run",
                "-i",
                "--rm",
                "--read-only",
                IMAGE,
                "--rm",
                "--transport",
                "stdio",
            ],
        )


if __name__ == "__main__":
    unittest.main()
