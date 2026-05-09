"""Regression tests for issue #1207 -- local-bundle install routing.

Covers:

* D1: ``pack.target`` no longer hardcoded to ``"copilot"``; new bundles
  emit ``"all"`` (target-agnostic) when no ``--target`` is provided and
  no project target can be detected, while ``check_target_mismatch``
  treats ``"all"`` as universal coverage.
* D2.a: ``plugin.json`` (case-insensitive) is never deployed to consumer
  projects, regardless of which casing it appears under in the bundle
  manifest or filesystem.
* D2.b: ``instructions/*.md`` for compile-only targets (opencode, codex,
  gemini -- profiles without an ``instructions`` primitive) are staged
  under ``apm_modules/<slug>/.apm/instructions/`` so ``apm compile``
  picks them up, instead of being copied verbatim into ``<root>/
  instructions/`` where they would be invisible to those clients.
* D3: the local-bundle install path no longer prints
  ``"Install interrupted"`` on the success path.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
import types
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.bundle.local_bundle import check_target_mismatch
from apm_cli.install.services import integrate_local_bundle
from apm_cli.integration.targets import KNOWN_TARGETS

# ---------------------------------------------------------------------------
# Minimal fixtures -- intentionally lighter than the air-gap E2E suite.
# ---------------------------------------------------------------------------


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _build_bundle(
    base: Path,
    *,
    plugin_id: str = "ai",
    pack_target: str | None = "all",
    files: dict[str, str] | None = None,
    plugin_json_name: str = "plugin.json",
) -> Path:
    bundle = base / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / plugin_json_name).write_text(
        json.dumps({"id": plugin_id, "name": plugin_id}), encoding="utf-8"
    )
    files = files or {}
    bundle_files: dict[str, str] = {}
    for rel, content in files.items():
        p = bundle / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8"))
        bundle_files[rel] = _sha256(content)
    # plugin.json is bundle metadata -- include in manifest so the install
    # primary loop exercises the case-insensitive skip branch.
    pj_path = bundle / plugin_json_name
    bundle_files[plugin_json_name] = hashlib.sha256(pj_path.read_bytes()).hexdigest()

    pack_meta: dict = {"format": "plugin", "bundle_files": bundle_files}
    if pack_target is not None:
        pack_meta["target"] = pack_target
    lock_data = {
        "pack": pack_meta,
        "dependencies": [
            {
                "repo_url": f"owner/{plugin_id}",
                "resolved_commit": "abc123",
                "deployed_files": list(files.keys()),
                "deployed_file_hashes": {k: bundle_files[k] for k in files},
            }
        ],
    }
    (bundle / "apm.lock.yaml").write_text(
        yaml.dump(lock_data, default_flow_style=False), encoding="utf-8"
    )
    return bundle


def _make_tarball(base: Path, bundle_dir: Path) -> Path:
    archive = base / f"{bundle_dir.name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(bundle_dir, arcname=bundle_dir.name)
    return archive


def _bundle_info(bundle_dir: Path):
    """Synthetic ``LocalBundleInfo`` mirroring what the CLI seam produces."""
    lock = yaml.safe_load((bundle_dir / "apm.lock.yaml").read_text(encoding="utf-8"))
    return types.SimpleNamespace(
        source_dir=bundle_dir,
        lockfile=lock,
        package_id="ai",
        pack_targets=[lock.get("pack", {}).get("target", "all")],
        temp_dir=None,
    )


# ---------------------------------------------------------------------------
# D1: pack.target "all" -> no mismatch warning
# ---------------------------------------------------------------------------


class TestPackTargetAllIsUniversal:
    def test_check_target_mismatch_returns_none_for_all(self) -> None:
        assert check_target_mismatch(["all"], ["opencode"]) is None
        assert check_target_mismatch(["all"], ["copilot", "claude"]) is None
        assert check_target_mismatch(["all"], []) is None

    def test_check_target_mismatch_still_warns_on_real_mismatch(self) -> None:
        warning = check_target_mismatch(["copilot"], ["opencode"])
        assert warning is not None
        assert "copilot" in warning
        assert "opencode" in warning


# ---------------------------------------------------------------------------
# D2.a: plugin.json never deployed (case-insensitive)
# ---------------------------------------------------------------------------


class TestPluginJsonNeverDeployed:
    @pytest.mark.parametrize("name", ["plugin.json", "Plugin.json", "PLUGIN.JSON"])
    def test_plugin_json_skipped_regardless_of_case(self, tmp_path: Path, name: str) -> None:
        bundle = _build_bundle(
            tmp_path,
            files={"agents/coder.md": "# Coder\n"},
            plugin_json_name=name,
        )
        project = tmp_path / "project"
        project.mkdir()
        bi = _bundle_info(bundle)
        target = KNOWN_TARGETS["copilot"]

        result = integrate_local_bundle(
            bi,
            project,
            targets=[target],
            force=False,
            dry_run=False,
            diagnostics=None,
            logger=None,
            scope=None,
            alias=None,
        )

        # plugin.json must never appear under the target's deploy root.
        for f in result["deployed_files"]:
            assert "plugin.json" not in Path(f).name.lower(), f"plugin.json deployed to {f}"
        # And the file must not exist on disk under any casing.
        for child in (project / target.root_dir).rglob("*"):
            assert child.name.lower() != "plugin.json", f"plugin.json materialised at {child}"


# ---------------------------------------------------------------------------
# D2: .mcp.json never deployed as a flat file
# ---------------------------------------------------------------------------


class TestMcpJsonNeverDeployed:
    @pytest.mark.parametrize("name", [".mcp.json", ".MCP.json", ".Mcp.Json"])
    def test_mcp_json_skipped_regardless_of_case(self, tmp_path: Path, name: str) -> None:
        # ``.mcp.json`` is wired via ``MCPIntegrator`` after the deploy
        # loop; it must never land as a flat file under the consumer's
        # target root.  Case-folding filesystems (HFS+, NTFS) make a
        # case-sensitive skip exploitable.
        bundle = _build_bundle(
            tmp_path,
            files={"agents/coder.md": "# Coder\n", name: '{"mcpServers": {}}'},
        )
        project = tmp_path / "project"
        project.mkdir()
        bi = _bundle_info(bundle)
        target = KNOWN_TARGETS["copilot"]

        result = integrate_local_bundle(
            bi,
            project,
            targets=[target],
            force=False,
            dry_run=False,
            diagnostics=None,
            logger=None,
            scope=None,
            alias=None,
        )

        for f in result["deployed_files"]:
            assert Path(f).name.lower() != ".mcp.json", f".mcp.json deployed as flat file to {f}"
        for child in (project / target.root_dir).rglob("*"):
            assert child.name.lower() != ".mcp.json", f".mcp.json materialised at {child}"


# ---------------------------------------------------------------------------
# D2.b: instructions staged for compile-only targets
# ---------------------------------------------------------------------------


_COMPILE_ONLY_TARGETS = ["opencode", "codex", "gemini"]
_NATIVE_INSTRUCTION_TARGETS = ["copilot", "claude", "cursor"]


class TestInstructionStaging:
    @pytest.mark.parametrize("target_name", _COMPILE_ONLY_TARGETS)
    def test_instructions_staged_under_apm_modules_for_compile_only(
        self, tmp_path: Path, target_name: str
    ) -> None:
        bundle = _build_bundle(
            tmp_path,
            files={"instructions/style.md": "# Style guide\n"},
        )
        project = tmp_path / "project"
        project.mkdir()
        bi = _bundle_info(bundle)
        target = KNOWN_TARGETS[target_name]
        # Sanity check: profile must lack ``instructions`` for this branch
        # to fire.
        assert "instructions" not in (target.primitives or {})

        result = integrate_local_bundle(
            bi,
            project,
            targets=[target],
            force=False,
            dry_run=False,
            diagnostics=None,
            logger=None,
            scope=None,
            alias=None,
        )

        deployed = result["deployed_files"]
        # Exactly one file should land under apm_modules/ai/.apm/instructions/.
        staged = [
            f
            for f in deployed
            if f.replace("\\", "/") == "apm_modules/ai/.apm/instructions/style.md"
        ]
        assert staged, f"Instruction not staged; deployed={deployed}"
        assert (project / "apm_modules" / "ai" / ".apm" / "instructions" / "style.md").is_file()
        # And it must NOT have been copied verbatim under the target's root.
        assert not (project / target.root_dir / "instructions" / "style.md").exists()

    @pytest.mark.parametrize("target_name", _NATIVE_INSTRUCTION_TARGETS)
    def test_instructions_NOT_staged_for_native_instruction_targets(
        self, tmp_path: Path, target_name: str
    ) -> None:
        """When the target HAS an ``instructions`` primitive, the bundle's
        ``instructions/*.md`` keeps the verbatim copy path so the existing
        downstream behaviour (or a future primitive-aware deploy) still
        runs.  This test pins that the staging branch does NOT fire for
        targets with native instructions support, preventing accidental
        regression of compile flow for clients that don't need it.
        """
        bundle = _build_bundle(
            tmp_path,
            files={"instructions/style.md": "# Style guide\n"},
        )
        project = tmp_path / "project"
        project.mkdir()
        bi = _bundle_info(bundle)
        target = KNOWN_TARGETS[target_name]
        assert "instructions" in (target.primitives or {})

        result = integrate_local_bundle(
            bi,
            project,
            targets=[target],
            force=False,
            dry_run=False,
            diagnostics=None,
            logger=None,
            scope=None,
            alias=None,
        )

        deployed = result["deployed_files"]
        # Must not have been staged under apm_modules/.
        for f in deployed:
            assert "apm_modules/" not in f.replace("\\", "/"), (
                f"Instruction wrongly staged for {target_name}: {f}"
            )

    def test_unsafe_slug_skips_staging(self, tmp_path: Path) -> None:
        """A bundle whose ``package_id`` contains traversal segments must
        NOT escape ``apm_modules/``.  ``validate_path_segments`` on the
        slug guards the destination construction.
        """
        bundle = _build_bundle(
            tmp_path,
            files={"instructions/x.md": "# x\n"},
        )
        project = tmp_path / "project"
        project.mkdir()
        bi = _bundle_info(bundle)
        bi.package_id = "../escape"

        result = integrate_local_bundle(
            bi,
            project,
            targets=[KNOWN_TARGETS["opencode"]],
            force=False,
            dry_run=False,
            diagnostics=None,
            logger=None,
            scope=None,
            alias=None,
        )
        # Skipped, not deployed.
        assert result["skipped"] >= 1
        # And nothing materialised outside the project root.
        for f in result["deployed_files"]:
            assert ".." not in f

    @pytest.mark.parametrize(
        "bad_slug,reason",
        [
            ("foo/bar", "forward slash creates nested dirs"),
            ("a\x00b", "null byte must be rejected before path resolution"),
            (".hidden", "leading dot is forbidden"),
            ("trailing.", "trailing dot is forbidden"),
            ("a@b", "@ is outside the [A-Za-z0-9._-] whitelist"),
            ("a b", "whitespace is outside the whitelist"),
        ],
    )
    def test_adversarial_slug_skips_staging(
        self, tmp_path: Path, bad_slug: str, reason: str
    ) -> None:
        """Slugs that fail the documented [A-Za-z0-9._-] whitelist must
        be skipped cleanly, not crash the install with a bare ValueError
        from path resolution (sec-2) or smuggle a forward slash past the
        slug guard (sec-1).
        """
        bundle = _build_bundle(
            tmp_path,
            files={"instructions/x.md": "# x\n"},
        )
        project = tmp_path / "project"
        project.mkdir()
        bi = _bundle_info(bundle)
        bi.package_id = bad_slug

        result = integrate_local_bundle(
            bi,
            project,
            targets=[KNOWN_TARGETS["opencode"]],
            force=False,
            dry_run=False,
            diagnostics=None,
            logger=None,
            scope=None,
            alias=None,
        )
        assert result["skipped"] >= 1, f"slug {bad_slug!r}: {reason}"
        # Nothing under apm_modules/ for a rejected slug.
        for f in result["deployed_files"]:
            assert "apm_modules/" not in f.replace("\\", "/")


# ---------------------------------------------------------------------------
# D3: no false "Install interrupted"
# ---------------------------------------------------------------------------


class TestSummaryRenderedFlag:
    def test_local_bundle_install_does_not_print_install_interrupted(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The local-bundle install path must mark ``summary_rendered = True``
        before returning so the outer ``finally`` block does not emit a
        misleading ``"Install interrupted"`` line on success.
        """
        from apm_cli.cli import cli

        bundle = _build_bundle(
            tmp_path,
            files={"agents/coder.md": "# Coder\n"},
        )
        archive = _make_tarball(tmp_path, bundle)

        project = tmp_path / "project"
        project.mkdir()
        # Minimal apm.yml, .github/ to anchor copilot detection.
        (project / "apm.yml").write_text(
            yaml.dump({"name": "p", "version": "1.0.0"}), encoding="utf-8"
        )
        (project / ".github").mkdir()
        (project / ".github" / "copilot-instructions.md").write_text("# proj\n", encoding="utf-8")

        monkeypatch.chdir(project)
        result = CliRunner().invoke(cli, ["install", str(archive)], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert "Install interrupted" not in result.output


# ---------------------------------------------------------------------------
# D2.c: bundle .mcp.json wired through MCPIntegrator (per-target dispatch)
# ---------------------------------------------------------------------------


class TestBundleMcpWiring:
    """``.mcp.json`` (Anthropic plugin format) is parsed and routed
    through ``MCPIntegrator.install`` so each resolved target's native
    MCP config gets the servers in its own format/location.  The file
    itself is never deployed verbatim.
    """

    def test_parse_bundle_mcp_servers_anthropic_format(self, tmp_path: Path) -> None:
        from apm_cli.install.local_bundle_handler import _parse_bundle_mcp_servers

        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "weather": {
                            "type": "stdio",
                            "command": "npx",
                            "args": ["-y", "@example/weather-mcp"],
                            "env": {"API_KEY": "${WEATHER_KEY}"},
                        },
                        "search": {
                            "type": "http",
                            "url": "https://search.example.com/mcp",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        deps = _parse_bundle_mcp_servers(bundle)
        names = {d.name for d in deps}
        assert names == {"weather", "search"}
        # ``type`` aliases ``transport`` (handled by MCPDependency.from_dict).
        weather = next(d for d in deps if d.name == "weather")
        assert weather.transport == "stdio"
        assert weather.command == "npx"
        # Self-defined (not pulled from registry).
        assert weather.registry is False

    def test_parse_bundle_mcp_servers_case_insensitive(self, tmp_path: Path) -> None:
        from apm_cli.install.local_bundle_handler import _parse_bundle_mcp_servers

        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / ".MCP.json").write_text(
            json.dumps({"mcpServers": {"x": {"type": "stdio", "command": "echo"}}}),
            encoding="utf-8",
        )
        deps = _parse_bundle_mcp_servers(bundle)
        assert {d.name for d in deps} == {"x"}

    def test_parse_bundle_mcp_servers_missing_or_malformed_returns_empty(
        self, tmp_path: Path
    ) -> None:
        from apm_cli.install.local_bundle_handler import _parse_bundle_mcp_servers

        bundle = tmp_path / "bundle"
        bundle.mkdir()
        # Missing file.
        assert _parse_bundle_mcp_servers(bundle) == []
        # Malformed JSON.
        (bundle / ".mcp.json").write_text("{not json", encoding="utf-8")
        assert _parse_bundle_mcp_servers(bundle) == []
        # Valid JSON, no mcpServers key.
        (bundle / ".mcp.json").write_text(json.dumps({"unrelated": 1}), encoding="utf-8")
        assert _parse_bundle_mcp_servers(bundle) == []
        # Bad per-server entry is skipped, others survive.  ``noxform`` is
        # not a valid transport so strict validation raises and the entry
        # is dropped; ``good`` parses cleanly.
        (bundle / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "good": {"type": "stdio", "command": "ok"},
                        "bad": {"type": "noxform", "command": "x"},
                    }
                }
            ),
            encoding="utf-8",
        )
        deps = _parse_bundle_mcp_servers(bundle)
        assert {d.name for d in deps} == {"good"}

    def test_wire_bundle_mcp_servers_invokes_integrator_with_csv_targets(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """``_wire_bundle_mcp_servers`` calls ``MCPIntegrator.install`` once
        with a CSV of all resolved target names so per-target dispatch is
        delegated to the integrator's existing fan-out.
        """
        from apm_cli.install import local_bundle_handler

        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"a": {"type": "stdio", "command": "x"}}}),
            encoding="utf-8",
        )

        captured: dict = {}

        def fake_install(deps, **kwargs):
            captured["deps"] = deps
            captured["kwargs"] = kwargs
            return len(deps)

        monkeypatch.setattr(
            "apm_cli.integration.mcp_integrator.MCPIntegrator.install",
            staticmethod(fake_install),
        )

        targets = [KNOWN_TARGETS["copilot"], KNOWN_TARGETS["opencode"]]
        logger = types.SimpleNamespace(
            success=lambda *a, **k: None,
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
        )
        count = local_bundle_handler._wire_bundle_mcp_servers(
            bundle_dir=bundle,
            targets=targets,
            project_root=tmp_path / "project",
            user_scope=False,
            verbose=False,
            logger=logger,
        )
        assert count == 1
        assert captured["kwargs"]["explicit_target"] == "copilot,opencode"
        assert captured["kwargs"]["apm_config"]["target"] == "copilot,opencode"
        assert captured["kwargs"]["user_scope"] is False
        assert {d.name for d in captured["deps"]} == {"a"}

    def test_wire_bundle_mcp_servers_handles_missing_mcp_json(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from apm_cli.install import local_bundle_handler

        bundle = tmp_path / "bundle"
        bundle.mkdir()  # No .mcp.json present.

        called = {"n": 0}

        def fake_install(deps, **kwargs):
            called["n"] += 1
            return 0

        monkeypatch.setattr(
            "apm_cli.integration.mcp_integrator.MCPIntegrator.install",
            staticmethod(fake_install),
        )

        logger = types.SimpleNamespace(
            success=lambda *a, **k: None,
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
        )
        count = local_bundle_handler._wire_bundle_mcp_servers(
            bundle_dir=bundle,
            targets=[KNOWN_TARGETS["copilot"]],
            project_root=tmp_path / "project",
            user_scope=False,
            verbose=False,
            logger=logger,
        )
        assert count == 0
        # Integrator is not invoked when the bundle has no servers to wire.
        assert called["n"] == 0

    def test_wire_bundle_mcp_servers_isolates_integrator_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A failing ``MCPIntegrator.install`` must not propagate -- the
        rest of the install completes and the user is told to wire the
        servers manually via ``apm.yml``.
        """
        from apm_cli.install import local_bundle_handler

        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"a": {"type": "stdio", "command": "x"}}}),
            encoding="utf-8",
        )

        def boom(deps, **kwargs):
            raise RuntimeError("simulated wiring failure")

        monkeypatch.setattr(
            "apm_cli.integration.mcp_integrator.MCPIntegrator.install",
            staticmethod(boom),
        )

        warnings: list[str] = []
        logger = types.SimpleNamespace(
            success=lambda *a, **k: None,
            info=lambda *a, **k: None,
            warning=lambda msg, *a, **k: warnings.append(msg),
        )

        count = local_bundle_handler._wire_bundle_mcp_servers(
            bundle_dir=bundle,
            targets=[KNOWN_TARGETS["copilot"]],
            project_root=tmp_path / "project",
            user_scope=False,
            verbose=False,
            logger=logger,
        )
        assert count == 0
        assert any("simulated wiring failure" in w for w in warnings)


# ---------------------------------------------------------------------------
# PR #1217 review: nested instruction paths must not collide at staging
# ---------------------------------------------------------------------------


class TestNestedInstructionStaging:
    def test_same_basename_in_different_subdirs_does_not_collide(self, tmp_path: Path) -> None:
        """Two ``instructions/<sub>/x.md`` entries with the same leaf
        name must stage to distinct paths under
        ``apm_modules/<slug>/.apm/instructions/``, not collapse to one
        ``x.md`` (regression trap for ``Path(rel).name`` collapsing).
        """
        bundle = _build_bundle(
            tmp_path,
            files={
                "instructions/a/x.md": "# A\n",
                "instructions/b/x.md": "# B\n",
                "instructions/top.md": "# top\n",
            },
        )
        project = tmp_path / "project"
        project.mkdir()
        bi = _bundle_info(bundle)

        result = integrate_local_bundle(
            bi,
            project,
            targets=[KNOWN_TARGETS["opencode"]],
            force=False,
            dry_run=False,
            diagnostics=None,
            logger=None,
            scope=None,
            alias=None,
        )

        deployed = {f.replace("\\", "/") for f in result["deployed_files"]}
        # Both nested entries land at distinct staged paths.
        assert "apm_modules/ai/.apm/instructions/a/x.md" in deployed
        assert "apm_modules/ai/.apm/instructions/b/x.md" in deployed
        # And the flat one preserves its leaf.
        assert "apm_modules/ai/.apm/instructions/top.md" in deployed
        # Real files exist on disk.
        assert (project / "apm_modules/ai/.apm/instructions/a/x.md").read_text() == "# A\n"
        assert (project / "apm_modules/ai/.apm/instructions/b/x.md").read_text() == "# B\n"
