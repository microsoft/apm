from pathlib import Path, PurePosixPath

import pytest

from apm_cli.core.errors import UnknownTargetError
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.local_package import LocalPackage, LocalPackageFactory

_GENERATED_OR_DEPLOYED_ROOTS = (
    "apm.lock",
    "apm.lock.yaml",
    "apm_modules",
    "build",
    "dist",
    "AGENTS.md",
    ".agents",
    ".claude",
    ".codex",
    ".cursor",
    ".gemini",
    ".github",
    ".kiro",
    ".opencode",
    ".windsurf",
)


def _assert_source_tree(package_root: Path, expected: set[str]) -> None:
    actual = {path.relative_to(package_root).as_posix() for path in package_root.rglob("*")}
    assert actual == expected
    assert {
        name for name in _GENERATED_OR_DEPLOYED_ROOTS if (package_root / name).exists()
    } == set()


def test_create_authors_manifest_and_primitives_only(tmp_path: Path) -> None:
    factory = LocalPackageFactory(tmp_path / "packages")
    package = factory.create("source-package", targets=("copilot",))
    _assert_source_tree(package.root, {"apm.yml"})
    skill_content = "# Grill me\n"
    agent_content = "---\ndescription: Test helper\n---\n# Helper\n"
    instruction_content = "---\ndescription: Test rules\n---\n# Rules\n"

    skill = factory.add_skill(package, "grill-me", skill_content)
    _assert_source_tree(
        package.root,
        {
            "apm.yml",
            "skills",
            "skills/grill-me",
            "skills/grill-me/SKILL.md",
        },
    )
    agent = factory.add_agent(package, "helper", agent_content)
    _assert_source_tree(
        package.root,
        {
            ".apm",
            ".apm/agents",
            ".apm/agents/helper.agent.md",
            "apm.yml",
            "skills",
            "skills/grill-me",
            "skills/grill-me/SKILL.md",
        },
    )
    instruction = factory.add_instruction(package, "rules", instruction_content)
    _assert_source_tree(
        package.root,
        {
            ".apm",
            ".apm/agents",
            ".apm/agents/helper.agent.md",
            ".apm/instructions",
            ".apm/instructions/rules.instructions.md",
            "apm.yml",
            "skills",
            "skills/grill-me",
            "skills/grill-me/SKILL.md",
        },
    )

    assert skill == package.root / "skills/grill-me/SKILL.md"
    assert agent == package.root / ".apm/agents/helper.agent.md"
    assert instruction == package.root / ".apm/instructions/rules.instructions.md"
    assert skill.read_bytes() == skill_content.encode("utf-8")
    assert agent.read_bytes() == agent_content.encode("utf-8")
    assert instruction.read_bytes() == instruction_content.encode("utf-8")
    assert load_yaml(package.manifest_path) == {
        "name": "source-package",
        "version": "0.1.0",
        "description": "Hermetic test package source-package",
        "author": "APM Test",
        "targets": ["copilot"],
    }

    for unsafe_name in ("", ".", "..", "/", "\\", "../outside", r"..\outside"):
        with pytest.raises(ValueError):
            factory.create(unsafe_name)
    assert not (tmp_path / "outside").exists()

    for unsafe_skill_name in (
        "nested/skill",
        r"..\outside",
        "%2e%2e",
        "%252e%252e",
    ):
        with pytest.raises(ValueError):
            factory.add_skill(package, unsafe_skill_name, skill_content)
    with pytest.raises(ValueError, match="Unsafe agent name"):
        factory.add_agent(package, "nested/agent", agent_content)
    with pytest.raises(ValueError, match="Unsafe instruction name"):
        factory.add_instruction(package, "nested/instruction", instruction_content)

    symlink_package = factory.create("symlink-package")
    outside_primitives = tmp_path / "outside-primitives"
    outside_primitives.mkdir()
    (symlink_package.root / ".apm").symlink_to(
        outside_primitives,
        target_is_directory=True,
    )
    with pytest.raises(ValueError, match=r"outside|symlink"):
        factory.add_instruction(symlink_package, "escaped", instruction_content)
    assert not (outside_primitives / "instructions/escaped.instructions.md").exists()

    with pytest.raises(UnknownTargetError, match="Unknown target"):
        factory.create("invalid-target", targets=("not-a-target",))
    assert not (tmp_path / "packages/invalid-target").exists()


def test_relative_dependency_uses_portable_manifest_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = LocalPackageFactory(tmp_path / "packages")
    parent = factory.create("consumer")
    child = factory.create("dependency")
    _assert_source_tree(parent.root, {"apm.yml"})
    _assert_source_tree(child.root, {"apm.yml"})

    factory.add_relative_dependency(
        parent,
        child,
        alias="dep",
        skills=("grill-me",),
        targets=("copilot",),
    )

    manifest = load_yaml(parent.manifest_path)
    assert manifest is not None
    assert manifest["dependencies"]["apm"] == [
        {
            "path": "../dependency",
            "alias": "dep",
            "skills": ["grill-me"],
            "targets": ["copilot"],
        }
    ]
    _assert_source_tree(parent.root, {"apm.yml"})
    parent_manifest_bytes = parent.manifest_path.read_bytes()

    with pytest.raises(ValueError, match="Invalid alias"):
        factory.add_relative_dependency(parent, child, alias="../dep")
    assert parent.manifest_path.read_bytes() == parent_manifest_bytes
    with pytest.raises(ValueError, match="traversal sequence"):
        factory.add_relative_dependency(parent, child, skills=(r"..\secret",))
    assert parent.manifest_path.read_bytes() == parent_manifest_bytes
    with pytest.raises(ValueError, match="Unknown target"):
        factory.add_relative_dependency(parent, child, targets=("not-a-target",))
    assert parent.manifest_path.read_bytes() == parent_manifest_bytes

    foreign_factory = LocalPackageFactory(tmp_path / "foreign-packages")
    foreign = foreign_factory.create("foreign")
    with pytest.raises(ValueError, match="not owned"):
        factory.add_relative_dependency(parent, foreign)
    assert parent.manifest_path.read_bytes() == parent_manifest_bytes

    with pytest.raises(ValueError, match="Unsupported field"):
        factory.create(
            "lock-shaped",
            dependencies=(
                {
                    "path": "../dependency",
                    "resolved_commit": "a" * 40,
                },
            ),
        )
    assert not (tmp_path / "packages/lock-shaped").exists()

    declared = factory.create(
        "declared",
        dependencies=(
            {
                "path": "../dependency",
                "alias": "dep",
                "skills": ["grill-me"],
                "targets": ["copilot"],
            },
        ),
    )
    assert load_yaml(declared.manifest_path) == {
        "name": "declared",
        "version": "0.1.0",
        "description": "Hermetic test package declared",
        "author": "APM Test",
        "dependencies": {
            "apm": [
                {
                    "path": "../dependency",
                    "alias": "dep",
                    "skills": ["grill-me"],
                    "targets": ["copilot"],
                }
            ]
        },
    }
    _assert_source_tree(declared.root, {"apm.yml"})

    string_declared = factory.create(
        "string-declared",
        dependencies=("microsoft/apm-sample-package#v1.0.0",),
    )
    assert load_yaml(string_declared.manifest_path) == {
        "name": "string-declared",
        "version": "0.1.0",
        "description": "Hermetic test package string-declared",
        "author": "APM Test",
        "dependencies": {
            "apm": ["microsoft/apm-sample-package#v1.0.0"],
        },
    }
    _assert_source_tree(string_declared.root, {"apm.yml"})

    remote_forms = factory.create(
        "remote-forms",
        dependencies=(
            "owner/repo",
            {"git": "owner/repo", "alias": "renamed"},
            {
                "git": "owner/repo",
                "ref": "v1.2.3",
                "alias": "versioned",
            },
            {
                "git": "owner/repo",
                "skills": ["grill-me"],
                "targets": ["copilot"],
            },
        ),
    )
    assert load_yaml(remote_forms.manifest_path) == {
        "name": "remote-forms",
        "version": "0.1.0",
        "description": "Hermetic test package remote-forms",
        "author": "APM Test",
        "dependencies": {
            "apm": [
                "owner/repo",
                {
                    "git": "owner/repo",
                    "alias": "renamed",
                },
                {
                    "git": "owner/repo",
                    "ref": "v1.2.3",
                    "alias": "versioned",
                },
                {
                    "git": "owner/repo",
                    "skills": ["grill-me"],
                    "targets": ["copilot"],
                },
            ]
        },
    }
    _assert_source_tree(remote_forms.root, {"apm.yml"})

    for package_name, extra_field in (
        ("remote-lock-shaped", "resolved_commit"),
        ("remote-typo-shaped", "alais"),
    ):
        with pytest.raises(ValueError, match="Unsupported field"):
            factory.create(
                package_name,
                dependencies=(
                    {
                        "git": "owner/repo",
                        "alias": "renamed",
                        extra_field: "unexpected",
                    },
                ),
            )
        assert not (tmp_path / "packages" / package_name).exists()

    with pytest.raises(TypeError, match="strings or mappings"):
        factory.create("invalid-dependency-type", dependencies=(42,))
    assert not (tmp_path / "packages/invalid-dependency-type").exists()

    minimal = factory.create("minimal")
    factory.add_relative_dependency(minimal, child)
    minimal_manifest = load_yaml(minimal.manifest_path)
    assert minimal_manifest is not None
    assert minimal_manifest["dependencies"]["apm"] == [{"path": "../dependency"}]
    assert set(minimal_manifest["dependencies"]["apm"][0]) == {"path"}
    _assert_source_tree(minimal.root, {"apm.yml"})

    windows_parent = factory.create("windows-parent")
    monkeypatch.setattr(
        "tests.utils.local_package.os.path.relpath",
        lambda *_args: r"..\dependency",
    )
    factory.add_relative_dependency(windows_parent, child)
    windows_manifest = load_yaml(windows_parent.manifest_path)
    assert windows_manifest is not None
    assert windows_manifest["dependencies"]["apm"] == [{"path": "../dependency"}]
    _assert_source_tree(windows_parent.root, {"apm.yml"})

    malformed = factory.create("malformed")
    dump_yaml(
        {
            "name": "malformed",
            "dependencies": [],
        },
        malformed.manifest_path,
    )
    malformed_bytes = malformed.manifest_path.read_bytes()
    with pytest.raises(ValueError, match="Invalid dependencies mapping"):
        factory.add_relative_dependency(malformed, child)
    assert malformed.manifest_path.read_bytes() == malformed_bytes

    malformed_yaml = factory.create("malformed-yaml")
    malformed_yaml.manifest_path.write_bytes(b"dependencies: [\n")
    malformed_yaml_bytes = malformed_yaml.manifest_path.read_bytes()
    with pytest.raises(ValueError, match="Invalid manifest YAML"):
        factory.add_relative_dependency(malformed_yaml, child)
    assert malformed_yaml.manifest_path.read_bytes() == malformed_yaml_bytes

    malformed_apm = factory.create("malformed-apm")
    dump_yaml(
        {
            "name": "malformed-apm",
            "dependencies": {"apm": "../dependency"},
        },
        malformed_apm.manifest_path,
    )
    malformed_apm_bytes = malformed_apm.manifest_path.read_bytes()
    with pytest.raises(ValueError, match="Invalid APM dependencies list"):
        factory.add_relative_dependency(malformed_apm, child)
    assert malformed_apm.manifest_path.read_bytes() == malformed_apm_bytes

    outside_manifest = tmp_path / "outside-manifest.yml"
    outside_manifest.write_text("name: outside\n", encoding="utf-8")
    parent.manifest_path.unlink()
    parent.manifest_path.symlink_to(outside_manifest)
    with pytest.raises(ValueError, match=r"outside|symlink"):
        factory.add_relative_dependency(parent, child)
    assert outside_manifest.read_text(encoding="utf-8") == "name: outside\n"


def test_relative_link_and_policy_are_source_inputs(tmp_path: Path) -> None:
    factory = LocalPackageFactory(tmp_path / "packages")
    package = factory.create("source-package")
    _assert_source_tree(package.root, {"apm.yml"})

    link = factory.add_relative_link(
        package,
        PurePosixPath(".apm/instructions/reference.instructions.md"),
        PurePosixPath("../../README.md"),
        label="reference",
    )
    assert link == package.root / ".apm/instructions/reference.instructions.md"
    _assert_source_tree(
        package.root,
        {
            ".apm",
            ".apm/instructions",
            ".apm/instructions/reference.instructions.md",
            "apm.yml",
        },
    )
    factory.add_skill(package, "grill-me", "# Grill me\n")
    nested_link = factory.add_relative_link(
        package,
        PurePosixPath("skills/grill-me/references/guide.md"),
        PurePosixPath("../assets/example.txt"),
        label="guide",
    )
    assert nested_link == package.root / "skills/grill-me/references/guide.md"
    assert nested_link.read_bytes() == b"[guide](../assets/example.txt)\n"
    _assert_source_tree(
        package.root,
        {
            ".apm",
            ".apm/instructions",
            ".apm/instructions/reference.instructions.md",
            "apm.yml",
            "skills",
            "skills/grill-me",
            "skills/grill-me/SKILL.md",
            "skills/grill-me/references",
            "skills/grill-me/references/guide.md",
        },
    )
    policy = factory.write_policy(
        package,
        {"name": "strict", "version": "1.0.0", "enforcement": "block"},
    )

    assert link.read_text(encoding="utf-8") == "[reference](../../README.md)\n"
    assert policy == package.root / "apm-policy.yml"
    assert load_yaml(policy) == {
        "name": "strict",
        "version": "1.0.0",
        "enforcement": "block",
    }
    _assert_source_tree(
        package.root,
        {
            ".apm",
            ".apm/instructions",
            ".apm/instructions/reference.instructions.md",
            "apm-policy.yml",
            "apm.yml",
            "skills",
            "skills/grill-me",
            "skills/grill-me/SKILL.md",
            "skills/grill-me/references",
            "skills/grill-me/references/guide.md",
        },
    )

    foreign_factory = LocalPackageFactory(tmp_path / "foreign-packages")
    foreign = foreign_factory.create("foreign")
    with pytest.raises(ValueError, match="not owned"):
        factory.write_policy(foreign, {"name": "strict"})
    assert not (foreign.root / "apm-policy.yml").exists()

    forged = LocalPackage(
        name=package.name,
        root=package.root,
        manifest_path=package.manifest_path,
    )
    with pytest.raises(ValueError, match="not owned"):
        factory.write_policy(forged, {"name": "strict"})
    assert load_yaml(policy) == {
        "name": "strict",
        "version": "1.0.0",
        "enforcement": "block",
    }
    rejected_link = package.root / ".apm/instructions/rejected.instructions.md"
    for unsafe_target in (
        PurePosixPath("/absolute/README.md"),
        PurePosixPath(r"..\README.md"),
        PurePosixPath(r"C:\repo\README.md"),
        PurePosixPath("C:/repo/README.md"),
        PurePosixPath(r"\\server\share\README.md"),
        PurePosixPath("//server/share/README.md"),
    ):
        with pytest.raises(ValueError, match="relative POSIX"):
            factory.add_relative_link(
                package,
                PurePosixPath(".apm/instructions/rejected.instructions.md"),
                unsafe_target,
            )
        assert not rejected_link.exists()
        assert link.read_bytes() == b"[reference](../../README.md)\n"

    symlink_package = factory.create("symlink-policy")
    outside_policy = tmp_path / "outside-policy.yml"
    outside_policy.write_text("name: outside\n", encoding="utf-8")
    (symlink_package.root / "apm-policy.yml").symlink_to(outside_policy)
    with pytest.raises(ValueError, match=r"outside|symlink"):
        factory.write_policy(symlink_package, {"name": "strict"})
    assert outside_policy.read_text(encoding="utf-8") == "name: outside\n"

    symlink_skill = factory.create("symlink-skill")
    factory.add_skill(symlink_skill, "linked", "# Linked\n")
    outside_skill = tmp_path / "outside-skill"
    outside_skill.mkdir()
    (symlink_skill.root / "skills/linked/references").symlink_to(
        outside_skill,
        target_is_directory=True,
    )
    with pytest.raises(ValueError, match=r"outside|symlink"):
        factory.add_relative_link(
            symlink_skill,
            PurePosixPath("skills/linked/references/escaped.md"),
            PurePosixPath("../assets/example.txt"),
        )
    assert not (outside_skill / "escaped.md").exists()

    in_package_symlink = factory.create("in-package-symlink")
    factory.add_skill(in_package_symlink, "linked", "# Linked\n")
    _assert_source_tree(
        in_package_symlink.root,
        {
            "apm.yml",
            "skills",
            "skills/linked",
            "skills/linked/SKILL.md",
        },
    )
    real_references = in_package_symlink.root / "skills/linked/references"
    real_references.mkdir()
    (in_package_symlink.root / "skills/linked/reference-alias").symlink_to(
        real_references,
        target_is_directory=True,
    )
    with pytest.raises(ValueError, match="symlink"):
        factory.add_relative_link(
            in_package_symlink,
            PurePosixPath("skills/linked/reference-alias/escaped.md"),
            PurePosixPath("../assets/example.txt"),
        )
    assert not (real_references / "escaped.md").exists()

    for unsafe_path in (
        PurePosixPath("."),
        PurePosixPath("../README.md"),
        PurePosixPath(r"..\README.md"),
        PurePosixPath("%2e%2e/README.md"),
    ):
        with pytest.raises(ValueError, match="traversal sequence"):
            factory.add_relative_link(
                package,
                unsafe_path,
                PurePosixPath("source.md"),
            )


def test_product_output_paths_are_rejected(tmp_path: Path) -> None:
    factory = LocalPackageFactory(tmp_path / "packages")
    package = factory.create("source-package")

    forbidden_paths = (
        PurePosixPath("apm.lock"),
        PurePosixPath("apm.lock.yaml"),
        PurePosixPath("apm_modules/source.md"),
        PurePosixPath("build/source.md"),
        PurePosixPath("dist/source.md"),
        PurePosixPath(".apm/cache/source.md"),
        PurePosixPath(".apm/skills/example/SKILL.md"),
        PurePosixPath(".apm/agents/helper/references/guide.agent.md"),
        PurePosixPath(".apm/instructions/rules/references/guide.instructions.md"),
        PurePosixPath("AGENTS.md"),
        PurePosixPath("README.md"),
        PurePosixPath(".agents/generated.md"),
        PurePosixPath("bundle/output.md"),
        PurePosixPath("cache/data.json"),
        PurePosixPath("locks/apm.lock.yaml"),
        PurePosixPath(".git/config"),
        PurePosixPath("skills/example"),
        PurePosixPath("skills/example/.git/config"),
        PurePosixPath(r"skills/example/references\guide.md"),
        PurePosixPath(".github/copilot-instructions.md"),
        PurePosixPath(".claude/agents/generated.md"),
    )
    for forbidden_path in forbidden_paths:
        with pytest.raises(ValueError, match="unsupported package source layout"):
            factory.add_relative_link(
                package,
                forbidden_path,
                PurePosixPath("source.md"),
            )
        assert not package.root.joinpath(*forbidden_path.parts).exists()

    for forbidden in (
        "apm.lock",
        "apm.lock.yaml",
        "apm_modules",
        "build",
        "dist",
        ".apm/cache",
        ".apm/skills",
        "AGENTS.md",
        ".agents",
        "bundle",
        "cache",
        "locks",
        ".git",
        ".github",
        ".claude",
    ):
        assert not (package.root / forbidden).exists()
