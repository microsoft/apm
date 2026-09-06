"""Exercise the production Unix install phase without network or elevation."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

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
        "grep",
        "sort",
        "head",
        "tr",
        "uname",
        "mktemp",
        "sh",
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
    bootstrap: bool = False,
    pip_fallback: bool = False,
    failure_stage: Literal["binary", "glibc"] | None = None,
    script_args: tuple[str, ...] = (),
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    """Run unchanged configuration and installation code from this checkout."""
    root, env = installation
    source = INSTALLER.read_text(encoding="ascii")
    config = source.split("# Banner\n", 1)[0]
    if defaults:
        body = next(line for line in source.splitlines() if line.startswith('APM_LIB_DIR="'))
        body += '\nprintf "%s\\n%s\\n" "$APM_INSTALL_DIR" "$APM_LIB_DIR"\n'
    elif bootstrap:
        config = ""
        body = source.replace(
            "apm_resolve_install_paths /usr/local/bin/apm /opt/homebrew/bin/apm /usr/local/lib/apm/apm",
            'apm_resolve_install_paths "$HISTORICAL_APM"',
        )
        overrides["HISTORICAL_APM"] = str(root / "usr/local/bin/apm")
    elif pip_fallback or failure_stage is not None:
        support = source.split("is_truthy() {", 1)[1].split(
            "# Function to check Python availability and version\n", 1
        )[0]
        body = "is_truthy() {" + support
        body += source.split("# Function to check Python availability and version\n", 1)[1]
        body = body.split("# Early glibc compatibility check", 1)[0]
        if failure_stage == "binary":
            body += (
                "\nBINARY_TEST_EXIT_CODE=1\nBINARY_TEST_OUTPUT='fixture binary failure'\n"
                "GLIBC_VERSION=''\nPLATFORM=darwin\n"
            )
            failure_branch = source.index("if [ $BINARY_TEST_EXIT_CODE -eq 0 ]; then")
            body += source[failure_branch:].split("# Resolve before either installation path", 1)[0]
        elif failure_stage == "glibc":
            body += "\nPLATFORM=linux\n"
            body += source.split("# Early glibc compatibility check for Linux\n", 1)[1].split(
                "# Detect if running in a container", 1
            )[0]
        else:
            body += "\ntry_pip_installation\n"
        # Exercise the same fallback consumer, with its historical path arguments
        # redirected to fixture paths rather than inspecting the host's installs.
        body = body.replace(
            "apm_resolve_install_paths /usr/local/bin/apm /opt/homebrew/bin/apm /usr/local/lib/apm/apm",
            'apm_resolve_install_paths "$HISTORICAL_APM"',
        )
        overrides["HISTORICAL_APM"] = str(root / "usr/local/bin/apm")
    else:
        if script_args:
            body = 'apm_parse_installer_args "$@"\napm_resolve_install_paths "$HISTORICAL_APM"\n'
            overrides["HISTORICAL_APM"] = str(root / "usr/local/bin/apm")
        else:
            body = 'apm_resolve_install_paths "$1" "$2" "$3"\n'
        body += source[source.index("# Install binary directory structure\n") :]
    args = (
        list(script_args)
        if script_args
        else [
            str(root / "usr/local/bin/apm"),
            str(root / "opt/homebrew/bin/apm"),
            str(root / "usr/local/lib/apm/apm"),
        ]
    )
    return subprocess.run(
        [
            env["TEST_SHELL"],
            "-c",
            config + body,
            "--",
            *args,
        ],
        cwd=root,
        env={**env, **overrides},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _run_arg_probe(
    installation: tuple[Path, dict[str, str]], *args: str, **overrides: str
) -> subprocess.CompletedProcess[str]:
    """Exercise the production argument parser and path owner without writes."""
    root, env = installation
    source = INSTALLER.read_text(encoding="ascii")
    config = source.split("# Banner\n", 1)[0]
    body = (
        'apm_parse_installer_args "$@"\n'
        'apm_resolve_install_paths "$HISTORICAL_APM"\n'
        'printf "VERSION=%s\\nAPM_INSTALL_DIR=%s\\nAPM_LIB_DIR=%s\\n" '
        '"$VERSION" "$APM_INSTALL_DIR" "$APM_LIB_DIR"\n'
    )
    return subprocess.run(
        [env["TEST_SHELL"], "-c", config + body, "--", *args],
        cwd=root,
        env={
            **env,
            "HISTORICAL_APM": str(root / "usr/local/bin/apm"),
            **overrides,
        },
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


@pytest.mark.parametrize("syntax", ["split", "equals"])
def test_prefix_derives_launcher_and_bundle_destinations(
    installation: tuple[Path, dict[str, str]], syntax: str
) -> None:
    """--prefix is the single explicit selector for both Unix destinations."""
    root, _ = installation
    prefix = root / "selected prefix"
    args = ("--prefix", str(prefix)) if syntax == "split" else (f"--prefix={prefix}",)
    result = _run(installation, script_args=args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (prefix / "bin/apm").resolve() == prefix / "lib/apm/apm"
    assert (prefix / "lib/apm/.apm-installed").is_file()
    assert not (root / "home/.local").exists()
    assert not (root / "sudo.log").exists()


@pytest.mark.parametrize(
    ("args", "expected_version"),
    [
        (("@v1.2.3", "--prefix", "PREFIX"), "v1.2.3"),
        (("--prefix=PREFIX", "@v1.2.3"), "v1.2.3"),
        (("1.2.3", "--prefix", "PREFIX"), "1.2.3"),
    ],
)
def test_prefix_and_version_arguments_are_order_independent(
    installation: tuple[Path, dict[str, str]], args: tuple[str, ...], expected_version: str
) -> None:
    """The new option must not steal the historical positional version."""
    root, _ = installation
    prefix = root / "order"
    expanded = tuple(
        str(prefix) if arg == "PREFIX" else arg.replace("PREFIX", str(prefix)) for arg in args
    )
    result = _run_arg_probe(installation, *expanded)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"VERSION={expected_version}",
        f"APM_INSTALL_DIR={prefix / 'bin'}",
        f"APM_LIB_DIR={prefix / 'lib/apm'}",
    ]


def test_prefix_counts_as_both_root_destinations(installation: tuple[Path, dict[str, str]]) -> None:
    """Root identity accepts --prefix because it explicitly selects both paths."""
    root, _ = installation
    identity = root / "tools/id"
    identity.unlink()
    identity.write_text("#!/bin/sh\nprintf '0\\n'\n", encoding="ascii")
    identity.chmod(0o755)
    prefix = root / "usr/local"
    result = _run(installation, script_args=("--prefix", str(prefix)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (prefix / "bin/apm").is_symlink()
    assert (prefix / "bin/apm").resolve() == prefix / "lib/apm/apm"
    assert not (root / "home/.local").exists()
    assert not (root / "sudo.log").exists()


@pytest.mark.parametrize(
    "args",
    [
        ("--prefix",),
        ("--prefix", ""),
        ("--prefix", "--help"),
        ("--prefix=",),
        ("--prefix", "relative"),
        ("--prefix", "../escape"),
        ("--prefix", "/opt/../apm"),
        ("--prefix", "/opt/apm", "--prefix", "/other"),
        ("--unknown",),
        ("@not-a-version",),
        ("not-a-version",),
    ],
)
def test_prefix_argument_errors_are_explicit_and_preflighted(
    installation: tuple[Path, dict[str, str]], args: tuple[str, ...]
) -> None:
    """Malformed command-line input fails before creating destinations."""
    root, _ = installation
    result = _run_arg_probe(installation, *args)
    assert result.returncode == 1
    assert "[x]" in result.stderr
    assert not (root / "home/.local").exists()
    assert not (root / "opt").exists()


@pytest.mark.parametrize(
    ("extra_env", "accepted"),
    [
        ({"APM_INSTALL_DIR": "PREFIX/bin"}, True),
        ({"APM_LIB_DIR": "PREFIX/lib/apm"}, True),
        ({"APM_INSTALL_DIR": "PREFIX/bin", "APM_LIB_DIR": "PREFIX/lib/apm"}, True),
        ({"APM_INSTALL_DIR": "PREFIX/other-bin"}, False),
        ({"APM_LIB_DIR": "PREFIX/other-lib/apm"}, False),
    ],
)
def test_prefix_redundant_destinations_must_match(
    installation: tuple[Path, dict[str, str]], extra_env: dict[str, str], accepted: bool
) -> None:
    """Prefix plus env destinations is accepted only when exactly redundant."""
    root, _ = installation
    prefix = root / "matching"
    env = {key: value.replace("PREFIX", str(prefix)) for key, value in extra_env.items()}
    result = _run_arg_probe(installation, "--prefix", str(prefix), **env)
    assert result.returncode == (0 if accepted else 1)
    if accepted:
        assert f"APM_INSTALL_DIR={prefix / 'bin'}" in result.stdout
        assert f"APM_LIB_DIR={prefix / 'lib/apm'}" in result.stdout
    else:
        assert "Use one destination selector" in result.stderr
        assert not (root / "matching").exists()


def test_prefix_prints_shell_safe_path_guidance(installation: tuple[Path, dict[str, str]]) -> None:
    """PATH handoff for a prefixed install preserves spaces and quotes."""
    root, env = installation
    prefix = root / "quote ' prefix"
    result = _run(installation, script_args=("--prefix", str(prefix)))
    assert result.returncode == 0, result.stdout + result.stderr
    instruction = next(
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith("export PATH=")
    )
    executed = subprocess.run(
        ["/bin/sh", "-c", instruction + '\nprintf "%s\\n" "$PATH"'],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert executed.returncode == 0, executed.stderr
    assert executed.stdout == f"{prefix / 'bin'}:{env['PATH']}\n"
    assert "No shell profiles were changed." in result.stdout


def test_prefix_does_not_migrate_existing_installation(
    installation: tuple[Path, dict[str, str]],
) -> None:
    """A new prefix cannot redirect an owner-managed existing installation."""
    root, env = installation
    bindir, lib = _prior(root)
    prefix = root / "new-prefix"
    result = _run(
        installation,
        PATH=f"{bindir}:{env['PATH']}",
        script_args=("--prefix", str(prefix)),
    )
    assert result.returncode == 1
    assert "conflict with existing APM" in result.stderr
    assert (lib / "VERSION").read_text(encoding="ascii") == "old\n"
    assert not prefix.exists()
    assert not (root / "sudo.log").exists()


def test_help_documents_prefix_without_resolving_destinations(
    installation: tuple[Path, dict[str, str]],
) -> None:
    """User-visible help names the derived paths and privilege model."""
    result = _run_arg_probe(installation, "--help")
    assert result.returncode == 0
    assert "--prefix PATH" in result.stdout
    assert "PATH/bin" in result.stdout
    assert "PATH/lib/apm" in result.stdout
    assert "never runs sudo" in result.stdout


@pytest.mark.parametrize(
    "invalid",
    ["root", "relative", "dot-segment", "overlap", "prefix-relative", "prefix-conflict"],
)
def test_invalid_bootstrap_request_never_fetches_or_extracts(
    installation: tuple[Path, dict[str, str]], invalid: str
) -> None:
    """The complete bootstrap must reject local errors before external work."""
    root, _ = installation
    for name in ("curl", "tar", "ldd"):
        tool = root / "tools" / name
        tool.write_text(
            f'#!/bin/sh\nprintf "{name}\\n" >> "$OPERATION_LOG"\nexit 97\n',
            encoding="ascii",
        )
        tool.chmod(0o755)
    uname = root / "tools/uname"
    uname.unlink()
    uname.write_text(
        '#!/bin/sh\ncase "$1" in -s) printf "Linux\\n";; -m) printf "x86_64\\n";; esac\n',
        encoding="ascii",
    )
    uname.chmod(0o755)
    options = {}
    if invalid == "root":
        identity = root / "tools/id"
        identity.unlink()
        identity.write_text("#!/bin/sh\nprintf '0\\n'\n", encoding="ascii")
        identity.chmod(0o755)
    elif invalid == "relative":
        options["APM_INSTALL_DIR"] = "relative/bin"
    elif invalid == "dot-segment":
        options["APM_INSTALL_DIR"] = str(root / "invalid/../bin")
    elif invalid == "prefix-relative":
        options["script_args"] = ("--prefix", "relative")
    elif invalid == "prefix-conflict":
        options["script_args"] = ("--prefix", str(root / "conflict-prefix"))
        options["APM_INSTALL_DIR"] = str(root / "conflict-prefix/other-bin")
    else:
        options["APM_INSTALL_DIR"] = str(root / "nested/apm/bin")
        options["APM_LIB_DIR"] = str(root / "nested/apm")
    log = root / "external-operations.log"
    result = _run(
        installation,
        bootstrap=True,
        VERSION="v1.2.3",
        TMPDIR=str(root),
        OPERATION_LOG=str(log),
        **options,
    )
    assert result.returncode == 1
    assert not log.exists()
    assert "[x]" in result.stderr
    assert not (root / "home/.local").exists()


def test_writable_custom_install(installation: tuple[Path, dict[str, str]]) -> None:
    """A custom prefix installs without sudo or shell-profile changes."""
    root, _ = installation
    target = root / "custom/bin"
    result = _run(installation, APM_INSTALL_DIR=str(target))
    assert result.returncode == 0, result.stderr
    assert (target / "apm").resolve() == root / "custom/lib/apm/apm"
    assert (root / "custom/lib/apm/.apm-installed").is_file()
    assert f"export PATH='{target}':\"$PATH\"" in result.stdout
    assert list((root / "home").iterdir()) == []
    assert not (root / "sudo.log").exists()


@pytest.mark.parametrize(
    "name",
    [
        "plain",
        "space dir",
        'double"quote',
        "single'quote",
        "dollar$literal",
        "bin$(touch injected)",
        "bin`touch injected`",
        "colon:dir",
        "line\nbreak",
    ],
)
def test_native_path_instruction_preserves_literal_destination(
    installation: tuple[Path, dict[str, str]], name: str
) -> None:
    """Execute printed guidance; quotes and substitutions must remain path data."""
    root, env = installation
    target = root / name / "bin"
    result = _run(installation, APM_INSTALL_DIR=str(target))
    assert result.returncode == 0, result.stdout + result.stderr
    if ":" in name or "\n" in name:
        assert "export PATH=" not in result.stdout
        instruction = (
            result.stdout.split("Run APM using its absolute path:\n", 1)[1]
            .split("\nFor PATH discovery,", 1)[0]
            .strip()
        )
        expected = "apm fixture\n"
    else:
        instruction = next(
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith("export PATH=")
        )
        instruction += '\nprintf "%s\\n" "$PATH"\n'
        expected = f"{target}:{env['PATH']}\n"
    executed = subprocess.run(
        ["/bin/sh", "-c", instruction],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert executed.returncode == 0, executed.stderr
    assert executed.stdout == expected
    assert not (root / "injected").exists()
    assert not list((root / "home").iterdir())
    assert "No shell profiles were changed." in result.stdout


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


def _stage_fake_python_pip(
    root: Path,
    *,
    python_name: str = "python3",
    query_exit: int = 0,
    install_launcher: bool = False,
) -> tuple[Path, Path]:
    """Stage a selected Python and harmless pip module for fallback tests."""
    fake_modules = root / "python-modules"
    pip = fake_modules / "pip"
    pip.mkdir(parents=True)
    (pip / "__init__.py").touch()
    install_body = ""
    if install_launcher:
        install_body = (
            "    scripts = Path(os.environ['PYTHONUSERBASE']) / 'bin'\n"
            "    scripts.mkdir(parents=True, exist_ok=True)\n"
            "    launcher = scripts / 'apm'\n"
            "    launcher.write_text('#!/bin/sh\\nprintf \"pip fixture\\\\n\"\\n', encoding='ascii')\n"
            "    launcher.chmod(0o755)\n"
        )
    (pip / "__main__.py").write_text(
        "import os, sys\nfrom pathlib import Path\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('pip fixture')\n"
        "else:\n"
        "    Path(os.environ['PIP_LOG']).write_text('\\n'.join(sys.argv[1:]), encoding='ascii')\n"
        f"{install_body}",
        encoding="ascii",
    )
    python = root / "tools" / python_name
    python.write_text(
        "#!/bin/sh\n"
        f'case "$*" in *get_preferred_scheme*) [ {query_exit} -eq 0 ] || exit {query_exit};; esac\n'
        f'exec {shlex.quote(sys.executable)} "$@"\n',
        encoding="ascii",
    )
    python.chmod(0o755)
    return fake_modules, python


def test_bundle_preflight_uses_one_linear_scan(installation: tuple[Path, dict[str, str]]) -> None:
    """Growing the prior tree must not repeat traversal or skip directory checks."""
    root, _ = installation
    for tool in ("find", "test", "sh"):
        _record_tool_calls(root, tool)
    for width in (10, 100):
        bindir, lib = _prior(root, f"tree-{width}")
        for index in range(width):
            directory = lib / f"directory-{index} with spaces\nand newline"
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
        assert calls.count("test") == 0
        assert calls.count("sh") == 1
        assert (lib / "VERSION").read_text(encoding="ascii") == "new\n"


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="Real permission proof requires an ordinary user",
)
@pytest.mark.parametrize("mode", [0o555, 0o666])
def test_batched_permissions_preserve_late_unmanageable_directory(
    installation: tuple[Path, dict[str, str]], mode: int
) -> None:
    """A late, unusually named directory must retain both permission checks."""
    root, _ = installation
    bindir, lib = _prior(root)
    for index in range(100):
        (lib / f"directory-{index}").mkdir()
    blocked = lib / "last directory\nwith spaces"
    blocked.mkdir()
    blocked.chmod(mode)
    try:
        result = _run(installation, APM_INSTALL_DIR=str(bindir), APM_LIB_DIR=str(lib))
        assert result.returncode == 1
        assert (lib / "VERSION").read_bytes() == b"old\n"
        assert not (root / "sudo.log").exists()
    finally:
        if blocked.exists():
            blocked.chmod(0o755)


def test_permission_batch_failure_preserves_bundle(
    installation: tuple[Path, dict[str, str]],
) -> None:
    """A failed permission worker cannot look like a successful empty scan."""
    root, _ = installation
    bindir, lib = _prior(root)
    worker = root / "tools/sh"
    worker.unlink()
    worker.write_text("#!/bin/sh\nexit 57\n", encoding="ascii")
    worker.chmod(0o755)
    result = _run(installation, APM_INSTALL_DIR=str(bindir), APM_LIB_DIR=str(lib))
    assert result.returncode == 1
    assert (lib / "VERSION").read_bytes() == b"old\n"
    assert not (root / "sudo.log").exists()


def test_recognized_symlink_bundle_preserves_original_destinations(
    installation: tuple[Path, dict[str, str]],
) -> None:
    """Recognized identity does not authorize replacing a symlinked bundle."""
    root, _ = installation
    bindir, lib = _prior(root)
    alias = root / "apm"
    alias.symlink_to(lib, target_is_directory=True)
    result = _run(installation, APM_INSTALL_DIR=str(bindir), APM_LIB_DIR=str(alias))
    assert result.returncode == 1
    assert "APM_LIB_DIR is a symlink" in result.stderr
    assert alias.is_symlink()
    assert (lib / "VERSION").read_bytes() == b"old\n"
    assert (bindir / "apm").resolve() == lib / "apm"
    assert not (root / "sudo.log").exists()


def test_bundle_scan_does_not_follow_external_symlink(
    installation: tuple[Path, dict[str, str]],
) -> None:
    """Replacing an owned bundle never traverses or removes a symlink's target."""
    root, _ = installation
    bindir, lib = _prior(root)
    external = root / "external"
    external.mkdir()
    keep = external / "unrelated"
    keep.write_bytes(b"external\n")
    (lib / "linked-directory").symlink_to(external, target_is_directory=True)
    external.chmod(0o555)
    try:
        result = _run(installation, APM_INSTALL_DIR=str(bindir), APM_LIB_DIR=str(lib))
        assert result.returncode == 0, result.stderr
        assert keep.read_bytes() == b"external\n"
        assert (lib / "VERSION").read_bytes() == b"new\n"
        assert not (root / "sudo.log").exists()
    finally:
        external.chmod(0o755)


@pytest.mark.parametrize("marker", [None, "VERSION", ".apm-installed", "apm.cmd", "apm"])
def test_unrecognized_bundle_data_is_preserved(
    installation: tuple[Path, dict[str, str]], marker: str | None
) -> None:
    """A generic file is not permission to recursively delete unrelated data."""
    root, _ = installation
    target = root / "home/.local/lib/apm"
    target.mkdir(parents=True)
    payload = b"unrelated\x00bytes\n"
    keep = target / "unrelated-data"
    keep.write_bytes(payload)
    if marker is not None:
        (target / marker).write_text("unrelated-v1\n", encoding="ascii")
    result = _run(installation)
    assert keep.read_bytes() == payload
    assert result.returncode == 1, result.stdout + result.stderr
    assert "rm -rf" not in result.stdout + result.stderr
    if marker != "apm":
        assert "Inspect this directory" in result.stdout
    assert not (root / "home/.local/bin/apm").exists()
    assert not (root / "sudo.log").exists()


def test_symlinked_bundle_executable_preserves_external_data(
    installation: tuple[Path, dict[str, str]],
) -> None:
    """A symlinked executable is not bundle identity, even with real markers."""
    root, _ = installation
    bindir, lib = _prior(root)
    external = root / "external-apm"
    (lib / "apm").rename(external)
    (lib / "apm").symlink_to(external)
    keep = lib / "unrelated-data"
    keep.write_bytes(b"unrelated\n")
    before = external.read_bytes()
    launcher_inode = (bindir / "apm").lstat().st_ino
    result = _run(installation, APM_INSTALL_DIR=str(bindir), APM_LIB_DIR=str(lib))
    assert result.returncode == 1
    assert keep.read_bytes() == b"unrelated\n"
    assert external.read_bytes() == before
    assert (bindir / "apm").lstat().st_ino == launcher_inode
    assert (lib / "apm").is_symlink()
    assert not (root / "sudo.log").exists()
    source = INSTALLER.read_text(encoding="ascii")
    identity = source.split("# INSTALL_OWNERSHIP_BEGIN", 1)[1].split("# INSTALL_OWNERSHIP_END", 1)[
        0
    ]
    probe = subprocess.run(
        ["/bin/sh", "-c", identity + '\napm_is_recognized_bundle "$1"\n', "--", str(lib)],
        cwd=root,
        env=installation[1],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert probe.returncode == 1


@pytest.mark.parametrize("marker", [".apm-installed", "VERSION", "_internal"])
def test_symlinked_bundle_identity_is_not_authorization(
    installation: tuple[Path, dict[str, str]], marker: str
) -> None:
    """External identity entries cannot authorize replacement of unrelated bytes."""
    root, _ = installation
    bindir, lib = _prior(root, legacy=marker != ".apm-installed")
    keep = lib / "unrelated-data"
    keep.write_bytes(b"unrelated\x00bytes\n")
    original_launcher = (bindir / "apm").lstat().st_ino
    external = root / "external-identity"
    entry = lib / marker
    if marker == "_internal":
        entry.rmdir()
        external.mkdir()
        entry.symlink_to(external, target_is_directory=True)
    else:
        entry.unlink()
        external.write_bytes(b"external identity\n")
        entry.symlink_to(external)
    result = _run(installation, APM_INSTALL_DIR=str(bindir), APM_LIB_DIR=str(lib))
    assert result.returncode == 1, result.stdout + result.stderr
    assert keep.read_bytes() == b"unrelated\x00bytes\n"
    assert (bindir / "apm").lstat().st_ino == original_launcher
    assert entry.is_symlink()
    assert external.exists()
    assert not (root / "sudo.log").exists()


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


def test_path_discovery_does_not_spawn_per_entry(
    installation: tuple[Path, dict[str, str]],
) -> None:
    """A wider real PATH must not create one dirname process per candidate."""
    root, env = installation
    _record_tool_calls(root, "dirname")
    counts = []
    for width in (10, 100):
        entries = [str(root / f"missing-path-{width}-{index}") for index in range(width)]
        log = root / f"path-width-{width}.log"
        result = _run(
            installation,
            APM_INSTALL_DIR=str(root / f"install-{width}/bin"),
            PATH=":".join([*entries, env["PATH"]]),
            TOOL_LOG=str(log),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        counts.append(log.read_text(encoding="ascii").splitlines().count("dirname"))
    assert counts[0] == counts[1]


def test_fresh_install_uses_defaults(installation: tuple[Path, dict[str, str]]) -> None:
    """Fresh user installation creates only the documented home prefix."""
    root, _ = installation
    result = _run(installation)
    assert result.returncode == 0, result.stderr
    assert (root / "home/.local/bin/apm").resolve() == root / "home/.local/lib/apm/apm"
    assert not (root / "sudo.log").exists()
    assert not list((root / "home").glob(".*rc"))


@pytest.mark.parametrize("on_path", [False, True])
@pytest.mark.parametrize("exit_code", [0, 67])
def test_copied_launcher_is_checked_before_completion(
    installation: tuple[Path, dict[str, str]], on_path: bool, exit_code: int
) -> None:
    """Both PATH branches must verify the installed launcher and explain failure."""
    root, env = installation
    binary = Path(env["TMP_DIR"]) / env["EXTRACTED_DIR"] / "apm"
    binary.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$0" >> "$SMOKE_LOG"\n'
        f'printf "apm fixture\\n"\nexit {exit_code}\n',
        encoding="ascii",
    )
    bindir = root / "home/.local/bin"
    log = root / "smoke.log"
    result = _run(
        installation,
        PATH=f"{bindir}:{env['PATH']}" if on_path else env["PATH"],
        SMOKE_LOG=str(log),
    )
    assert log.is_file()
    assert log.read_text(encoding="ascii").splitlines() == [str(bindir / "apm")]
    assert result.returncode == (1 if exit_code else 0)
    if exit_code:
        assert "failed its --version check" in result.stderr
        assert "same destinations" in result.stderr
        assert "Installation complete!" not in result.stdout
    else:
        assert "Installation complete!" in result.stdout


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


@pytest.mark.parametrize("explicit", ["neither", "install-only", "lib-only", "both"])
def test_administrator_requires_both_destinations(
    installation: tuple[Path, dict[str, str]], explicit: str
) -> None:
    """Simulate only root identity; all writes remain ordinary-user fixture writes."""
    root, _ = installation
    identity = root / "tools/id"
    identity.unlink()
    identity.write_text("#!/bin/sh\nprintf '0\\n'\n", encoding="ascii")
    identity.chmod(0o755)
    options = {}
    if explicit in ("install-only", "both"):
        options["APM_INSTALL_DIR"] = str(root / "usr/local/bin")
    if explicit in ("lib-only", "both"):
        options["APM_LIB_DIR"] = str(root / "usr/local/lib/apm")
    result = _run(installation, **options)
    assert result.returncode == (0 if explicit == "both" else 1), result.stderr
    if explicit == "both":
        assert (root / "usr/local/bin/apm").is_symlink()
    else:
        assert "requires explicit" in result.stderr
        assert not (root / "home/.local").exists()
        assert not (root / "usr").exists()
    assert not (root / "sudo.log").exists()


@pytest.mark.parametrize("query_exit", [0, 61])
def test_pip_fallback_uses_selected_python_user_scripts(
    installation: tuple[Path, dict[str, str]], query_exit: int
) -> None:
    """Use pip's interpreter and user scheme, with no guessed or unsafe export."""
    root, _ = installation
    fake_modules, _ = _stage_fake_python_pip(root, query_exit=query_exit)
    standalone = root / "tools/pip3"
    standalone.write_text(
        f'#!/bin/sh\nprintf "called\\n" >> "$STANDALONE_PIP_LOG"\n'
        f'exec {shlex.quote(sys.executable)} -m pip "$@"\n',
        encoding="ascii",
    )
    standalone.chmod(0o755)
    user_base = root / "python user 'base"
    log = root / "pip.log"
    standalone_log = root / "standalone-pip.log"
    result = _run(
        installation,
        pip_fallback=True,
        PYTHONPATH=str(fake_modules),
        PYTHONUSERBASE=str(user_base),
        PIP_LOG=str(log),
        STANDALONE_PIP_LOG=str(standalone_log),
        APM_PYPI_INDEX_URL="https://mirror.invalid/simple",
    )
    assert result.returncode == (1 if query_exit else 0), result.stdout + result.stderr
    if query_exit:
        assert "user-script directory" in result.stderr
        assert not log.exists()
        assert "Installation complete!" not in result.stdout
    else:
        mirror = "https://mirror.invalid/simple"
        assert log.read_text(encoding="ascii").splitlines() == [
            "install",
            "--user",
            "--index-url",
            mirror,
            "apm-cli",
        ]
        assert mirror not in result.stdout + result.stderr
        export = next(
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith("export PATH=")
        )
        executed = subprocess.run(
            ["/bin/sh", "-c", export + '\nprintf "%s\\n" "$PATH"'],
            cwd=root,
            env=installation[1],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert executed.returncode == 0, executed.stderr
        assert executed.stdout == f"{user_base / 'bin'}:{installation[1]['PATH']}\n"
        assert "No shell profiles were changed." in result.stdout
    assert not standalone_log.exists()
    assert not list((root / "home").iterdir())
    assert not (root / "sudo.log").exists()


@pytest.mark.parametrize("scripts_name", ["normal path", "colon:path", "line\nbreak"])
def test_pip_fallback_printed_handoff_executes(
    installation: tuple[Path, dict[str, str]], scripts_name: str
) -> None:
    """Printed pip PATH or absolute guidance must execute the installed launcher."""
    root, env = installation
    fake_modules, _ = _stage_fake_python_pip(root, install_launcher=True)
    user_base = root / scripts_name
    log = root / "pip-handoff.log"
    result = _run(
        installation,
        pip_fallback=True,
        PYTHONPATH=str(fake_modules),
        PYTHONUSERBASE=str(user_base),
        PIP_LOG=str(log),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if ":" in scripts_name or "\n" in scripts_name:
        assert "export PATH=" not in result.stdout
        instruction = (
            result.stdout.split("Run APM using its absolute path:\n", 1)[1]
            .split("\nFor PATH discovery,", 1)[0]
            .strip()
        )
        run_hint = instruction.rsplit(" --version", 1)[0]
    else:
        instruction = next(
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith("export PATH=")
        )
        instruction += "\napm --version"
        run_hint = "apm"
    executed = subprocess.run(
        ["/bin/sh", "-c", instruction],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert executed.returncode == 0, executed.stderr
    assert executed.stdout == "pip fixture\n"
    assert f"{run_hint} init my-app" in result.stdout
    assert f"{run_hint} run" in result.stdout
    assert "No shell profiles were changed." in result.stdout
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


@pytest.mark.parametrize("python_name", ["python3", "python"])
@pytest.mark.parametrize("failure_stage", ["binary", "glibc"])
def test_pip_recovery_guidance_uses_selected_python_command(
    installation: tuple[Path, dict[str, str]],
    python_name: str,
    failure_stage: Literal["binary", "glibc"],
) -> None:
    """Eligible manual recovery must use the interpreter selected by the installer."""
    root, _ = installation
    python = root / "tools" / python_name
    python.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then printf \'3.10\\n\'; exit 0; fi\n'
        'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then exit 1; fi\n'
        "exit 1\n",
        encoding="ascii",
    )
    python.chmod(0o755)
    if failure_stage == "glibc":
        ldd = root / "tools/ldd"
        ldd.write_text("#!/bin/sh\nprintf 'ldd fixture 2.17\\n'\n", encoding="ascii")
        ldd.chmod(0o755)
    result = _run(installation, failure_stage=failure_stage)
    assert result.returncode == 1
    assert f"{python_name} -m pip install --user apm-cli" in result.stdout
    assert not any(
        line.strip().startswith(("pip install ", "pip3 install "))
        for line in result.stdout.splitlines()
    )


@pytest.mark.parametrize("failure_stage", ["binary", "glibc"])
def test_missing_python_recovery_does_not_invent_pip_command(
    installation: tuple[Path, dict[str, str]],
    failure_stage: Literal["binary", "glibc"],
) -> None:
    """Missing Python guidance must not invent an interpreter-backed command."""
    root, _ = installation
    if failure_stage == "glibc":
        ldd = root / "tools/ldd"
        ldd.write_text("#!/bin/sh\nprintf 'ldd fixture 2.17\\n'\n", encoding="ascii")
        ldd.chmod(0o755)
    result = _run(installation, failure_stage=failure_stage)
    assert result.returncode == 1
    assert "Install Python 3.10+ first" in result.stdout
    assert "-m pip install" not in result.stdout
    assert not any(
        line.strip().startswith(("pip install ", "pip3 install "))
        for line in result.stdout.splitlines()
    )


def test_fail_closed_pip_recovery_never_prints_public_command(
    installation: tuple[Path, dict[str, str]],
) -> None:
    """Fail-closed recovery must require the configured mirror, not public PyPI."""
    root, _ = installation
    python = root / "tools/python3"
    python.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then printf \'3.10\\n\'; exit 0; fi\n'
        'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then exit 1; fi\n'
        "exit 1\n",
        encoding="ascii",
    )
    python.chmod(0o755)
    result = _run(
        installation,
        failure_stage="binary",
        APM_NO_DIRECT_FALLBACK="1",
    )
    assert result.returncode == 1
    assert "APM_PYPI_INDEX_URL" in result.stdout
    assert "python3 -m pip install --user apm-cli" not in result.stdout
    assert "Attempting installation via python3 -m pip" not in result.stdout


@pytest.mark.parametrize("kind", ["existing", "custom", "administrator"])
def test_ineligible_fallback_prints_no_pip_attempt_or_command(
    installation: tuple[Path, dict[str, str]], kind: str
) -> None:
    """Refused ownership states must not announce an automatic pip attempt."""
    root, _ = installation
    options = {}
    if kind == "existing":
        _prior(root, "usr/local")
    elif kind == "custom":
        options["APM_INSTALL_DIR"] = str(root / "custom/bin")
    else:
        identity = root / "tools/id"
        identity.unlink()
        identity.write_text("#!/bin/sh\nprintf '0\\n'\n", encoding="ascii")
        identity.chmod(0o755)
        options["APM_INSTALL_DIR"] = str(root / "usr/local/bin")
        options["APM_LIB_DIR"] = str(root / "usr/local/lib/apm")
    result = _run(installation, failure_stage="binary", **options)
    assert result.returncode == 1
    assert "Pip fallback cannot preserve" in result.stderr
    assert "Attempting automatic fallback to pip" not in result.stdout
    assert "Attempting installation via pip" not in result.stdout
    assert not any(
        line.strip().startswith(("pip install ", "pip3 install ")) or " -m pip install " in line
        for line in result.stdout.splitlines()
    )


@pytest.mark.parametrize("kind", ["existing", "custom", "fresh"])
@pytest.mark.parametrize("python_version", [None, "3.9", "3.10"])
@pytest.mark.parametrize("failure_stage", ["binary", "glibc"])
def test_binary_failure_advice_preserves_installation_ownership(
    installation: tuple[Path, dict[str, str]],
    kind: str,
    python_version: str | None,
    failure_stage: Literal["binary", "glibc"],
) -> None:
    """The real failure caller must not recommend a second install after refusal."""
    root, _ = installation
    if failure_stage == "glibc":
        ldd = root / "tools/ldd"
        ldd.write_text("#!/bin/sh\nprintf 'ldd fixture 2.17\\n'\n", encoding="ascii")
        ldd.chmod(0o755)
    if python_version is not None:
        python = root / "tools/python3"
        python.write_text(
            f'#!/bin/sh\n[ "$1" = "-c" ] || exit 1\nprintf "{python_version}\\n"\n',
            encoding="ascii",
        )
        python.chmod(0o755)
    options = {}
    if kind == "existing":
        _prior(root, "usr/local")
    elif kind == "custom":
        options["APM_INSTALL_DIR"] = str(root / "custom/bin")
    result = _run(installation, failure_stage=failure_stage, **options)
    assert result.returncode == 1
    if kind == "fresh":
        if python_version == "3.10":
            assert "pip is not available" in result.stdout
            assert "python3 -m pip install --user apm-cli" in result.stdout
        else:
            assert "Python 3.10+" in result.stdout
            assert "-m pip install" not in result.stdout
        assert not any(
            line.strip().startswith(("pip install ", "pip3 install "))
            for line in result.stdout.splitlines()
        )
    else:
        assert "Pip fallback cannot preserve" in result.stderr
        assert "-m pip install" not in result.stdout
        assert "Manual installation options" not in result.stdout
    if kind == "existing":
        assert (root / "usr/local/lib/apm/VERSION").read_text(encoding="ascii") == "old\n"
    assert not (root / "home/.local").exists()


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
        _prior(root, "system")
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
