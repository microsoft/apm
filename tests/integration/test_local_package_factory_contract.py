from pathlib import Path, PurePosixPath

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.local_package import LocalPackageFactory


def test_create_authors_manifest_and_primitives_only(tmp_path: Path) -> None:
    factory = LocalPackageFactory(tmp_path / "packages")
    package = factory.create("source-package", targets=("copilot",))
    skill_content = "# Grill me\n"
    agent_content = "---\ndescription: Test helper\n---\n# Helper\n"
    instruction_content = "---\ndescription: Test rules\n---\n# Rules\n"

    skill = factory.add_skill(package, "grill-me", skill_content)
    agent = factory.add_agent(package, "helper", agent_content)
    instruction = factory.add_instruction(package, "rules", instruction_content)

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

    outside = tmp_path / "outside"
    with pytest.raises(ValueError, match="Unsafe package name"):
        factory.create("../outside")
    assert not outside.exists()

    with pytest.raises(ValueError, match="Unsafe skill name"):
        factory.add_skill(package, "nested/skill", skill_content)
    with pytest.raises(ValueError, match="Unsafe agent name"):
        factory.add_agent(package, "nested/agent", agent_content)
    with pytest.raises(ValueError, match="Unsafe instruction name"):
        factory.add_instruction(package, "nested/instruction", instruction_content)


def test_relative_dependency_uses_portable_manifest_path(tmp_path: Path) -> None:
    factory = LocalPackageFactory(tmp_path / "packages")
    parent = factory.create("consumer")
    child = factory.create("dependency")

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


def test_relative_link_and_policy_are_source_inputs(tmp_path: Path) -> None:
    factory = LocalPackageFactory(tmp_path / "packages")
    package = factory.create("source-package")

    link = factory.add_relative_link(
        package,
        PurePosixPath(".apm/instructions/reference.instructions.md"),
        PurePosixPath("../../README.md"),
        label="reference",
    )
    policy = factory.write_policy(
        package,
        {"name": "strict", "version": "1.0.0", "enforcement": "block"},
    )

    assert link.read_text(encoding="utf-8") == "[reference](../../README.md)\n"
    assert policy == package.root / "apm-policy.yml"
    policy_data = load_yaml(policy)
    assert policy_data is not None
    assert policy_data["enforcement"] == "block"


def test_product_output_paths_are_rejected(tmp_path: Path) -> None:
    factory = LocalPackageFactory(tmp_path / "packages")
    package = factory.create("source-package")

    forbidden_paths = (
        PurePosixPath("apm.lock.yaml"),
        PurePosixPath("apm_modules/source.md"),
        PurePosixPath("build/source.md"),
        PurePosixPath("dist/source.md"),
        PurePosixPath(".apm/cache/source.md"),
    )
    for forbidden_path in forbidden_paths:
        with pytest.raises(ValueError, match="product-generated path"):
            factory.add_relative_link(
                package,
                forbidden_path,
                PurePosixPath("source.md"),
            )

    for forbidden in ("apm.lock.yaml", "apm_modules", "build", "dist", ".apm/cache"):
        assert not (package.root / forbidden).exists()
