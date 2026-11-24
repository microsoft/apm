from pathlib import Path

p = Path("tests/benchmarks/cpu_attr.py")
s = p.read_text()

old = '''_realpath_calls = collections.Counter()'''
new = '''_realpath_calls = collections.Counter()
_leaf_kind = collections.Counter()

# Native-core gate categories, by the innermost Python frame (C calls such as
# os.stat or hashlib are charged to the Python frame that made them).
_KINDS = (
    ("import", ("<import>",)),
    ("yaml_emit", ("yaml/emitter", "yaml/serializer", "yaml/representer")),
    ("yaml_parse", ("yaml/scanner", "yaml/parser", "yaml/reader", "yaml/composer", "yaml/constructor", "yaml/resolver", "yaml/nodes", "yaml/events", "yaml/tokens", "yaml/cyaml", "yaml/loader", "_yaml")),
    ("realpath", (":realpath", ":_joinrealpath", ":resolve", ":_getfinalpathname", ":_readlink_deep", ":abspath")),
    ("stat", (":stat", ":lstat", ":exists", ":is_file", ":is_dir", ":is_symlink", ":isdir", ":isfile", ":islink", ":lexists", ":samefile", ":getsize")),
    ("walk_scan", (":walk", ":scandir", ":_scandir", ":iterdir", ":listdir", ":_select_from", ":glob", ":rglob", ":_iterate_directories", "glob.py:", "fnmatch.py:")),
    ("hash", ("hashlib", "content_hash.py:", ":_hash", ":_digest", ":hexdigest")),
    ("file_io", (":open", ":read_bytes", ":read_text", ":write_bytes", ":write_text", ":read", ":write", "shutil.py:", "tempfile.py:", "atomic_io.py:", ":_fastcopy_sendfile", ":copyfile", ":copystat", ":replace", ":rename", ":unlink", ":mkdir", ":makedirs", ":rmdir", ":chmod", ":fsync", ":fdopen", "codecs.py:")),
    ("network", ("ssl.py:", "socket.py:", "http/client", "urllib", "selectors.py:", "truststore", "certifi", "requests/", "urllib3/")),
    ("subprocess_parent", ("subprocess.py:",)),
    ("thread_wait", ("threading.py:", "thread.py:", "queue.py:")),
)


def _kind(keys: list[str]) -> str:
    leaf = keys[0]
    if any("<import>" in key for key in keys):
        return "import"
    for name, needles in _KINDS[1:]:
        if any(needle in leaf for needle in needles):
            return name
    return "python_other"'''
assert old in s
s = s.replace(old, new, 1)

old = '''                if not keys[0].startswith("apm_cli/"):
                    _leaf_owner[f"{keys[0]} <- {owner}"] += delta'''
new = '''                if not keys[0].startswith("apm_cli/"):
                    _leaf_owner[f"{keys[0]} <- {owner}"] += delta
                _leaf_kind[_kind(keys)] += delta'''
assert old in s
s = s.replace(old, new, 1)

old = '''        "apm_owner_self": top(_apm_owner, 150),'''
new = '''        "leaf_kind": top(_leaf_kind, 30),
        "apm_owner_self": top(_apm_owner, 150),'''
assert old in s
s = s.replace(old, new, 1)
p.write_text(s)
print("patched")
