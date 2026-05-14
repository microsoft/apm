# -*- mode: python ; coding: utf-8 -*-

import sys
import os
import subprocess
from pathlib import Path

# Check if UPX is available
def is_upx_available():
    try:
        subprocess.run(['upx', '--version'], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def should_use_upx():
    """Enable UPX only on non-Windows platforms where it is available.

    UPX-compressed PE binaries trigger ML-based AV false positives
    (e.g. Trojan:Win32/Bearfoos.B!ml) on Windows Defender.
    """
    if sys.platform == 'win32':
        return False
    return is_upx_available()

# Get the directory where this spec file is located
spec_dir = Path(SPECPATH)
repo_root = spec_dir.parent

# --- Windows PE version info (reduces AV false positives) ---
# Anonymous executables without metadata score poorly in AV ML models.
# Embedding company, product, and version info into the PE header provides
# positive trust signals to heuristic scanners like Windows Defender.
def _read_version_from_pyproject(repo_root):
    """Read version string from pyproject.toml and return as 4-tuple."""
    import re
    pyproject = repo_root / 'pyproject.toml'
    if not pyproject.exists():
        return (0, 0, 0, 0)
    content = pyproject.read_text(encoding='utf-8')
    match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
    if not match:
        return (0, 0, 0, 0)
    parts = re.match(r'(\d+)\.(\d+)\.(\d+)', match.group(1))
    if not parts:
        return (0, 0, 0, 0)
    return (int(parts.group(1)), int(parts.group(2)), int(parts.group(3)), 0)

_win_version_info = None
if sys.platform == 'win32':
    try:
        from PyInstaller.utils.win32 import versioninfo as vi
        _ver = _read_version_from_pyproject(repo_root)
        _ver_str = f'{_ver[0]}.{_ver[1]}.{_ver[2]}'
        _win_version_info = vi.VSVersionInfo(
            ffi=vi.FixedFileInfo(
                filevers=_ver,
                prodvers=_ver,
                mask=0x3f,
                flags=0x0,
                OS=0x40004,    # VOS_NT_WINDOWS32
                fileType=0x1,  # VFT_APP
                subtype=0x0,
            ),
            kids=[
                vi.StringFileInfo([vi.StringTable('040904B0', [  # Lang: US English (0409), Charset: Unicode (04B0)
                    vi.StringStruct('CompanyName', 'Microsoft'),
                    vi.StringStruct('FileDescription',
                                    'APM - Agent Package Manager'),
                    vi.StringStruct('FileVersion', _ver_str),
                    vi.StringStruct('InternalName', 'apm'),
                    vi.StringStruct('LegalCopyright',
                                    'Copyright (c) Microsoft Corporation'),
                    vi.StringStruct('OriginalFilename', 'apm.exe'),
                    vi.StringStruct('ProductName', 'APM'),
                    vi.StringStruct('ProductVersion', _ver_str),
                ])]),
                vi.VarFileInfo([vi.VarStruct('Translation', [1033, 1200])]),  # LCID 1033 = en-US, Codepage 1200 = UTF-16
            ],
        )
    except ImportError:
        _win_version_info = None

# APM CLI entry point
entry_point = repo_root / 'src' / 'apm_cli' / 'cli.py'

# Data files to include - recursively include all template files
datas = [
    (str(repo_root / 'scripts' / 'runtime'), 'scripts/runtime'),  # Bundle runtime setup scripts
    (str(repo_root / 'pyproject.toml'), '.'),  # Bundle pyproject.toml for version reading
]

# Bundle platform-appropriate token helper
if sys.platform == 'win32':
    datas.append((str(repo_root / 'scripts' / 'windows' / 'github-token-helper.ps1'), 'scripts'))
else:
    datas.append((str(repo_root / 'scripts' / 'github-token-helper.sh'), 'scripts'))

# Recursively add all files from templates directory, including hidden directories
def collect_template_files(templates_root):
    """Recursively collect all template files, including those in hidden directories."""
    template_files = []
    
    for root, dirs, files in os.walk(templates_root):
        for file in files:
            source_path = os.path.join(root, file)
            # Calculate the relative path from the templates root
            rel_path = os.path.relpath(source_path, templates_root)
            # Destination should maintain the same structure under templates/
            dest_dir = os.path.dirname(f'templates/{rel_path}')
            if dest_dir == 'templates':
                dest_dir = 'templates'
            template_files.append((source_path, dest_dir))
    
    return template_files

# Add all template files to datas
template_files = collect_template_files(str(repo_root / 'templates'))
datas.extend(template_files)

# Hidden imports for APM modules that might not be auto-detected
hiddenimports = [
    'apm_cli',
    'apm_cli.cli',
    'apm_cli.config',
    'apm_cli.factory',
    'apm_cli.version',  # Add version module
    'apm_cli.adapters',
    'apm_cli.adapters.client',
    'apm_cli.adapters.client.base',
    'apm_cli.adapters.client.vscode',
    'apm_cli.adapters.client.windsurf',
    'apm_cli.adapters.package_manager',
    'apm_cli.compilation',  # Add compilation module
    'apm_cli.compilation.agents_compiler',
    'apm_cli.compilation.template_builder',
    'apm_cli.compilation.link_resolver',
    'apm_cli.compilation.constitution',
    'apm_cli.compilation.constitution_block',
    'apm_cli.compilation.constants',
    'apm_cli.compilation.context_optimizer',
    'apm_cli.compilation.distributed_compiler',
    'apm_cli.compilation.injector',
    'apm_cli.primitives',  # Add primitives module
    'apm_cli.primitives.models',
    'apm_cli.primitives.discovery',
    'apm_cli.primitives.parser',
    'apm_cli.core',
    'apm_cli.core.operations',
    'apm_cli.core.script_runner',
    'apm_cli.core.conflict_detector',
    'apm_cli.core.docker_args',
    'apm_cli.core.safe_installer',
    'apm_cli.core.token_manager',
    'apm_cli.deps',
    'apm_cli.deps.aggregator',
    'apm_cli.deps.verifier',
    'apm_cli.deps.apm_resolver',
    'apm_cli.deps.github_downloader',
    'apm_cli.deps.package_validator',
    'apm_cli.deps.dependency_graph',
    'apm_cli.models',
    'apm_cli.models.apm_package',
    'apm_cli.output',
    'apm_cli.output.formatters',
    'apm_cli.output.models',
    'apm_cli.output.script_formatters',
    'apm_cli.registry',
    'apm_cli.registry.client',
    'apm_cli.registry.integration',
    'apm_cli.runtime',
    'apm_cli.runtime.base',
    'apm_cli.runtime.codex_runtime',
    'apm_cli.runtime.factory',
    'apm_cli.runtime.llm_runtime',
    'apm_cli.runtime.manager',  # Add runtime manager
    'apm_cli.utils',
    'apm_cli.utils.helpers',
    'apm_cli.workflow',
    'apm_cli.workflow.runner',
    'apm_cli.workflow.parser', 
    'apm_cli.workflow.discovery',
    # Common dependencies
    'yaml',
    'click',
    'colorama',
    'pathlib',
    'frontmatter',
    'requests',
    'certifi',  # CA certificate bundle for SSL verification in frozen binary
    # Rich modules (lazily imported, must be explicitly included)
    'rich',
    'rich.console',
    'rich.theme',
    'rich.panel',
    'rich.table',
    'rich.text',
    'rich.prompt',
    # Standard library modules needed for HTTP/networking
    'email',
    'email.message',
    'email.parser',
    'email.utils',
    'urllib',
    'urllib.parse',
    'urllib.request', 
    'urllib.response',
    'urllib.error',
    'http',
    'http.client',
    'html',
    'html.parser',
    # JSON and TOML parsers for config files
    'json',
    'toml',
    # Subprocess for runtime operations
    'subprocess',
    'shlex',
    # Version detection for pip installations
    'importlib.metadata',
    'importlib_metadata',
]

# Modules to exclude to reduce binary size
excludes = [
    # GUI frameworks - not needed for CLI
    'tkinter',
    'PyQt5',
    'PyQt6',
    'PySide2',
    'PySide6',
    'PIL',
    # Data science libraries - not needed
    'matplotlib',
    'scipy',
    'numpy',
    'pandas',
    # Interactive environments - not needed
    'jupyter',
    'IPython',
    'notebook',
    # Development/testing tools - not needed in binary
    'unittest',
    'doctest',
    'pdb',
    'bdb',
    'test',
    'tests',
    # Build tools - not needed at runtime (but keep distutils as it's needed by importlib.metadata)
    'lib2to3',
    # Audio/image processing - not needed
    'wave',          # safe to exclude
    'audioop',       # safe to exclude
    'chunk',         # safe to exclude
    'imghdr',        # not needed
    'sndhdr',        # not needed
    'sunau',         # not needed
]

a = Analysis(
    [str(entry_point)],
    pathex=[str(repo_root / 'src')],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(repo_root / 'build' / 'hooks' / 'runtime_hook_ssl_certs.py')],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
    optimize=1,  # -O: strip asserts; keep __doc__ so Click reads command help from docstrings (#1298)
)

# Exclude bundled OpenSSL shared libraries on Linux.
# PyInstaller's bootloader sets LD_LIBRARY_PATH to the binary directory in
# --onedir mode. When apm spawns git, git-remote-https inherits that path
# and loads the bundled (build-machine) libssl instead of the system one.
# On distros where system libcurl requires a newer OpenSSL ABI than the
# build machine provides (e.g. Fedora 43 with OPENSSL_3.2.0), this causes
# "symbol lookup error" and git clone failures. Excluding these libs lets
# the system OpenSSL be used instead, which is expected to be available on
# supported Linux targets. Python's _ssl module still works because it finds
# system libssl via the standard dynamic linker search path. See:
# github.com/microsoft/apm/issues/462
if sys.platform == 'linux':
    _openssl_libs = {'libssl.so.3', 'libcrypto.so.3'}
    a.binaries = [(name, path, typ) for name, path, typ in a.binaries
                  if name not in _openssl_libs]

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# GNU strip corrupts Windows PE/COFF binaries; only enable on Unix
_strip = sys.platform != 'win32'

# Switch to --onedir for directory-based deployment (faster startup with --onedir)
exe = EXE(
    pyz,
    a.scripts,
    [],            # Empty for --onedir mode
    exclude_binaries=True,  # Exclude binaries for --onedir mode
    name='apm',
    debug=False,
    bootloader_ignore_signals=False,
    strip=_strip,  # Strip debug symbols (Unix only; corrupts Windows DLLs)
    upx=should_use_upx(),  # Enable UPX compression only if available (disabled on Windows)
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=_win_version_info,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=_strip,
    upx=should_use_upx(),
    upx_exclude=[],
    name='apm'
)
