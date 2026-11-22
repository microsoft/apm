"""Claude instruction routing onto AGENTS.md (#3939)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationConfig
from apm_cli.compilation.claude_agents_md import (
    claude_agents_md_blocker,
    imports_agents_md,
    routes_to_agents_md,
)
from apm_cli.compilation.claude_formatter import CLAUDE_HEADER
from apm_cli.compilation.constants import DISTRIBUTED_AGENTS_MD_GENERATED_MARKER
from apm_cli.core.scope import InstallScope
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.primitives.models import Instruction, PrimitiveCollection
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-claude-agents-md-instruction-routing")]


@pytest.mark.parametrize(
    ("apply_to", "routed"),
    [
        (None, True),
        ("", True),
        ("**", True),
        ("**/*", True),
        ("src/test/**", True),
        ("docs/**/*", True),
        (["src/api/**"], True),
        ("**/*.py", False),
        ("src/**/*.py", False),
        ("*/test/**", False),
        ("src/test/**, docs/**", False),
        ("../outside/**", False),
        ("src/{a,b}/**", False),
    ],
)
def test_routes_only_always_on_and_single_directory_globs(apply_to, routed):
    assert routes_to_agents_md(apply_to) is routed


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("@AGENTS.md\n", True),
        ("See @./AGENTS.md for the shared rules.\n", True),
        ("Shared rules: @AGENTS.md.\n", True),
        ("Mention `@AGENTS.md` only.\n", False),
        ("```\n@AGENTS.md\n```\n", False),
        ("@AGENTS.md.bak\n", False),
        ("email me@AGENTS.md\n", False),
    ],
)
def test_import_detection_ignores_code_and_lookalikes(content, expected):
    assert imports_agents_md(content) is expected


class TestBlocker:
    def test_clean_project_is_not_blocked(self, tmp_path):
        assert claude_agents_md_blocker(tmp_path) is None

    def test_hand_authored_claude_md_without_import_blocks(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Team notes\n", encoding="utf-8")
        assert "does not import @AGENTS.md" in claude_agents_md_blocker(tmp_path)

    def test_hand_authored_claude_md_with_import_is_ready(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n# Team notes\n", encoding="utf-8")
        assert claude_agents_md_blocker(tmp_path) is None

    def test_generated_claude_md_without_import_is_ready(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text(f"# CLAUDE.md\n{CLAUDE_HEADER}\n", encoding="utf-8")
        assert claude_agents_md_blocker(tmp_path) is None

    def test_hand_authored_agents_md_blocks_unless_managed_section(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# Team AGENTS.md\n", encoding="utf-8")
        assert "AGENTS.md is hand-authored" in claude_agents_md_blocker(tmp_path)

        (tmp_path / "apm.yml").write_text(
            "name: p\nversion: 1.0.0\ncompilation:\n  agents_md:\n    mode: managed_section\n",
            encoding="utf-8",
        )
        (tmp_path / "AGENTS.md").write_text(
            "# Team AGENTS.md\n<!-- apm:start -->\n<!-- apm:end -->\n", encoding="utf-8"
        )
        assert claude_agents_md_blocker(tmp_path) is None

    @pytest.mark.parametrize(
        "body",
        [
            "# Team notes only\n",
            "<!-- apm:start -->\n<!-- apm:start -->\n<!-- apm:end -->\n",
            "<!-- apm:end -->\n<!-- apm:start -->\n",
        ],
        ids=["missing", "duplicated", "reversed"],
    )
    def test_managed_section_without_one_marker_pair_keeps_the_rules(self, tmp_path, body):
        """Compile would reject the file, so install keeps .claude/rules/ (#4787)."""
        (tmp_path / ".claude").mkdir()
        (tmp_path / "apm.yml").write_text(
            "name: p\nversion: 1.0.0\ncompilation:\n  agents_md:\n    mode: managed_section\n",
            encoding="utf-8",
        )
        (tmp_path / "AGENTS.md").write_text(body, encoding="utf-8")
        diagnostics = DiagnosticCollector()

        deployed = _deploy(tmp_path, _package(tmp_path, {"style": "**"}), diagnostics=diagnostics)

        assert deployed == ["style.md"]
        (warning,) = [d.message for d in diagnostics.by_category()["warning"]]
        assert "<!-- apm:start --> ... <!-- apm:end --> managed section" in warning

    def test_symlinked_agents_md_blocks_even_with_managed_section(self, tmp_path):
        (tmp_path / "apm.yml").write_text(
            "name: p\nversion: 1.0.0\ncompilation:\n  agents_md:\n    mode: managed_section\n",
            encoding="utf-8",
        )
        (tmp_path / "shared.md").write_text("# Shared\n", encoding="utf-8")
        (tmp_path / "AGENTS.md").symlink_to(tmp_path / "shared.md")
        assert claude_agents_md_blocker(tmp_path) == (
            "AGENTS.md is a symlink; replace it with a regular file"
        )


class TestDirectoryDestination:
    """A directory instruction routes only when compile can deliver <dir>/AGENTS.md."""

    def test_clean_directory_routes(self, tmp_path):
        (tmp_path / "docs").mkdir()
        assert routes_to_agents_md("docs/**", tmp_path) is True

    def test_generated_nested_agents_md_routes(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "AGENTS.md").write_text(
            f"{DISTRIBUTED_AGENTS_MD_GENERATED_MARKER}\n# docs\n", encoding="utf-8"
        )
        assert routes_to_agents_md("docs/**", tmp_path) is True

    @pytest.mark.parametrize(
        ("name", "content"),
        [("AGENTS.md", "# Hand-authored docs guidance\n"), ("CLAUDE.md", "# Docs notes\n")],
    )
    def test_hand_authored_nested_file_keeps_the_rule(self, tmp_path, name, content):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / name).write_text(content, encoding="utf-8")
        assert routes_to_agents_md("docs/**", tmp_path) is False
        assert routes_to_agents_md("**", tmp_path) is True

    def test_nested_claude_md_with_import_routes(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        assert routes_to_agents_md("docs/**", tmp_path) is True

    def test_directory_symlinked_outside_the_project_keeps_the_rule(self, tmp_path):
        """Compile refuses ``src/AGENTS.md`` outside the project, so install keeps the rule."""
        project, outside = tmp_path / "project", tmp_path / "outside"
        (project / ".claude").mkdir(parents=True)
        outside.mkdir()
        (project / "src").symlink_to(outside, target_is_directory=True)

        assert routes_to_agents_md("src/**", project) is False
        assert _deploy(project, _package(tmp_path, {"src": "src/**"})) == ["src.md"]

    def test_nested_git_repository_keeps_the_rule(self, tmp_path):
        (tmp_path / "vendor" / "lib" / "src").mkdir(parents=True)
        (tmp_path / "vendor" / "lib" / ".git").mkdir()
        assert routes_to_agents_md("vendor/lib/src/**", tmp_path) is False
        assert routes_to_agents_md("vendor/lib/**", tmp_path) is False


def _package(tmp_path: Path, files: dict[str, str]) -> SimpleNamespace:
    instructions = tmp_path / "pkg" / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    for name, apply_to in files.items():
        (instructions / f"{name}.instructions.md").write_text(
            f"---\napplyTo: '{apply_to}'\n---\n\n# {name}\n", encoding="utf-8"
        )
    return SimpleNamespace(install_path=tmp_path / "pkg", package=SimpleNamespace(name="pkg"))


def _deploy(tmp_path, package, **kwargs) -> list[str]:
    result = InstructionIntegrator().integrate_instructions_for_target(
        KNOWN_TARGETS["claude"], package, tmp_path, **kwargs
    )
    return sorted(path.name for path in result.target_paths)


def _compile_package(project: Path, package: SimpleNamespace, *, source_dir: Path | None = None):
    """Compile *package*'s instructions for Claude the way ``apm compile`` does."""
    from apm_cli.primitives.parser import parse_primitive_file

    primitives = PrimitiveCollection()
    apm_dir = package.install_path / ".apm"
    for path in sorted(apm_dir.rglob("*.md")):
        primitives.add_primitive(parse_primitive_file(path, source="dependency:pkg"))
    compiler = AgentsCompiler(str(project), str(source_dir) if source_dir else None)
    return compiler.compile(CompilationConfig(target="claude"), primitives)


class TestInstallRouting:
    FILES = {"style": "**", "tests": "src/test/**", "python": "**/*.py"}  # noqa: RUF012

    def test_project_install_keeps_only_file_type_rules(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        (tmp_path / "src" / "test").mkdir(parents=True)
        integrator = InstructionIntegrator()
        result = integrator.integrate_instructions_for_target(
            KNOWN_TARGETS["claude"], _package(tmp_path, self.FILES), tmp_path
        )
        assert [p.name for p in result.target_paths] == ["python.md"]
        assert integrator.agents_md_routed == {
            "style.instructions.md",
            "tests.instructions.md",
        }

    def test_compile_reminder_only_while_agents_md_lacks_the_instruction(self, tmp_path):
        """A no-op reinstall after compile is quiet; a changed body asks again."""
        (tmp_path / ".claude").mkdir()
        (tmp_path / "src" / "test").mkdir(parents=True)
        package = _package(tmp_path, self.FILES)

        def stale() -> set[str]:
            integrator = InstructionIntegrator()
            integrator.integrate_instructions_for_target(KNOWN_TARGETS["claude"], package, tmp_path)
            return integrator.agents_md_stale

        assert stale() == {"style.instructions.md", "tests.instructions.md"}
        _compile_package(tmp_path, package)
        assert stale() == set()
        # Claude loads AGENTS.md only through a CLAUDE.md that imports it; a
        # pre-#3939 generated CLAUDE.md does not, so compile must run again.
        (tmp_path / "CLAUDE.md").write_text(f"# CLAUDE.md\n{CLAUDE_HEADER}\n", encoding="utf-8")
        assert stale() == {"style.instructions.md"}
        (tmp_path / "CLAUDE.md").write_text(
            f"# CLAUDE.md\n{CLAUDE_HEADER}\n@AGENTS.md\n", encoding="utf-8"
        )
        assert stale() == set()
        source = package.install_path / ".apm" / "instructions" / "style.instructions.md"
        source.write_text(source.read_text(encoding="utf-8") + "More.\n", encoding="utf-8")
        assert stale() == {"style.instructions.md"}

    def test_directory_rule_stays_when_directory_is_missing(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        assert _deploy(tmp_path, _package(tmp_path, self.FILES)) == ["python.md", "tests.md"]

    def test_single_file_project_keeps_directory_rule(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        (tmp_path / "src" / "test").mkdir(parents=True)
        (tmp_path / "apm.yml").write_text(
            "name: p\nversion: 1.0.0\ncompilation:\n  strategy: single-file\n",
            encoding="utf-8",
        )
        assert _deploy(tmp_path, _package(tmp_path, self.FILES)) == ["python.md", "tests.md"]

    def test_user_scope_keeps_every_rule(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        package = _package(tmp_path, self.FILES)
        deployed = _deploy(tmp_path, package, scope=InstallScope.USER)
        assert deployed == ["python.md", "style.md", "tests.md"]

    def test_blocked_project_keeps_every_rule_and_warns_once(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        (tmp_path / "CLAUDE.md").write_text("# Team notes\n", encoding="utf-8")
        package = _package(tmp_path, self.FILES)
        diagnostics = DiagnosticCollector()
        integrator = InstructionIntegrator()

        for _ in range(2):
            result = integrator.integrate_instructions_for_target(
                KNOWN_TARGETS["claude"], package, tmp_path, diagnostics=diagnostics, force=True
            )

        assert sorted(p.name for p in result.target_paths) == [
            "python.md",
            "style.md",
            "tests.md",
        ]
        warnings = [d.message for d in diagnostics.by_category().get("warning", [])]
        assert len([w for w in warnings if "@AGENTS.md" in w]) == 1
        assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "# Team notes\n"

    def test_hand_authored_agents_md_warning_names_managed_section(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        (tmp_path / "AGENTS.md").write_text("# Team AGENTS.md\n", encoding="utf-8")
        diagnostics = DiagnosticCollector()
        InstructionIntegrator().integrate_instructions_for_target(
            KNOWN_TARGETS["claude"],
            _package(tmp_path, self.FILES),
            tmp_path,
            diagnostics=diagnostics,
        )
        (warning,) = [d.message for d in diagnostics.by_category()["warning"]]
        assert "compilation.agents_md.mode: managed_section" in warning
        assert "add '@AGENTS.md'" not in warning

    def test_replay_reads_blocker_from_live_project(self, tmp_path):
        live, scratch = tmp_path / "live", tmp_path / "scratch"
        (live / ".claude").mkdir(parents=True)
        (scratch / ".claude").mkdir(parents=True)
        (live / "CLAUDE.md").write_text("# Team notes\n", encoding="utf-8")
        package = _package(tmp_path, self.FILES)
        package.deployment_frame = (scratch, live)

        assert _deploy(scratch, package) == ["python.md", "style.md", "tests.md"]


class TestCompileShims:
    def _primitives(self, root: Path) -> PrimitiveCollection:
        primitives = PrimitiveCollection()
        for name, apply_to in (("style", "**"), ("tests", "src/test/**"), ("py", "**/*.py")):
            primitives.add_primitive(
                Instruction(
                    name=name,
                    file_path=root / ".apm" / "instructions" / f"{name}.instructions.md",
                    description=name,
                    apply_to=apply_to,
                    content=f"{name.upper()}_BODY",
                    author="test",
                )
            )
        return primitives

    def _compile(self, root: Path):
        for relative in ("src/test/a_test.py", "src/app/b.py", "docs/guide.md"):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text("x = 1\n", encoding="utf-8")
        rules = root / ".claude" / "rules"
        rules.mkdir(parents=True)
        (rules / "py.md").write_text("---\npaths:\n  - '**/*.py'\n---\nPY_BODY\n")
        result = AgentsCompiler(str(root)).compile(
            CompilationConfig(target="claude", resolve_links=False), self._primitives(root)
        )
        assert result.success, result.errors
        return result

    def test_root_and_nested_agents_md_with_claude_imports(self, tmp_path):
        self._compile(tmp_path)

        root_agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        nested_agents = (tmp_path / "src" / "test" / "AGENTS.md").read_text(encoding="utf-8")
        assert "STYLE_BODY" in root_agents
        assert "TESTS_BODY" in nested_agents
        # The deployed file-type rule is not duplicated into AGENTS.md.
        assert "PY_BODY" not in root_agents + nested_agents
        for claude_md in (tmp_path / "CLAUDE.md", tmp_path / "src" / "test" / "CLAUDE.md"):
            assert "@AGENTS.md" in claude_md.read_text(encoding="utf-8").splitlines()

    def test_widely_matching_directory_glob_is_pinned_not_hoisted(self, tmp_path):
        """A ``src/**`` the optimizer would hoist to root stays in src/AGENTS.md."""
        for name in ("a", "b", "c", "d"):
            (tmp_path / "src" / name).mkdir(parents=True)
            (tmp_path / "src" / name / "x.py").write_text("x = 1\n", encoding="utf-8")
        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="src",
                file_path=tmp_path / ".apm" / "instructions" / "src.instructions.md",
                description="src",
                apply_to="src/**",
                content="SRC_BODY",
                author="test",
            )
        )
        result = AgentsCompiler(str(tmp_path)).compile(
            CompilationConfig(target="claude", resolve_links=False), primitives
        )

        assert result.success, result.errors
        assert "SRC_BODY" in (tmp_path / "src" / "AGENTS.md").read_text(encoding="utf-8")
        root_agents = tmp_path / "AGENTS.md"
        assert not root_agents.exists() or "SRC_BODY" not in root_agents.read_text("utf-8")

    @pytest.mark.parametrize(
        ("rule", "carried"),
        [
            ('---\npaths:\n  - "**/*.py"\n---\nOTHER_BODY\n', True),
            ('---\npaths:\n  - "**"\n---\nSTYLE_BODY edited\n', False),
        ],
        ids=["other-source-rule", "kept-legacy-rule"],
    )
    def test_same_named_rule_covers_an_instruction_only_with_its_scope(
        self, tmp_path, rule, carried
    ):
        """Another source's ``style.md`` must not hide an always-on ``style`` (#4005 review)."""
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / ".claude" / "rules").mkdir(parents=True)
        (tmp_path / ".claude" / "rules" / "style.md").write_text(rule, encoding="utf-8")
        primitives = PrimitiveCollection()
        primitives.add_primitive(
            Instruction(
                name="style",
                file_path=tmp_path / "apm_modules" / "o" / "a" / "style.instructions.md",
                description="style",
                apply_to="**",
                content="STYLE_BODY",
                author="test",
            )
        )

        result = AgentsCompiler(str(tmp_path)).compile(
            CompilationConfig(target="claude", resolve_links=False), primitives
        )

        assert result.success, result.errors
        agents_md = tmp_path / "AGENTS.md"
        text = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
        assert ("STYLE_BODY" in text) is carried

    def test_single_file_compile_keeps_directory_rules_out_of_root(self, tmp_path):
        (tmp_path / "src" / "test").mkdir(parents=True)
        result = AgentsCompiler(str(tmp_path)).compile(
            CompilationConfig(target="claude", resolve_links=False, single_agents=True),
            self._primitives(tmp_path),
        )

        assert result.success, result.errors
        root_agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        assert "STYLE_BODY" in root_agents
        assert "TESTS_BODY" not in root_agents
        assert any("single-file AGENTS.md" in warning for warning in result.warnings)

    def test_user_claude_md_with_import_is_left_alone_silently(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n# Team notes\n", encoding="utf-8")

        result = self._compile(tmp_path)

        assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n# Team notes\n"
        assert not any("CLAUDE.md" in warning for warning in result.warnings)

    def test_hand_authored_claude_md_without_import_warns(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Team notes\n", encoding="utf-8")
        (tmp_path / "src" / "test").mkdir(parents=True)
        (tmp_path / "src" / "test" / "a_test.py").write_text("x = 1\n", encoding="utf-8")

        result = AgentsCompiler(str(tmp_path)).compile(
            CompilationConfig(target="claude", resolve_links=False), self._primitives(tmp_path)
        )

        assert any("add '@AGENTS.md' to CLAUDE.md" in w for w in result.warnings)
        assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "# Team notes\n"


class TestPackRoutedRules:
    """The pack renders routed rules with install's authorization (#4005 review)."""

    def test_divergent_same_path_renders_are_refused(self):
        from apm_cli.bundle.claude_routed_rules import merged_routed_claude_rules

        same = {"a/x": {".claude/rules/style.md": "one"}, "b/y": {".claude/rules/style.md": "one"}}
        assert merged_routed_claude_rules(same) == {".claude/rules/style.md": "one"}
        with pytest.raises(ValueError, match=r"a/x and b/y both route a different instruction"):
            merged_routed_claude_rules(
                {"a/x": {".claude/rules/style.md": "one"}, "b/y": {".claude/rules/style.md": "two"}}
            )

    @pytest.mark.parametrize(("targets", "rendered"), [("[copilot]", False), ("[claude]", True)])
    def test_package_targets_restriction_applies(self, tmp_path, targets, rendered):
        from apm_cli.deps.lockfile import LockedDependency, LockFile
        from apm_cli.integration.instruction_integrator import routed_claude_rule_renders

        lockfile = LockFile()
        lockfile.add_dependency(LockedDependency(repo_url="owner/kit", resolved_commit="a" * 40))
        package = tmp_path / "apm_modules" / "owner" / "kit"
        (package / ".apm" / "instructions").mkdir(parents=True)
        (package / "apm.yml").write_text(
            f"name: kit\nversion: 1.0.0\ntargets: {targets}\n", encoding="utf-8"
        )
        (package / ".apm" / "instructions" / "style.instructions.md").write_text(
            "---\napplyTo: '**'\n---\n# Style\n", encoding="utf-8"
        )

        rules = routed_claude_rule_renders(
            lockfile, tmp_path, tmp_path / "apm_modules", include_local=False
        )

        assert bool(rules) is rendered

    @staticmethod
    def _cached_kit(tmp_path, instruction="---\napplyTo: '**'\n---\n# Style\n"):
        package = tmp_path / "apm_modules" / "owner" / "kit"
        (package / ".apm" / "instructions").mkdir(parents=True)
        (package / "apm.yml").write_text("name: kit\nversion: 1.0.0\n", encoding="utf-8")
        (package / ".apm" / "instructions" / "style.instructions.md").write_text(
            instruction, encoding="utf-8"
        )
        return package

    def test_pack_refuses_routed_rules_without_a_locked_content_hash(self, tmp_path):
        """No ``content_hash`` means nothing attests the cache the rule renders from."""
        from apm_cli.deps.lockfile import LockedDependency, LockFile
        from apm_cli.integration.instruction_integrator import routed_claude_rule_renders

        self._cached_kit(tmp_path)
        lockfile = LockFile()
        lockfile.add_dependency(LockedDependency(repo_url="owner/kit", resolved_commit="a" * 40))

        with pytest.raises(ValueError, match="records no content hash"):
            routed_claude_rule_renders(
                lockfile, tmp_path, tmp_path / "apm_modules", include_local=False, verified=True
            )

        # A lock that already records the rule as deployed packs it from disk.
        lockfile.dependencies["owner/kit"].deployed_files = [".claude/rules/style.md"]
        assert (
            routed_claude_rule_renders(
                lockfile, tmp_path, tmp_path / "apm_modules", include_local=False, verified=True
            )
            == {}
        )

    def test_routed_rule_at_a_deployed_path_merges_only_identical_bytes(self, tmp_path):
        """A deployed same-path rule ships once when equal, else the pack refuses."""
        from apm_cli.bundle.claude_routed_rules import merged_routed_claude_rules

        deployed = tmp_path / "style.md"
        deployed.write_bytes(b"one")
        routed = {"a/x": {".claude/rules/style.md": "one"}}
        shipped = {".claude/rules/style.md": deployed}

        assert merged_routed_claude_rules(routed, shipped) == {}
        deployed.write_bytes(b"two")
        with pytest.raises(ValueError, match=r"another dependency deploys a different file"):
            merged_routed_claude_rules(routed, shipped)


class TestRoutingFollowUps:
    """The #4005 review follow-ups (#4787); each cell fails without its fix."""

    def test_pack_renders_only_instructions_install_admits(self, tmp_path):
        """A root-level instruction install never deploys is not packed (item 1)."""
        from apm_cli.deps.lockfile import LockedDependency, LockFile
        from apm_cli.integration.instruction_integrator import routed_claude_rule_renders

        package = TestPackRoutedRules._cached_kit(tmp_path)
        (package / "excluded.instructions.md").write_text(
            "---\napplyTo: '**'\n---\n# Excluded\n", encoding="utf-8"
        )
        lockfile = LockFile()
        lockfile.add_dependency(LockedDependency(repo_url="owner/kit", resolved_commit="a" * 40))

        rules = routed_claude_rule_renders(
            lockfile, tmp_path, tmp_path / "apm_modules", include_local=False
        )

        assert sorted(rules["owner/kit"]) == [".claude/rules/style.md"]

    def _stale(self, project: Path, package: SimpleNamespace) -> set[str]:
        integrator = InstructionIntegrator()
        integrator.integrate_instructions_for_target(KNOWN_TARGETS["claude"], package, project)
        return integrator.agents_md_stale

    def test_reminder_clears_after_compile_rewrites_a_context_link(self, tmp_path):
        """Compile rewrites the link; a no-op reinstall stays quiet (item 2)."""
        project = tmp_path / "project"
        (project / ".claude").mkdir(parents=True)
        package = _package(tmp_path, {})
        (package.install_path / ".apm" / "context").mkdir()
        (package.install_path / ".apm" / "context" / "notes.context.md").write_text(
            "---\ndescription: notes\n---\nNotes.\n", encoding="utf-8"
        )
        (package.install_path / ".apm" / "instructions" / "style.instructions.md").write_text(
            "---\napplyTo: '**'\ndescription: style\n---\n\n"
            "Read [the notes](../context/notes.context.md) first.\n",
            encoding="utf-8",
        )

        assert _compile_package(project, package).success
        compiled = (project / "AGENTS.md").read_text(encoding="utf-8")
        assert "[the notes](../context/notes.context.md)" not in compiled
        assert "[the notes](" in compiled

        assert self._stale(project, package) == set()

    @pytest.mark.parametrize("edit", ["narrowed-scope", "shortened-body"])
    def test_scope_or_body_edit_after_compile_asks_again(self, tmp_path, edit):
        """A substring of the compiled text is not the compiled instruction (item 7)."""
        project = tmp_path / "project"
        (project / ".claude").mkdir(parents=True)
        (project / "src").mkdir()
        package = _package(tmp_path, {})
        source = package.install_path / ".apm" / "instructions" / "style.instructions.md"
        source.write_text(
            "---\napplyTo: '**'\ndescription: style\n---\n\nKeep it short.\nUse ASCII.\n",
            encoding="utf-8",
        )
        assert _compile_package(project, package).success
        assert self._stale(project, package) == set()

        text = source.read_text(encoding="utf-8")
        if edit == "narrowed-scope":
            source.write_text(text.replace("applyTo: '**'", "applyTo: 'src/**'"), "utf-8")
        else:
            source.write_text(text.replace("Use ASCII.\n", ""), encoding="utf-8")

        assert self._stale(project, package) == {"style.instructions.md"}

    @staticmethod
    def _agents_md_reads(tmp_path, monkeypatch, rules: int) -> int:
        project = tmp_path / f"project{rules}"
        (project / ".claude").mkdir(parents=True)
        package = _package(tmp_path / f"pkg{rules}", {f"rule{i}": "**" for i in range(rules)})
        assert _compile_package(project, package).success
        agents_md = project / "AGENTS.md"
        reads: list[Path] = []
        real_open, real_read_bytes = Path.open, Path.read_bytes

        def counting_open(self, *args, **kwargs):
            if self == agents_md:
                reads.append(self)
            return real_open(self, *args, **kwargs)

        def counting_read_bytes(self):
            if self == agents_md:
                reads.append(self)
            return real_read_bytes(self)

        with monkeypatch.context() as patched:
            patched.setattr(Path, "open", counting_open)
            patched.setattr(Path, "read_bytes", counting_read_bytes)
            integrator = InstructionIntegrator()
            integrator.integrate_instructions_for_target(KNOWN_TARGETS["claude"], package, project)
        assert integrator.agents_md_stale == set()
        return len(reads)

    def test_one_install_reads_the_compiled_agents_md_a_fixed_number_of_times(
        self, tmp_path, monkeypatch
    ):
        """Twenty routed instructions read AGENTS.md as often as one does (item 4)."""
        one = self._agents_md_reads(tmp_path, monkeypatch, 1)
        assert self._agents_md_reads(tmp_path, monkeypatch, 20) == one

    def test_compile_root_places_directory_guidance_from_the_source_tree(self, tmp_path):
        """``--root <empty dir>`` still writes src/AGENTS.md and its shim (item 5)."""
        source, output = tmp_path / "source", tmp_path / "output"
        (source / "src").mkdir(parents=True)
        (source / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        output.mkdir()
        package = _package(tmp_path, {"src": "src/**"})

        result = _compile_package(output, package, source_dir=source)

        assert result.success, result.errors
        assert "# src" in (output / "src" / "AGENTS.md").read_text(encoding="utf-8")
        assert "@AGENTS.md" in (output / "src" / "CLAUDE.md").read_text(encoding="utf-8")

    def test_rejected_agents_md_batch_writes_no_claude_shim(self, tmp_path):
        """A batch the output gate rejects leaves no import of a missing file (item 6)."""
        project = tmp_path / "project"
        (project / "src").mkdir(parents=True)
        package = _package(tmp_path, {"src": "src/**"})
        (package.install_path / ".apm" / "instructions" / "style.instructions.md").write_text(
            "---\napplyTo: '**'\ndescription: style\n---\n\nHidden \u202e text.\n",
            encoding="utf-8",
        )

        result = _compile_package(project, package)

        assert not result.success
        assert not (project / "AGENTS.md").exists()
        assert not (project / "CLAUDE.md").exists()
        assert not (project / "src" / "CLAUDE.md").exists()
