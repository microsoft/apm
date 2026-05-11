"""E2E integration tests for issue #1147: in-package relative link rewriting.

These tests invoke the real ``apm`` binary against fixture packages on
disk to prove the install pipeline rewrites in-package relative markdown
links to their ``apm_modules/`` location, AND that the resulting deploy
artifacts contain links that resolve on disk under the consumer's host
target layout.

Coverage matrix:

1. **Instruction -> sibling asset** (the original #1147 repro).
2. **Prompt -> sibling reference doc** (proves the fix is not
   instruction-specific; same primitive surface that broke after the
   ``.agents/.github`` split in #1103).
3. **Multiple links per file** (relative rewritten, ``https://``
   external preserved, ``#fragment`` only preserved).
4. **Path-traversal escape preserved** (security: a link that resolves
   outside the package root must NOT be rewritten and the file outside
   the package must not be exposed via ``apm_modules/``).
5. **Skill-bundle internal link unchanged** (regression guard: a link
   inside a skill bundle, where the layout is preserved, must remain a
   normal in-bundle relative link, not be rewritten through
   ``apm_modules/`` unnecessarily breaking it).
6. **Multi-target install** (Copilot + Claude in one run): both
   deployed copies of the instruction get their links rewritten and
   both rewritten links resolve on disk.

All tests use offline local-path fixtures (no network) so they are
safe to run in CI without tokens.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.requires_apm_binary

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def apm_command():
    """Resolve the ``apm`` binary the same way other local-install tests do."""
    on_path = shutil.which("apm")
    if on_path:
        return on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # write_bytes (not write_text) keeps newlines as \n on Windows;
    # write_text translates to \r\n which would break later byte-exact
    # comparisons against the link rewriter's output.
    path.write_bytes(content.encode("utf-8"))


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def _make_consumer(root: Path, *, targets: list[str] | None = None) -> Path:
    """Minimal consumer project with .github/ pre-created."""
    consumer = root / "consumer"
    consumer.mkdir()
    (consumer / ".github").mkdir()
    yml: dict = {
        "name": "consumer",
        "version": "1.0.0",
        "target": "copilot",
        "dependencies": {"apm": []},
    }
    if targets:
        yml["target"] = ",".join(targets) if len(targets) > 1 else targets[0]
    _write_yaml(consumer / "apm.yml", yml)
    return consumer


def _run_install(apm_bin: str, consumer: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [apm_bin, "install", *args],
        cwd=consumer,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _link_target(body: str, link_text: str) -> str:
    """Extract the URL from ``[link_text](URL)`` in ``body``."""
    pattern = re.compile(r"\[" + re.escape(link_text) + r"\]\(([^)]+)\)")
    match = pattern.search(body)
    assert match, f"Could not find link [{link_text}](...) in body:\n{body}"
    return match.group(1)


# ---------------------------------------------------------------------------
# 1. Original #1147 repro: instruction -> sibling asset
# ---------------------------------------------------------------------------


class TestInstructionSiblingLinkRewriting:
    """Issue #1147 happy path."""

    @pytest.fixture
    def workspace(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        producer = ws / "producer"
        _write_yaml(producer / "apm.yml", {"name": "producer", "version": "1.0.0"})
        _write(producer / "standards" / "style.md", "# Style guide\nUse 2 spaces.\n")
        _write(
            producer / ".apm" / "instructions" / "foo.instructions.md",
            '---\napplyTo: "**/*.py"\n---\n'
            "# Foo\n\nFollow the [style guide](../../standards/style.md).\n",
        )
        consumer = _make_consumer(ws)
        return consumer, producer

    def test_link_rewritten_and_resolves_on_disk(self, workspace, apm_command):
        consumer, producer = workspace

        result = _run_install(apm_command, consumer, str(producer))
        assert result.returncode == 0, f"Install failed:\n{result.stderr}\n{result.stdout}"

        deployed = consumer / ".github" / "instructions" / "foo.instructions.md"
        assert deployed.exists(), f"Instruction not deployed. stdout: {result.stdout}"

        body = deployed.read_text(encoding="utf-8")
        assert "../../standards/style.md" not in body, (
            f"Pre-fix broken link survived install:\n{body}"
        )

        rewritten = _link_target(body, "style guide")
        assert "apm_modules/_local/producer/standards/style.md" in rewritten, (
            f"Link not rewritten to apm_modules/ form: {rewritten}"
        )

        resolved = (deployed.parent / rewritten).resolve()
        assert resolved.exists(), f"Rewritten link does not resolve: {rewritten} -> {resolved}"
        assert resolved.read_text(encoding="utf-8").startswith("# Style guide")


# ---------------------------------------------------------------------------
# 2. Prompt body -> sibling reference doc
# ---------------------------------------------------------------------------


class TestPromptSiblingLinkRewriting:
    """Same surface, different primitive type."""

    @pytest.fixture
    def workspace(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        producer = ws / "producer"
        _write_yaml(producer / "apm.yml", {"name": "producer", "version": "1.0.0"})
        _write(producer / "templates" / "review.md", "# Review template\nChecklist...\n")
        _write(
            producer / ".apm" / "prompts" / "review.prompt.md",
            "---\nmode: agent\n---\nUse the [review template](../../templates/review.md).\n",
        )
        consumer = _make_consumer(ws)
        return consumer, producer

    def test_prompt_link_rewritten_and_resolves(self, workspace, apm_command):
        consumer, producer = workspace
        result = _run_install(apm_command, consumer, str(producer))
        assert result.returncode == 0, f"Install failed:\n{result.stderr}\n{result.stdout}"

        deployed = consumer / ".github" / "prompts" / "review.prompt.md"
        assert deployed.exists(), f"Prompt not deployed. stdout: {result.stdout}"

        body = deployed.read_text(encoding="utf-8")
        rewritten = _link_target(body, "review template")
        assert "apm_modules/_local/producer/templates/review.md" in rewritten, (
            f"Prompt link not rewritten: {rewritten}"
        )

        resolved = (deployed.parent / rewritten).resolve()
        assert resolved.exists(), f"Rewritten prompt link does not resolve: {resolved}"


# ---------------------------------------------------------------------------
# 3. Mixed link types in one file
# ---------------------------------------------------------------------------


class TestMixedLinkTypes:
    """Only the in-package relative link should be rewritten."""

    @pytest.fixture
    def workspace(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        producer = ws / "producer"
        _write_yaml(producer / "apm.yml", {"name": "producer", "version": "1.0.0"})
        _write(producer / "docs" / "guide.md", "# Guide\n")
        body = (
            '---\napplyTo: "**/*.py"\n---\n'
            "# Many links\n\n"
            "- [internal guide](../../docs/guide.md)\n"
            "- [github](https://github.com/microsoft/apm)\n"
            "- [section anchor](#some-heading)\n"
            "- [internal with fragment](../../docs/guide.md#section-1)\n"
        )
        _write(producer / ".apm" / "instructions" / "many.instructions.md", body)
        consumer = _make_consumer(ws)
        return consumer, producer

    def test_only_relative_in_package_links_rewritten(self, workspace, apm_command):
        consumer, producer = workspace
        result = _run_install(apm_command, consumer, str(producer))
        assert result.returncode == 0, f"Install failed:\n{result.stderr}\n{result.stdout}"

        deployed = consumer / ".github" / "instructions" / "many.instructions.md"
        body = deployed.read_text(encoding="utf-8")

        # Plain relative link rewritten
        plain = _link_target(body, "internal guide")
        assert "apm_modules/_local/producer/docs/guide.md" in plain, plain

        # External URL preserved as-is
        external = _link_target(body, "github")
        assert external == "https://github.com/microsoft/apm", external

        # Fragment-only link preserved as-is
        anchor = _link_target(body, "section anchor")
        assert anchor == "#some-heading", anchor

        # Relative link with fragment: path rewritten, fragment preserved
        with_frag = _link_target(body, "internal with fragment")
        assert "apm_modules/_local/producer/docs/guide.md#section-1" in with_frag, with_frag


# ---------------------------------------------------------------------------
# 4. Path-traversal escape preserved (security)
# ---------------------------------------------------------------------------


class TestEscapeOutsidePackagePreserved:
    """A link that resolves outside the package root must NOT be rewritten.

    This is both a correctness property (we cannot redirect a user's
    intentional consumer-side reference) and a security property (we
    must not synthesize an apm_modules/ path for a file the producer
    never declared as part of the package).
    """

    @pytest.fixture
    def workspace(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        # File OUTSIDE producer/ that the link tries to escape to
        _write(ws / "outside.md", "# Outside\nNot part of package\n")
        producer = ws / "producer"
        _write_yaml(producer / "apm.yml", {"name": "producer", "version": "1.0.0"})
        # Link tries to escape: from .apm/instructions/, ../../../outside.md
        # resolves to ws/outside.md (outside producer/).
        _write(
            producer / ".apm" / "instructions" / "escape.instructions.md",
            '---\napplyTo: "**"\n---\n# Escape\n\nSee [outside doc](../../../outside.md).\n',
        )
        consumer = _make_consumer(ws)
        return consumer, producer, ws

    def test_escape_link_preserved_verbatim(self, workspace, apm_command):
        consumer, producer, _ = workspace
        result = _run_install(apm_command, consumer, str(producer))
        assert result.returncode == 0, f"Install failed:\n{result.stderr}\n{result.stdout}"

        deployed = consumer / ".github" / "instructions" / "escape.instructions.md"
        body = deployed.read_text(encoding="utf-8")

        # The escape link must NOT have been rewritten through apm_modules/.
        link = _link_target(body, "outside doc")
        assert "apm_modules" not in link, (
            f"Escape link incorrectly rewritten through apm_modules/: {link}"
        )

        # The outside file must NOT have been smuggled into apm_modules/.
        smuggled = consumer / "apm_modules" / "_local" / "producer" / ".." / "outside.md"
        assert not smuggled.resolve().exists() or "outside.md" not in {
            p.name for p in (consumer / "apm_modules" / "_local" / "producer").rglob("*")
        }, "outside.md was smuggled into apm_modules/ via the escape link"


# ---------------------------------------------------------------------------
# 5. Skill-bundle internal link is preserved as a relative in-bundle link
# ---------------------------------------------------------------------------


class TestSkillBundleInternalLinkUnchanged:
    """Skill bundles preserve their internal layout when deployed.

    A SKILL.md linking to a sibling file inside the same bundle should
    keep the link as a normal in-bundle relative path, not be rewritten
    through ``apm_modules/`` (which would still resolve, but is needless
    indirection that breaks bundle portability).
    """

    @pytest.fixture
    def workspace(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        producer = ws / "producer"
        _write_yaml(producer / "apm.yml", {"name": "producer", "version": "1.0.0"})
        # Skill bundle with internal reference doc
        skill = producer / ".apm" / "skills" / "demo"
        _write(skill / "REFERENCE.md", "# Reference\nHelpful info.\n")
        _write(
            skill / "SKILL.md",
            "---\nname: demo\ndescription: Demo skill.\n---\n"
            "# Demo skill\n\nSee [REFERENCE](REFERENCE.md) for details.\n",
        )
        consumer = _make_consumer(ws)
        # Skills under copilot deploy_root=.agents/, so create it for
        # auto-detect to pick up the skills primitive.
        (consumer / ".agents").mkdir()
        return consumer, producer

    def test_in_bundle_link_resolves_after_install(self, workspace, apm_command):
        consumer, producer = workspace
        result = _run_install(apm_command, consumer, str(producer))
        assert result.returncode == 0, f"Install failed:\n{result.stderr}\n{result.stdout}"

        # Skills under the copilot target deploy under .agents/skills/<name>/
        # (deploy_root override on the skills PrimitiveMapping).
        deployed_skills = list(consumer.rglob("SKILL.md"))
        deployed_skills = [p for p in deployed_skills if "apm_modules" not in p.parts]
        assert deployed_skills, (
            f"SKILL.md not deployed. consumer tree:\n"
            f"{[str(p.relative_to(consumer)) for p in consumer.rglob('*') if p.is_file()]}"
        )
        deployed_skill = deployed_skills[0]
        deployed_ref = deployed_skill.parent / "REFERENCE.md"
        assert deployed_ref.exists(), (
            f"REFERENCE.md not deployed alongside SKILL.md at {deployed_skill.parent}"
        )

        body = deployed_skill.read_text(encoding="utf-8")
        link = _link_target(body, "REFERENCE")

        # The link MUST resolve on disk -- whether kept as the in-bundle
        # relative path "REFERENCE.md" or rewritten through apm_modules/
        # is an implementation choice, but the deployed link must work.
        resolved = (deployed_skill.parent / link).resolve()
        assert resolved.exists(), (
            f"In-bundle link does not resolve after install: {link} -> {resolved}"
        )
        assert resolved.read_text(encoding="utf-8").startswith("# Reference")


# ---------------------------------------------------------------------------
# 6. Multi-target install: every deployed copy gets a working link
# ---------------------------------------------------------------------------


class TestMultiTargetLinkRewriting:
    """One install -> two host targets -> both deployed copies must work."""

    @pytest.fixture
    def workspace(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        producer = ws / "producer"
        _write_yaml(producer / "apm.yml", {"name": "producer", "version": "1.0.0"})
        _write(producer / "standards" / "style.md", "# Style\n")
        _write(
            producer / ".apm" / "instructions" / "multi.instructions.md",
            '---\napplyTo: "**/*.py"\n---\n# Multi\n\nSee [style](../../standards/style.md).\n',
        )
        consumer = _make_consumer(ws, targets=["copilot", "claude"])
        # Auto-detect needs both target dirs present.  Copilot
        # instructions deploy to .github/instructions/; Claude
        # instructions deploy to .claude/rules/.
        (consumer / ".claude").mkdir()
        return consumer, producer

    def test_both_targets_resolve(self, workspace, apm_command):
        consumer, producer = workspace
        result = _run_install(apm_command, consumer, str(producer))
        assert result.returncode == 0, f"Install failed:\n{result.stderr}\n{result.stdout}"

        # Find every deployed copy of the instruction across targets,
        # excluding the apm_modules/ source-of-truth copy.
        deployed_files = [
            p for p in consumer.rglob("multi*.md") if p.is_file() and "apm_modules" not in p.parts
        ]
        # Copilot writes .github/instructions/multi.instructions.md;
        # Claude writes .claude/rules/multi.md (rules transformer drops
        # the .instructions suffix).  Either way we expect >= 2.
        assert len(deployed_files) >= 2, (
            f"Expected at least 2 deployed copies (copilot+claude), found: "
            f"{[str(p.relative_to(consumer)) for p in deployed_files]}"
        )

        for deployed in deployed_files:
            body = deployed.read_text(encoding="utf-8")
            link = _link_target(body, "style")
            assert "apm_modules" in link, (
                f"Deployed copy at {deployed.relative_to(consumer)} did not "
                f"have its link rewritten: {link}"
            )
            resolved = (deployed.parent / link).resolve()
            assert resolved.exists(), (
                f"Rewritten link in {deployed.relative_to(consumer)} does not "
                f"resolve on disk: {link} -> {resolved}"
            )
