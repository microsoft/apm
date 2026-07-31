"""Regression tests for MCP Registry v0.1 ``oci`` container packages.

Issue #2376: a package whose ``registryType`` is the v0.1 spelling ``oci``
matched no launcher branch (the adapters key on ``docker``) and fell through
to the generic ``npx`` default, so the container image reference was handed
to npm as a package name -- a dependency-confusion exposure, not merely a
broken config.

The same registry exposes the image only as the package ``identifier``, never
inside ``runtimeArguments``, so the resolved args must also gain the image or
the generated ``docker run`` names no image at all.
"""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import pytest

from apm_cli.adapters.client.base import MCPClientAdapter
from apm_cli.adapters.client.codex import CodexClientAdapter
from apm_cli.adapters.client.copilot import CopilotClientAdapter
from apm_cli.adapters.client.gemini import GeminiClientAdapter
from apm_cli.adapters.client.vscode import VSCodeClientAdapter

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixtures -- the reporting registry's shape, after the registry client maps
# v0.1 camelCase onto the legacy snake_case keys adapters consume.
# ---------------------------------------------------------------------------

OCI_IMAGE = "myregistry.example.com/team/mcp-server:1.4.0"

_WORKDIR_VAR = {
    "workdir": {
        "description": "Working directory to mount into the container.",
        "isRequired": True,
        "format": "filepath",
    }
}

# Note: no runtime argument carries the image -- it exists only as `name`
# (v0.1 `identifier`). This is what makes the image-append path load-bearing.
OCI_PACKAGE = {
    "name": OCI_IMAGE,
    "registry_name": "oci",
    "runtime_hint": "docker",
    "transport": {"type": "stdio"},
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

SERVER_INFO = {"id": "team/mcp-server", "name": "team/mcp-server", "packages": [OCI_PACKAGE]}

RUNTIME_VARS = {"workdir": "/home/dev/project"}

EXPECTED_ARGS = [
    "run",
    "--rm",
    "-i",
    "-v",
    "/home/dev/project:/home/dev/project",
    "-w",
    "/home/dev/project",
    OCI_IMAGE,
]


def _make_copilot() -> CopilotClientAdapter:
    with (
        patch("apm_cli.adapters.client.copilot.SimpleRegistryClient"),
        patch("apm_cli.adapters.client.copilot.RegistryIntegration"),
    ):
        return CopilotClientAdapter()


def _make_codex() -> CodexClientAdapter:
    # Pin project_root to a scratch dir: the adapter resolves config paths
    # relative to it, and the default (cwd) would litter the repo.
    with (
        patch("apm_cli.adapters.client.codex.SimpleRegistryClient"),
        patch("apm_cli.adapters.client.codex.RegistryIntegration"),
    ):
        return CodexClientAdapter(project_root=tempfile.mkdtemp())


def _make_gemini() -> GeminiClientAdapter:
    # Gemini inherits Copilot's __init__; its own module exposes no registry
    # symbols to patch, so intercept them where they are resolved.
    with (
        patch("apm_cli.adapters.client.copilot.SimpleRegistryClient"),
        patch("apm_cli.adapters.client.copilot.RegistryIntegration"),
    ):
        return GeminiClientAdapter()


def _make_vscode() -> VSCodeClientAdapter:
    with (
        patch("apm_cli.adapters.client.vscode.SimpleRegistryClient"),
        patch("apm_cli.adapters.client.vscode.RegistryIntegration"),
    ):
        return VSCodeClientAdapter(project_root="/tmp/workspace")


# ---------------------------------------------------------------------------
# _infer_registry_name
# ---------------------------------------------------------------------------


class TestInferRegistryNameOci(unittest.TestCase):
    """The v0.1 ``oci`` registry type selects APM's container launcher."""

    def test_oci_maps_to_docker(self):
        self.assertEqual(
            MCPClientAdapter._infer_registry_name({"name": OCI_IMAGE, "registry_name": "oci"}),
            "docker",
        )

    def test_oci_mapping_is_case_and_space_insensitive(self):
        for spelling in ("OCI", " oci ", "Oci"):
            self.assertEqual(
                MCPClientAdapter._infer_registry_name(
                    {"name": OCI_IMAGE, "registry_name": spelling}
                ),
                "docker",
                f"registry type {spelling!r} must select the container launcher",
            )

    def test_other_explicit_registry_types_pass_through_untouched(self):
        for registry_type in ("npm", "pypi", "nuget", "mcpb", "homebrew", "docker"):
            self.assertEqual(
                MCPClientAdapter._infer_registry_name(
                    {"name": "pkg", "registry_name": registry_type}
                ),
                registry_type,
            )

    def test_uppercase_registry_types_reach_their_launcher_branch(self):
        """Dispatch compares lowercase; returning the raw value would send a
        sloppily-cased type to the generic npx default."""
        self.assertEqual(
            MCPClientAdapter._infer_registry_name({"name": "pkg", "registry_name": "NPM"}), "npm"
        )
        self.assertEqual(
            MCPClientAdapter._infer_registry_name({"name": "pkg", "registry_name": "Docker"}),
            "docker",
        )

    def test_non_string_registry_type_fails_closed_before_launcher_dispatch(self):
        """Malformed explicit types must never reach the generic npx fallback."""
        with self.assertRaisesRegex(ValueError, "registry_name must be a string"):
            MCPClientAdapter._infer_registry_name(
                {"name": "p", "registry_name": 5, "runtime_hint": "docker"}
            )

    def test_oci_package_wins_container_selection_priority(self):
        """_select_best_package ranks containers above pypi; oci must qualify."""
        pypi_package = {"name": "some-server", "registry_name": "pypi"}
        selected = MCPClientAdapter._select_best_package([pypi_package, OCI_PACKAGE])
        self.assertIs(selected, OCI_PACKAGE)


# ---------------------------------------------------------------------------
# _ensure_docker_image_arg
# ---------------------------------------------------------------------------


class TestEnsureDockerImageArg(unittest.TestCase):
    """The image operand is present exactly once, after the run options."""

    def test_image_appended_when_runtime_args_omit_it(self):
        args = MCPClientAdapter._ensure_docker_image_arg(["run", "--rm", "-i"], OCI_IMAGE)
        self.assertEqual(args, ["run", "--rm", "-i", OCI_IMAGE])

    def test_image_not_duplicated_when_already_present(self):
        base = ["run", "-i", "--rm", OCI_IMAGE]
        self.assertEqual(MCPClientAdapter._ensure_docker_image_arg(base, OCI_IMAGE), base)

    def test_pinned_runtime_arg_matches_unpinned_identifier(self):
        base = ["run", "ghcr.io/example/img:2.0.0"]
        args = MCPClientAdapter._ensure_docker_image_arg(base, "ghcr.io/example/img")
        self.assertEqual(args, base)

    def test_unpinned_runtime_arg_matches_pinned_identifier(self):
        base = ["run", "ghcr.io/example/img"]
        args = MCPClientAdapter._ensure_docker_image_arg(base, "ghcr.io/example/img:2.0.0")
        self.assertEqual(args, base)

    def test_digest_pinned_identifier_matches_plain_runtime_arg(self):
        """A duplicate operand would be parsed by Docker as the container command."""
        base = ["run", "--rm", "mcp/everything"]
        args = MCPClientAdapter._ensure_docker_image_arg(base, "mcp/everything@sha256:" + "d" * 64)
        self.assertEqual(args, base)

    def test_differently_tagged_runtime_arg_is_still_the_same_image(self):
        base = ["run", "ghcr.io/example/img:latest"]
        args = MCPClientAdapter._ensure_docker_image_arg(base, "ghcr.io/example/img:1.2.3")
        self.assertEqual(args, base)

    def test_registry_port_is_not_mistaken_for_a_tag(self):
        """`host:port/path` has no tag; the image must still be appended."""
        args = MCPClientAdapter._ensure_docker_image_arg(
            ["run", "--rm"], "myreg.example.com:5000/team/img"
        )
        self.assertEqual(args, ["run", "--rm", "myreg.example.com:5000/team/img"])

    def test_an_option_value_mid_list_does_not_stand_in_for_the_image(self):
        """`--name <image-basename>` is a common template.

        Only the trailing entry can be the image -- everything before it is a
        run option or an option's value -- so matching anywhere in the list let
        `--name mcp-server` suppress the append and render the image-less
        `docker run` this guard exists to prevent.
        """
        args = MCPClientAdapter._ensure_docker_image_arg(
            ["run", "--rm", "--name", "mcp-server", "-v", "/w:/w"], "mcp-server:1.0"
        )
        self.assertEqual(args[-1], "mcp-server:1.0")

    def test_docker_hub_short_form_matches_the_qualified_identifier(self):
        """Registries qualify the identifier while their run args stay implicit."""
        base = ["run", "-i", "--rm", "mcp/github"]
        args = MCPClientAdapter._ensure_docker_image_arg(base, "docker.io/mcp/github")
        self.assertEqual(args, base)

    def test_official_image_library_prefix_is_folded(self):
        base = ["run", "redis"]
        args = MCPClientAdapter._ensure_docker_image_arg(base, "docker.io/library/redis:7")
        self.assertEqual(args, base)

    def test_non_docker_hub_library_path_is_not_folded(self):
        """`library/` is only implicit on Docker Hub; elsewhere it is a real path."""
        args = MCPClientAdapter._ensure_docker_image_arg(
            ["run", "redis"], "ghcr.io/library/redis:7"
        )
        self.assertEqual(args, ["run", "redis", "ghcr.io/library/redis:7"])

    def test_distinct_image_repositories_are_not_conflated(self):
        args = MCPClientAdapter._ensure_docker_image_arg(
            ["run", "ghcr.io/other/img:1.0"], "ghcr.io/example/img:1.0"
        )
        self.assertEqual(args, ["run", "ghcr.io/other/img:1.0", "ghcr.io/example/img:1.0"])

    def test_bind_mount_value_is_not_mistaken_for_the_image(self):
        args = MCPClientAdapter._ensure_docker_image_arg(
            ["run", "-v", "/home/dev/project:/home/dev/project"], OCI_IMAGE
        )
        self.assertEqual(args[-1], OCI_IMAGE)

    def test_non_string_arguments_do_not_raise(self):
        """Registry payloads are untrusted; a numeric arg must not abort install."""
        args = MCPClientAdapter._ensure_docker_image_arg(["run", 8080], OCI_IMAGE)
        self.assertEqual(args, ["run", 8080, OCI_IMAGE])

    def test_empty_image_leaves_args_untouched(self):
        self.assertEqual(
            MCPClientAdapter._ensure_docker_image_arg(["run", "--rm"], ""), ["run", "--rm"]
        )

    def test_inputs_are_not_mutated(self):
        runtime_args = ["run", "--rm"]
        MCPClientAdapter._ensure_docker_image_arg(runtime_args, OCI_IMAGE)
        self.assertEqual(runtime_args, ["run", "--rm"])


# ---------------------------------------------------------------------------
# End-to-end launcher generation per target
# ---------------------------------------------------------------------------


class TestOciLauncherAcrossTargets(unittest.TestCase):
    """Every target renders a runnable docker launcher, never an npx one."""

    def test_copilot_generates_docker_launcher_with_image(self):
        config = _make_copilot()._format_server_config(SERVER_INFO, runtime_vars=RUNTIME_VARS)
        self.assertEqual(config.get("command"), "docker")
        self.assertEqual(config.get("args"), EXPECTED_ARGS)

    def test_copilot_never_routes_a_container_package_through_npm(self):
        """The #2376 dependency-confusion trap: npx would resolve `run` on npm."""
        config = _make_copilot()._format_server_config(SERVER_INFO, runtime_vars=RUNTIME_VARS)
        args = config.get("args", [])
        self.assertNotEqual(config.get("command"), "npx")
        self.assertNotIn("-y", args)
        self.assertEqual(args[-1], OCI_IMAGE, "image must be the final docker run operand")

    def test_gemini_generates_docker_launcher_with_image(self):
        config = _make_gemini()._format_server_config(SERVER_INFO, runtime_vars=RUNTIME_VARS)
        self.assertEqual(config.get("command"), "docker")
        self.assertEqual(config.get("args"), EXPECTED_ARGS)

    def test_codex_generates_docker_launcher_with_image(self):
        config = _make_codex()._format_server_config(SERVER_INFO, runtime_vars=RUNTIME_VARS)
        self.assertEqual(config.get("command"), "docker")
        self.assertEqual(config.get("args"), EXPECTED_ARGS)

    def test_existing_docker_package_gains_no_duplicate_image(self):
        """Regression guard for packages that already worked before #2376.

        The identifier may be digest-pinned while the runtime argument names
        the bare repository; appending a second operand would be read by
        Docker as the container command and break a working launcher.
        """
        package = {
            "name": "mcp/everything@sha256:" + "d" * 64,
            "registry_name": "docker",
            "runtime_hint": "docker",
            "runtime_arguments": [
                {"value": "run", "type": "positional"},
                {"value": "--rm", "type": "positional"},
                {"value": "-i", "type": "positional"},
                {"value": "mcp/everything", "type": "positional"},
            ],
        }
        config = _make_copilot()._format_server_config(
            {"id": "everything", "name": "everything", "packages": [package]}
        )
        self.assertEqual(config.get("args"), ["run", "--rm", "-i", "mcp/everything"])

    def test_codex_keeps_env_flags_ahead_of_the_appended_image(self):
        """``-e`` flags are ``docker run`` options, so they precede the image.

        ``_ensure_docker_env_flags`` inserts them before the trailing operand,
        which only lands correctly once that operand is the image.
        """
        package = dict(OCI_PACKAGE)
        package["environment_variables"] = [{"name": "API_KEY", "is_required": True}]
        config = _make_codex()._format_server_config(
            {"id": "s", "name": "s", "packages": [package]},
            env_overrides={"API_KEY": "secret"},
            runtime_vars=RUNTIME_VARS,
        )
        args = config.get("args", [])
        self.assertIn("-e", args)
        self.assertLess(args.index("-e"), args.index(OCI_IMAGE), "env flags must precede the image")

    def test_codex_container_argv_follows_the_image(self):
        """package_arguments are the container's argv: after the image, with
        the -e run option still ahead of it -- the composition that previously
        pushed -e into the argv left the env var unset in the container."""
        package = dict(OCI_PACKAGE)
        package["environment_variables"] = [{"name": "API_KEY", "is_required": True}]
        package["package_arguments"] = [
            {"value": "--transport", "type": "named"},
            {"value": "stdio", "type": "positional"},
        ]
        config = _make_codex()._format_server_config(
            {"id": "s", "name": "s", "packages": [package]},
            env_overrides={"API_KEY": "secret"},
            runtime_vars=RUNTIME_VARS,
        )
        args = config.get("args", [])
        image_at = args.index(OCI_IMAGE)
        self.assertLess(args.index("-e"), image_at, "env flags must precede the image")
        self.assertGreater(args.index("--transport"), image_at, "container argv must follow it")
        self.assertEqual(args[-2:], ["--transport", "stdio"])

    def test_package_arguments_follow_image_across_adapters(self):
        package = dict(OCI_PACKAGE)
        package["package_arguments"] = [
            {"value": "--transport", "type": "positional"},
            {"value": "stdio", "type": "positional"},
        ]
        for name, make_adapter in (
            ("copilot", _make_copilot),
            ("codex", _make_codex),
            ("gemini", _make_gemini),
            ("vscode", _make_vscode),
        ):
            with self.subTest(adapter=name):
                rendered = make_adapter()._format_server_config(
                    {"id": "s", "name": "s", "packages": [package]},
                    runtime_vars=RUNTIME_VARS,
                )
                config = rendered[0] if isinstance(rendered, tuple) else rendered
                args = config["args"]
                image_at = args.index(OCI_IMAGE)
                self.assertEqual(args.count(OCI_IMAGE), 1)
                self.assertEqual(args[image_at + 1 :], ["--transport", "stdio"])

    def test_vscode_still_renders_a_docker_launcher_for_an_oci_package(self):
        """Guard: VS Code already keyed off runtime_hint, and must stay that way.

        VS Code's own ``value``-shaped argument handling is tracked separately
        (#2377); this only pins that the registry-type change does not divert
        an ``oci`` package away from its container branch.
        """
        package = {
            "name": "ghcr.io/example/playwright-mcp:1.2.3",
            "registry_name": "oci",
            "runtime_hint": "docker",
            "runtime_arguments": [
                {"value_hint": "run"},
                {"value_hint": "-i"},
                {"value_hint": "--rm"},
                {"value_hint": "ghcr.io/example/playwright-mcp:1.2.3"},
            ],
        }
        config, _inputs = _make_vscode()._format_server_config(
            {"id": "playwright-mcp", "name": "playwright-mcp", "packages": [package]}
        )
        self.assertEqual(config.get("command"), "docker")
        self.assertEqual(
            config.get("args"),
            ["run", "-i", "--rm", "ghcr.io/example/playwright-mcp:1.2.3"],
        )


if __name__ == "__main__":
    unittest.main()
