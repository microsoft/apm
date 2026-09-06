"""Exercise the production Unix install phase without network or elevation."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[3] / "install.sh"
pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(sys.platform == "win32", reason="Unix installer filesystem contract"),
]


@pytest.fixture(params=["/bin/bash", "/bin/sh"])
def installation(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Path, dict[str, str]]:
    """Stage only harmless tools, a bundle and an empty home."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in (
        "dirname",
        "readlink",
        "realpath",
        "ls",
        "mkdir",
        "rm",
        "cp",
        "touch",
        "ln",
        "find",
        "id",
        "test",
    ):
        executable = shutil.which(name, path="/usr/bin:/bin")
        assert executable is not None
        (tools / name).symlink_to(executable)
    sudo = tools / "sudo"
    sudo.write_text('#!/bin/sh\nprintf "rejected\\n" >> "$SUDO_LOG"\nexit 97\n', encoding="ascii")
    sudo.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    bundle = tmp_path / "download" / "apm-fixture"
    bundle.mkdir(parents=True)
    binary = bundle / "apm"
    binary.write_text('#!/bin/sh\nprintf "apm fixture\\n"\n', encoding="ascii")
    binary.chmod(0o755)
    (bundle / "VERSION").write_text("new\n", encoding="ascii")
    return tmp_path, {
        "PATH": str(tools),
        "HOME": str(home),
        "LC_ALL": "C",
        "TMP_DIR": str(bundle.parent),
        "EXTRACTED_DIR": bundle.name,
        "SUDO_LOG": str(tmp_path / "sudo.log"),
        "TEST_SHELL": request.param,
    }


def _run(
    installation: tuple[Path, dict[str, str]],
    *,
    defaults: bool = False,
    pip_fallback: bool = False,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    """Run unchanged configuration and installation code from this checkout."""
    root, env = installation
    source = INSTALLER.read_text(encoding="ascii")
    config = source.split("# Banner\n", 1)[0]
    if defaults:
        body = next(line for line in source.splitlines() if line.startswith('APM_LIB_DIR="'))
        body += '\nprintf "%s\\n%s\\n" "$APM_INSTALL_DIR" "$APM_LIB_DIR"\n'
    elif pip_fallback:
        body = source.split("# Function to attempt pip installation\n", 1)[1].split(
            "# Early glibc compatibility check", 1
        )[0]
        # Exercise the same fallback consumer, with its historical path arguments
        # redirected to fixture paths rather than inspecting the host's installs.
        body = body.replace(
            "apm_resolve_install_paths /usr/local/bin/apm /opt/homebrew/bin/apm /usr/local/lib/apm/apm",
            'apm_resolve_install_paths "$HISTORICAL_APM"',
        )
        body += "\ntry_pip_installation\n"
        overrides["HISTORICAL_APM"] = str(root / "usr/local/bin/apm")
    else:
        body = 'apm_resolve_install_paths "$1" "$2" "$3"\n'
        body += source[source.index("# Install binary directory structure\n") :]
    return subprocess.run(
        [
            env["TEST_SHELL"],
            "-c",
            config + body,
            "--",
            str(root / "usr/local/bin/apm"),
            str(root / "opt/homebrew/bin/apm"),
            str(root / "usr/local/lib/apm/apm"),
        ],
        cwd=root,
        env={**env, **overrides},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_fresh_defaults_are_user_owned(installation: tuple[Path, dict[str, str]]) -> None:
    """Unset options must never choose a system destination."""
    root, _ = installation
    result = _run(installation, defaults=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        str(root / "home/.local/bin"),
        str(root / "home/.local/lib/apm"),
    ]


def test_writable_custom_install(installation: tuple[Path, dict[str, str]]) -> None:
    """A custom prefix installs without sudo or shell-profile changes."""
    root, _ = installation
    target = root / "custom/bin"
    result = _run(installation, APM_INSTALL_DIR=str(target))
    assert result.returncode == 0, result.stderr
    assert (target / "apm").resolve() == root / "custom/lib/apm/apm"
    assert (root / "custom/lib/apm/.apm-installed").is_file()
    assert f'export PATH="{target}:$PATH"' in result.stdout
    assert list((root / "home").iterdir()) == []
    assert not (root / "sudo.log").exists()


def _prior(root: Path, prefix: str = "prior", *, legacy: bool = False) -> tuple[Path, Path]:
    """Create a script-managed prior bundle and its relative launcher."""
    bindir = root / prefix / "bin"
    lib = root / prefix / "lib/apm"
    bindir.mkdir(parents=True)
    lib.mkdir(parents=True)
    binary = lib / "apm"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    binary.chmod(0o755)
    (lib / "VERSION").write_text("old\n", encoding="ascii")
    if legacy:
        (lib / "_internal").mkdir()
    else:
        (lib / ".apm-installed").touch()
    (bindir / "apm").symlink_to("../lib/apm/apm")
    return bindir, lib


def _record_tool_calls(root: Path, name: str) -> None:
    """Record subprocess counts while forwarding to the real filesystem tool."""
    tool = root / "tools" / name
    executable = tool.resolve()
    tool.unlink()
    tool.write_text(
        f'#!/bin/sh\nprintf "{name}\\n" >> "$TOOL_LOG"\nexec {shlex.quote(str(executable))} "$@"\n',
        encoding="ascii",
    )
    tool.chmod(0o755)


def test_bundle_preflight_uses_one_linear_scan(installation: tuple[Path, dict[str, str]]) -> None:
    """Growing the prior tree must not repeat traversal or skip directory checks."""
    root, _ = installation
    for tool in ("find", "test"):
        _record_tool_calls(root, tool)
    for width in (10, 100):
        bindir, lib = _prior(root, f"tree-{width}")
        for index in range(width):
            directory = lib / f"directory-{index}"
            directory.mkdir()
            (directory / "file").touch()
        log = root / f"tree-{width}.log"
        result = _run(
            installation,
            APM_INSTALL_DIR=str(bindir),
            APM_LIB_DIR=str(lib),
            TOOL_LOG=str(log),
        )
        assert result.returncode == 0, result.stderr
        calls = log.read_text(encoding="ascii").splitlines()
        assert calls.count("find") == 1
        assert calls.count("test") == 2 * (width + 1)
        assert (lib / "VERSION").read_text(encoding="ascii") == "new\n"


def test_ancestor_walk_does_not_spawn_per_depth(installation: tuple[Path, dict[str, str]]) -> None:
    """Tenfold path depth must not increase dirname process count."""
    root, _ = installation
    _record_tool_calls(root, "dirname")
    counts = []
    for depth in (2, 20):
        target = root / f"depth-{depth}" / Path(*(["nested"] * depth)) / "bin"
        log = root / f"depth-{depth}.log"
        result = _run(installation, APM_INSTALL_DIR=str(target), TOOL_LOG=str(log))
        assert result.returncode == 0, result.stderr
        counts.append(len(log.read_text(encoding="ascii").splitlines()))
        assert (target / "apm").is_file()
    assert counts[0] == counts[1]


def test_fresh_install_uses_defaults(installation: tuple[Path, dict[str, str]]) -> None:
    """Fresh user installation creates only the documented home prefix."""
    root, _ = installation
    result = _run(installation)
    assert result.returncode == 0, result.stderr
    assert (root / "home/.local/bin/apm").resolve() == root / "home/.local/lib/apm/apm"
    assert not (root / "sudo.log").exists()
    assert not list((root / "home").glob(".*rc"))


@pytest.mark.parametrize("prefix", ["prior", "usr/local", "custom path"])
@pytest.mark.parametrize("legacy", [False, True])
def test_existing_install_keeps_original_destinations(
    installation: tuple[Path, dict[str, str]], prefix: str, legacy: bool
) -> None:
    """PATH and historical installs update in place, including pre-marker bundles."""
    root, env = installation
    bindir, lib = _prior(root, prefix, legacy=legacy)
    # A historical system installation must also be found when absent from PATH.
    path = env["PATH"] if prefix == "usr/local" else f"{bindir}:{env['PATH']}"
    result = _run(installation, PATH=path)
    assert result.returncode == 0, result.stderr
    assert (lib / "VERSION").read_text(encoding="ascii") == "new\n"
    assert (bindir / "apm").resolve() == lib / "apm"
    assert not (root / "home/.local").exists()
    assert not (root / "sudo.log").exists()


@pytest.mark.parametrize("option", ["APM_INSTALL_DIR", "APM_LIB_DIR"])
def test_custom_redirection_cannot_shadow_existing_install(
    installation: tuple[Path, dict[str, str]], option: str
) -> None:
    """Explicit preferences do not authorize a second installation."""
    root, env = installation
    bindir, lib = _prior(root)
    result = _run(installation, PATH=f"{bindir}:{env['PATH']}", **{option: str(root / "new/apm")})
    assert result.returncode == 1
    assert "conflict" in result.stderr
    assert (lib / "VERSION").read_text(encoding="ascii") == "old\n"
    assert not (root / "new").exists()


@pytest.mark.parametrize("kind", ["pip", "brew", "directory", "broken"])
def test_unmanaged_launchers_are_never_overwritten(
    installation: tuple[Path, dict[str, str]], kind: str
) -> None:
    """Unknown files, package-manager targets and invalid launchers fail closed."""
    root, _ = installation
    bindir = root / "home/.local/bin"
    bindir.mkdir(parents=True)
    entry = bindir / "apm"
    if kind == "brew":
        _, lib = _prior(root, "opt/homebrew/Cellar/apm/1.0")
        entry.symlink_to(lib / "apm")
    elif kind == "directory":
        entry.mkdir()
    elif kind == "broken":
        entry.symlink_to(root / "missing/apm")
    else:
        entry.write_text("#!/bin/sh\nexit 99\n", encoding="ascii")
        entry.chmod(0o755)
    before = entry.lstat()
    result = _run(installation)
    assert result.returncode == 1
    assert "original installer" in result.stderr or "package manager" in result.stderr
    assert entry.lstat().st_ino == before.st_ino
    assert entry.lstat().st_mode == before.st_mode
    if kind == "pip":
        assert entry.read_text(encoding="ascii") == "#!/bin/sh\nexit 99\n"
    assert not (root / "home/.local/lib").exists()


def test_multiple_prior_bundles_refused(installation: tuple[Path, dict[str, str]]) -> None:
    """An ambiguous PATH never chooses one old installation to overwrite."""
    root, env = installation
    first, first_lib = _prior(root, "first")
    second, second_lib = _prior(root, "second")
    result = _run(installation, PATH=f"{first}:{second}:{env['PATH']}")
    assert result.returncode == 1
    assert "Conflicting APM installations" in result.stderr
    for lib in (first_lib, second_lib):
        assert (lib / "VERSION").read_text(encoding="ascii") == "old\n"


def test_self_update_without_path_cannot_create_shadow(
    installation: tuple[Path, dict[str, str]],
) -> None:
    """An absolute self-update with no discoverable launcher fails actionably."""
    root, _ = installation
    _, lib = _prior(root)
    result = _run(installation, APM_SELF_UPDATE_SOURCE=str(lib / "apm"))
    assert result.returncode == 1
    assert "original destinations" in result.stderr
    assert (lib / "VERSION").read_text(encoding="ascii") == "old\n"
    assert not (root / "home/.local").exists()


def test_missing_self_update_source_does_not_update_other_install(
    installation: tuple[Path, dict[str, str]],
) -> None:
    """A vanished running binary cannot redirect self-update to another installation."""
    root, _ = installation
    _, lib = _prior(root, "usr/local")
    result = _run(installation, APM_SELF_UPDATE_SOURCE=str(root / "missing/apm"))
    assert result.returncode == 1
    assert "running APM installation is missing" in result.stderr
    assert (lib / "VERSION").read_text(encoding="ascii") == "old\n"


def test_historical_bundle_without_launcher_prevents_second_install(
    installation: tuple[Path, dict[str, str]],
) -> None:
    """A missing historical system launcher must not hide its installed bundle."""
    root, _ = installation
    bindir, lib = _prior(root, "usr/local")
    (bindir / "apm").unlink()
    result = _run(installation)
    assert result.returncode == 1
    assert "original destinations" in result.stderr
    assert (lib / "VERSION").read_text(encoding="ascii") == "old\n"
    assert not (root / "home/.local").exists()


@pytest.mark.parametrize("prefix", ["custom", "opt/homebrew/Cellar/apm/1.0"])
def test_configured_off_path_bundle_cannot_create_second_launcher(
    installation: tuple[Path, dict[str, str]], prefix: str
) -> None:
    """An explicit bundle is still subject to identity and package-manager checks."""
    root, _ = installation
    bindir, lib = _prior(root, prefix)
    result = _run(installation, APM_LIB_DIR=str(lib))
    assert result.returncode == 1
    assert "original destinations" in result.stderr or "package manager" in result.stderr
    assert (lib / "VERSION").read_text(encoding="ascii") == "old\n"
    assert (bindir / "apm").resolve() == lib / "apm"
    assert not (root / "home/.local").exists()


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="Real search-permission proof requires an ordinary user",
)
@pytest.mark.parametrize("ancestor", ["lib", "usr"])
def test_inaccessible_historical_bundle_is_not_treated_as_missing(
    installation: tuple[Path, dict[str, str]], ancestor: str
) -> None:
    """A prior bundle hidden behind an unsearchable ancestor cannot be shadowed."""
    root, _ = installation
    bindir, lib = _prior(root, "usr/local")
    (bindir / "apm").unlink()
    blocked = lib if ancestor == "lib" else root / "usr"
    blocked.chmod(0o000)
    try:
        result = _run(installation)
        assert result.returncode == 1
        assert "not searchable" in result.stderr
        assert not (root / "home/.local").exists()
    finally:
        blocked.chmod(0o755)
    assert (lib / "VERSION").read_text(encoding="ascii") == "old\n"


@pytest.mark.parametrize("bindir", ["prefix/apm", "prefix/apm/bin", "prefix"])
def test_overlapping_destinations_refused(
    installation: tuple[Path, dict[str, str]], bindir: str
) -> None:
    """The launcher cannot replace or be deleted with its own bundle directory."""
    root, _ = installation
    result = _run(
        installation, APM_INSTALL_DIR=str(root / bindir), APM_LIB_DIR=str(root / "prefix/apm")
    )
    assert result.returncode == 1
    assert "overlaps" in result.stderr
    assert not (root / "prefix").exists()


def test_self_update_preserves_explicit_custom_bundle(
    installation: tuple[Path, dict[str, str]],
) -> None:
    """Source identity and explicit original destinations permit an in-place update."""
    root, _ = installation
    bindir, lib = _prior(root)
    custom = root / "separate/apm"
    custom.parent.mkdir()
    lib.rename(custom)
    (bindir / "apm").unlink()
    (bindir / "apm").symlink_to(custom / "apm")
    result = _run(
        installation,
        APM_SELF_UPDATE_SOURCE=str(custom / "apm"),
        APM_INSTALL_DIR=str(bindir),
        APM_LIB_DIR=str(custom),
    )
    assert result.returncode == 0, result.stderr
    assert (custom / "VERSION").read_text(encoding="ascii") == "new\n"
    assert not lib.exists()


def test_self_update_environment_routes_through_production_installer(
    installation: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real self-update environment builder drives an existing bundle update."""
    from apm_cli.commands import self_update

    root, env = installation
    bindir, lib = _prior(root)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(lib / "apm"))
    monkeypatch.setattr(
        self_update, "external_process_env", lambda: {**env, "PATH": f"{bindir}:{env['PATH']}"}
    )
    monkeypatch.setattr("apm_cli.config.get_self_update_install_dir", lambda: None)
    monkeypatch.setattr("apm_cli.config.get_self_update_channel", lambda: "stable")
    monkeypatch.delenv("APM_SELF_UPDATE_CHANNEL", raising=False)
    release = self_update._resolve_self_update_release("1.2.3")
    result = _run(installation, **self_update._build_self_update_installer_env(release))
    assert result.returncode == 0, result.stderr
    assert (lib / "VERSION").read_text(encoding="ascii") == "new\n"
    assert not (root / "home/.local").exists()


@pytest.mark.parametrize("explicit", [False, True])
def test_administrator_requires_both_destinations(
    installation: tuple[Path, dict[str, str]], explicit: bool
) -> None:
    """Simulate only root identity; all writes remain ordinary-user fixture writes."""
    root, _ = installation
    identity = root / "tools/id"
    identity.unlink()
    identity.write_text("#!/bin/sh\nprintf '0\\n'\n", encoding="ascii")
    identity.chmod(0o755)
    options = {}
    if explicit:
        options = {
            "APM_INSTALL_DIR": str(root / "usr/local/bin"),
            "APM_LIB_DIR": str(root / "usr/local/lib/apm"),
        }
    result = _run(installation, **options)
    assert result.returncode == (0 if explicit else 1), result.stderr
    if explicit:
        assert (root / "usr/local/bin/apm").is_symlink()
    else:
        assert "requires explicit" in result.stderr
        assert not (root / "home/.local").exists()
    assert not (root / "sudo.log").exists()


def test_existing_install_cannot_fall_back_to_pip(
    installation: tuple[Path, dict[str, str]],
) -> None:
    """The incompatible-binary path cannot install a second pip distribution."""
    root, _ = installation
    _, lib = _prior(root, "usr/local")
    result = _run(installation, pip_fallback=True)
    assert result.returncode == 1
    assert "Pip fallback cannot preserve" in result.stderr
    assert (lib / "VERSION").read_text(encoding="ascii") == "old\n"


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="Real unwritable-directory proof requires an ordinary user",
)
@pytest.mark.parametrize("prior", [False, True])
def test_unwritable_destination_never_elevates(
    installation: tuple[Path, dict[str, str]], prior: bool
) -> None:
    """Actual filesystem permissions must fail without invoking rejecting sudo."""
    root, _ = installation
    prefix = root / "system"
    lib_parent = prefix / "lib"
    lib_parent.mkdir(parents=True)
    lib = lib_parent / "apm"
    if prior:
        lib.mkdir()
        (lib / ".apm-installed").touch()
        (lib / "keep").write_text("old\n", encoding="ascii")
    lib_parent.chmod(0o555)
    try:
        assert not os.access(lib_parent, os.W_OK)
        result = _run(installation, APM_INSTALL_DIR=str(prefix / "bin"))
        assert result.returncode == 1, result.stdout + result.stderr
        assert not (root / "sudo.log").exists()
        assert "not writable" in result.stdout + result.stderr
        if prior:
            assert (lib / "keep").read_text(encoding="ascii") == "old\n"
    finally:
        lib_parent.chmod(0o755)


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="Real permission proof requires an ordinary user",
)
@pytest.mark.parametrize("readonly", ["bin", "nested", "unsearchable"])
def test_preflight_preserves_bundle_when_any_destination_is_unwritable(
    installation: tuple[Path, dict[str, str]], readonly: str
) -> None:
    """A writable lib parent alone does not authorize destructive replacement."""
    root, _ = installation
    bindir, lib = _prior(root)
    directory = bindir if readonly == "bin" else lib / "_internal"
    directory.mkdir(exist_ok=True)
    directory.chmod(0o666 if readonly == "unsearchable" else 0o555)
    try:
        result = _run(installation, APM_INSTALL_DIR=str(bindir), APM_LIB_DIR=str(lib))
        assert result.returncode == 1
        assert "not writable" in result.stderr
        assert (lib / "VERSION").read_text(encoding="ascii") == "old\n"
        assert not (root / "sudo.log").exists()
    finally:
        if directory.exists():
            directory.chmod(0o755)


@pytest.mark.parametrize("unsafe", ["relative/apm", "shared", "symlink", "unmarked"])
def test_original_safety_guards_remain_on_write_path(
    installation: tuple[Path, dict[str, str]], unsafe: str
) -> None:
    """Path/prior-data validation must still protect every destructive write."""
    root, _ = installation
    protected = root / "protected"
    protected.mkdir()
    keep = protected / "keep"
    keep.write_text("unrelated\n", encoding="ascii")
    if unsafe == "relative/apm":
        destination = Path(unsafe)
    elif unsafe == "shared":
        destination = protected
    elif unsafe == "symlink":
        (protected / ".apm-installed").touch()
        destination = root / "apm"
        destination.symlink_to(protected, target_is_directory=True)
    else:
        destination = protected / "apm"
        destination.mkdir()
        (destination / "keep").write_text("unrelated\n", encoding="ascii")
    result = _run(installation, APM_LIB_DIR=str(destination))
    assert result.returncode == 1
    assert keep.read_text(encoding="ascii") == "unrelated\n"
    assert not (root / "sudo.log").exists()
