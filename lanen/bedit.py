import os

os.chdir("/tmp/bm-wt")


def edit(p, o, n, cnt=1):
    s = open(p).read()
    assert s.count(o) == cnt, (p, o[:60], s.count(o))
    open(p, "w").write(s.replace(o, n))


IMP = "from apm_cli.utils.research_passmemo import passmemo as _passmemo\n"

ch = "src/apm_cli/utils/content_hash.py"
edit(ch, "from apm_cli.utils.paths import relative_to\n",
     "from apm_cli.utils.paths import relative_to\n" + IMP)
edit(ch, '''def _hash_package_tree(
    package_path: Path, exclude: Collection[str], *, legacy: bool, framed: bool
) -> tuple[str, str]:
    """Feed only the requested streams; an unrequested stream returns ``""``."""
''', '''def _hash_package_tree(
    package_path: Path, exclude: Collection[str], *, legacy: bool, framed: bool
) -> tuple[str, str]:
    """Research: one pass computes both streams; callers get the ones they asked for."""
    both = _hash_package_tree_both(package_path, tuple(sorted(exclude)))
    return (both[0] if legacy else ""), (both[1] if framed else "")


@_passmemo("hash_tree")
def _hash_package_tree_both(package_path: Path, exclude: tuple[str, ...]) -> tuple[str, str]:
    return _hash_package_tree_impl(package_path, exclude, legacy=True, framed=True)


def _hash_package_tree_impl(
    package_path: Path, exclude: Collection[str], *, legacy: bool, framed: bool
) -> tuple[str, str]:
    """Feed only the requested streams; an unrequested stream returns ``""``."""
''')
edit(ch, "def _package_files(\n    package_path: Path,",
     "@_passmemo(\"package_files\", copy=list)\ndef _package_files(\n    package_path: Path,")
edit(ch, "def compute_file_hash(file_path: Path) -> str:",
     "@_passmemo(\"file_hash\")\ndef compute_file_hash(file_path: Path) -> str:")

h = "src/apm_cli/utils/helpers.py"
edit(h, "def find_plugin_json(",
     IMP + "\n\n@_passmemo(\"find_plugin_json\")\ndef find_plugin_json(")

fd = "src/apm_cli/models/format_detection.py"
edit(fd, "def _collect_nested_skill_dirs(package_path: Path)",
     IMP + "\n\n@_passmemo(\"nested_skill_dirs\")\ndef _collect_nested_skill_dirs(package_path: Path)")
edit(fd, '''    def detect(
        self,
        package_path: Path,
        *,
        agent_plugin_detection: AgentPluginDetection | None = None,
    ) -> DetectionReport:''', '''    def detect(self, package_path: Path, **kwargs):
        if self._detectors is not _DEFAULT_DETECTORS:
            return self._detect(package_path, **kwargs)
        return _detect_default(self, package_path, **kwargs)

    def _detect(
        self,
        package_path: Path,
        *,
        agent_plugin_detection: AgentPluginDetection | None = None,
    ) -> DetectionReport:''')
open(fd, "a").write('''

@_passmemo("format_detect", path_arg=1)
def _detect_default(detector, package_path, **kwargs):
    return detector._detect(package_path, **kwargs)
''')

si = "src/apm_cli/integration/skill_integrator.py"
edit(si, '''    @staticmethod
    def _skill_names_in_directory(skills_dir: Path) -> frozenset[str]:''', '''    @staticmethod
    @_passmemo("skill_names")
    def _skill_names_in_directory(skills_dir: Path) -> frozenset[str]:''')
s = open(si).read()
i = s.index("\nclass ")
open(si, "w").write(s[:i] + "\n" + IMP + s[i:])

cl = "src/apm_cli/deps/carried_license.py"
edit(cl, "def _skill_dirs(package_root: Path) -> list[Path]:",
     IMP + "\n\n@_passmemo(\"carried_skill_dirs\", copy=list)\ndef _skill_dirs(package_root: Path) -> list[Path]:")
print("ok")
