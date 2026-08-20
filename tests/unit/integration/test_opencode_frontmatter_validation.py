"""Tests for OpenCode frontmatter validate-and-warn (Phase 1 of #581).

Covers both the pure validator and the install-time integration that
fires it before copy_agent() writes the file to .opencode/agents/.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from apm_cli.integration import AgentIntegrator
from apm_cli.integration.opencode_frontmatter import validate_opencode_frontmatter
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, GitReferenceType, PackageInfo, ResolvedReference
from apm_cli.utils.diagnostics import DiagnosticCollector


def _make_package_info(pkg_dir: Path, *, name: str = "test-pkg") -> PackageInfo:
    package = APMPackage(name=name, version="1.0.0", package_path=pkg_dir)
    resolved_ref = ResolvedReference(
        original_ref="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="abc123",
        ref_name="main",
    )
    return PackageInfo(
        package=package,
        install_path=pkg_dir,
        resolved_reference=resolved_ref,
        installed_at="2024-01-01T00:00:00",
    )


def _warning_messages(diagnostics: DiagnosticCollector) -> list[str]:
    # Surface raw warning messages so tests can assert on substrings.
    return [d.message for d in diagnostics._diagnostics if d.category == "warning"]


class TestValidateOpencodeFrontmatter:
    """Pure unit tests for validate_opencode_frontmatter()."""

    def test_tools_as_dict_no_warning(self):
        msgs = validate_opencode_frontmatter(
            {"tools": {"Read": True, "Grep": False}},
            Path("agent.md"),
        )
        assert msgs == []

    def test_tools_as_list_warns(self):
        msgs = validate_opencode_frontmatter(
            {"tools": ["Read", "Grep"]},
            Path("bad.agent.md"),
        )
        assert len(msgs) == 1
        assert "bad.agent.md" in msgs[0]
        assert "tools" in msgs[0]
        assert "list" in msgs[0]

    def test_tools_as_string_warns(self):
        msgs = validate_opencode_frontmatter(
            {"tools": "Read, Grep, Glob"},
            Path("claude.agent.md"),
        )
        assert len(msgs) == 1
        assert "tools" in msgs[0]
        assert "str" in msgs[0]

    def test_tools_dict_with_non_bool_value_warns(self):
        msgs = validate_opencode_frontmatter(
            {"tools": {"Read": "yes"}},
            Path("bad.agent.md"),
        )
        assert len(msgs) == 1
        assert "non-boolean" in msgs[0]

    def test_color_hex_no_warning(self):
        msgs = validate_opencode_frontmatter(
            {"color": "#aabbcc"},
            Path("agent.md"),
        )
        assert msgs == []

    def test_color_short_hex_no_warning(self):
        msgs = validate_opencode_frontmatter({"color": "#abc"}, Path("a.md"))
        assert msgs == []

    def test_color_theme_enum_no_warning(self):
        for theme in ("primary", "secondary", "accent", "success", "warning", "error", "info"):
            assert validate_opencode_frontmatter({"color": theme}, Path("a.md")) == []

    def test_color_named_not_in_enum_warns(self):
        msgs = validate_opencode_frontmatter(
            {"color": "cyan"},
            Path("cyan.agent.md"),
        )
        assert len(msgs) == 1
        assert "color" in msgs[0]
        assert "cyan" in msgs[0]

    def test_color_non_string_warns(self):
        msgs = validate_opencode_frontmatter({"color": 123}, Path("a.md"))
        assert len(msgs) == 1
        assert "color" in msgs[0]

    def test_empty_frontmatter_no_warning(self):
        assert validate_opencode_frontmatter({}, Path("a.md")) == []
        assert validate_opencode_frontmatter(None, Path("a.md")) == []

    def test_multiple_problems_each_warned(self):
        msgs = validate_opencode_frontmatter(
            {"tools": ["Read"], "color": "magenta"},
            Path("a.md"),
        )
        assert len(msgs) == 2

    def test_messages_are_ascii(self):
        msgs = validate_opencode_frontmatter(
            {"tools": ["x"], "color": "magenta"},
            Path("a.md"),
        )
        for m in msgs:
            m.encode("ascii")  # raises if non-ASCII

    def test_non_ascii_filename_sanitized(self):
        msgs = validate_opencode_frontmatter(
            {"color": "magenta"},
            Path("cy\u00e1n-agent.md"),
        )
        assert len(msgs) == 1
        # Non-ASCII filename codepoints replaced with '?' so message stays ASCII.
        msgs[0].encode("ascii")
        assert "?" in msgs[0]

    def test_non_ascii_color_value_sanitized(self):
        msgs = validate_opencode_frontmatter(
            {"color": "magent\u00e1"},
            Path("a.md"),
        )
        assert len(msgs) == 1
        # ascii() escapes non-ASCII codepoints in the repr.
        msgs[0].encode("ascii")
        assert "\\xe1" in msgs[0]

    def test_non_ascii_tool_key_and_value_sanitized(self):
        msgs = validate_opencode_frontmatter(
            {"tools": {"R\u00e9ad": "y\u00e8s"}},
            Path("a.md"),
        )
        assert len(msgs) == 1
        msgs[0].encode("ascii")
        # Both key and value escaped via ascii() rather than raw !r.
        assert "\\xe9" in msgs[0]
        assert "\\xe8" in msgs[0]

    def test_fm_none_accepted_by_signature(self):
        # Annotation is dict | None; calling with None must not raise.
        assert validate_opencode_frontmatter(None, Path("a.md")) == []

    def test_package_qualifier_prefixes_identifier(self):
        # Multi-package installs benefit from knowing which dependency
        # shipped the bad frontmatter; the package name appears as a
        # '<pkg>/<file>' prefix in every warning.
        msgs = validate_opencode_frontmatter(
            {"tools": ["Read"]},
            Path("demo.agent.md"),
            package_name="acme/security-pack",
        )
        assert len(msgs) == 1
        assert "'acme/security-pack/demo.agent.md'" in msgs[0]

    def test_package_qualifier_omitted_when_not_supplied(self):
        # Backward compatibility: bare filename when no package given.
        msgs = validate_opencode_frontmatter(
            {"tools": ["Read"]},
            Path("demo.agent.md"),
        )
        assert "'demo.agent.md'" in msgs[0]

    def test_package_qualifier_sanitized(self):
        # ASCII control chars and non-ASCII codepoints in the package
        # name are stripped/replaced so a malicious package name can
        # never inject ANSI escapes via the warning channel.
        msgs = validate_opencode_frontmatter(
            {"color": "magenta"},
            Path("a.md"),
            package_name="evil\x1b[31mpkg",
        )
        assert len(msgs) == 1
        msgs[0].encode("ascii")
        assert "\x1b" not in msgs[0]

    def test_filename_control_chars_stripped(self):
        # ASCII control chars in the filename (DEL, ESC, BEL) get
        # replaced by '?' rather than echoed verbatim, defending the
        # terminal against agent files crafted to inject escape codes.
        msgs = validate_opencode_frontmatter(
            {"color": "magenta"},
            Path("a\x1b[31mb.agent.md"),
        )
        assert len(msgs) == 1
        msgs[0].encode("ascii")
        assert "\x1b" not in msgs[0]
        assert "?" in msgs[0]

    def test_remediation_pointer_in_tools_warning(self):
        msgs = validate_opencode_frontmatter({"tools": ["Read"]}, Path("a.md"))
        assert "Fix:" in msgs[0]
        assert "tools:" in msgs[0]

    def test_remediation_pointer_in_color_warning(self):
        msgs = validate_opencode_frontmatter({"color": "cyan"}, Path("a.md"))
        assert "Fix:" in msgs[0]
        # Both 3- and 6-char hex literals are accepted by the validator;
        # the remediation pointer must mention both so users aren't
        # misled into thinking only '#rrggbb' is allowed.
        assert "#rgb" in msgs[0]
        assert "#rrggbb" in msgs[0]


class TestOpencodeInstallEmitsWarnings:
    """End-to-end: integrate_agents_for_target() emits diagnostics.warn()
    for OpenCode-incompatible frontmatter before deploying the file."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)
        self.integrator = AgentIntegrator()
        # OpenCode has auto_create=False and detect_by_dir=True; create
        # the marker directory so integration runs.
        (self.project_root / ".opencode").mkdir()

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_agent(self, frontmatter: str) -> Path:
        pkg = self.project_root / "package"
        agents_dir = pkg / ".apm" / "agents"
        agents_dir.mkdir(parents=True)
        agent_path = agents_dir / "demo.agent.md"
        agent_path.write_text(f"---\n{frontmatter}\n---\n\n# Demo\n")
        return pkg

    def test_tools_as_list_emits_warning(self):
        pkg = self._write_agent("tools:\n  - Read\n  - Grep\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        result = self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        assert result.files_integrated == 1
        msgs = _warning_messages(diagnostics)
        # Warning must name the offending file AND prefix it with the
        # owning package so multi-package installs are diagnosable.
        assert any("tools" in m and "test-pkg/demo.agent.md" in m and "Fix:" in m for m in msgs), (
            msgs
        )

    def test_tools_as_dict_no_warning(self):
        pkg = self._write_agent("tools:\n  Read: true\n  Grep: false\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        msgs = _warning_messages(diagnostics)
        assert not any("OpenCode agent" in m for m in msgs), msgs

    def test_color_named_emits_warning(self):
        pkg = self._write_agent('color: "cyan"\n')
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        msgs = _warning_messages(diagnostics)
        assert any("color" in m and "cyan" in m for m in msgs), msgs

    def test_color_hex_no_warning(self):
        pkg = self._write_agent('color: "#aabbcc"\n')
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        msgs = _warning_messages(diagnostics)
        assert not any("OpenCode agent" in m for m in msgs), msgs

    def test_file_is_still_deployed_when_warning_emitted(self):
        # Warnings must NOT block install: file lands in .opencode/agents/
        # so users can fix the source and reinstall, and so other valid
        # agents in the same package are not held up by the bad one.
        pkg = self._write_agent("tools:\n  - Read\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        result = self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

        assert result.files_integrated == 1
        deployed = self.project_root / ".opencode" / "agents" / "demo.md"
        assert deployed.exists()

    def test_rendered_package_name_is_sanitized(self, capsys):
        """OpenCode wrapper attribution must not reintroduce terminal controls."""
        pkg = self._write_agent("tools:\n  - Read\n")
        pkg_info = _make_package_info(pkg, name="evil\x1b[31mpkg\nnext")
        diagnostics = DiagnosticCollector()

        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"],
            pkg_info,
            self.project_root,
            diagnostics=diagnostics,
        )

        warnings = [item for item in diagnostics._diagnostics if item.category == "warning"]
        assert len(warnings) == 1
        assert warnings[0].package == "evil?[31mpkg?next"
        diagnostics.render_summary()
        output = capsys.readouterr().out
        assert "\x1b" not in output
        assert "pkg\nnext" not in output

    def test_malformed_yaml_does_not_crash(self):
        pkg = self._write_agent("tools: [unclosed\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()

        # Should not raise; validator gets empty fm when YAML is invalid.
        self.integrator.integrate_agents_for_target(
            KNOWN_TARGETS["opencode"], pkg_info, self.project_root, diagnostics=diagnostics
        )

    def test_diagnostics_none_does_not_crash(self):
        # Defensive guard: _warn_opencode_frontmatter must early-return
        # when the install path is called without a DiagnosticCollector,
        # so a future caller that omits the collector never crashes the
        # install on otherwise-valid agent files.
        from apm_cli.integration.agent_integrator import AgentIntegrator

        agent_path = self.project_root / "demo.agent.md"
        agent_path.write_text("---\ntools:\n  - Read\n---\n\nBody\n")
        # Must not raise even though the frontmatter would normally warn.
        AgentIntegrator._warn_opencode_frontmatter(agent_path, None, "test-pkg")

    @pytest.mark.parametrize("target_name", ["copilot", "claude", "codex", "windsurf", "cursor"])
    def test_non_opencode_targets_do_not_emit_opencode_warning(self, target_name):
        # The validation is scoped to format_id == "opencode_agent"; other
        # targets must not emit OpenCode-specific warnings even when the
        # frontmatter would be invalid for OpenCode.
        pkg = self._write_agent("tools:\n  - Read\ncolor: cyan\n")
        pkg_info = _make_package_info(pkg)
        diagnostics = DiagnosticCollector()
        target = KNOWN_TARGETS[target_name]
        # Create marker dir for targets that need it.
        marker = self.project_root / target.root_dir
        marker.mkdir(exist_ok=True)

        self.integrator.integrate_agents_for_target(
            target, pkg_info, self.project_root, diagnostics=diagnostics
        )

        msgs = _warning_messages(diagnostics)
        assert not any("OpenCode agent" in m for m in msgs), msgs
