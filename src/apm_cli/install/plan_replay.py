"""PoC (lane C, measurement only, never shipped): replay a recorded integrate phase.

Record: after acquire, snapshot the project tree and the integrate-visible state
(ctx, integrators, materialization session); after integrate, store the file
diff and every changed attribute in the machine cache.
Replay: on a key hit, write the recorded files, restore the attributes in place
and skip the integrators entirely.
"""

from __future__ import annotations

import hashlib
import os
import pickle
import sys
from pathlib import Path
from typing import Any

_SKIP_DIRS = {"apm_modules", ".git", ".apm-local"}
_DEBUG = os.environ.get("APM_POC_REPLAY_DEBUG") == "1"


def _log(msg: str) -> None:
    if _DEBUG:
        sys.stderr.write(f"[poc-replay] {msg}\n")


def _tree(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel == ".":
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                out[os.path.relpath(path, root)] = b"\0link:" + os.readlink(path).encode()
                continue
            with open(path, "rb") as handle:
                out[os.path.relpath(path, root)] = handle.read()
    return out


def _objects(ctx: Any) -> dict[str, Any]:
    objs = {"ctx": ctx}
    for name, integ in (getattr(ctx, "integrators", None) or {}).items():
        if integ is not None:
            objs[f"integrator:{name}"] = integ
    skill = (getattr(ctx, "integrators", None) or {}).get("skill")
    session = getattr(skill, "_materialization_session", None)
    if session is not None:
        objs["session"] = session
    return objs


def _state(objs: dict[str, Any]) -> dict[tuple[str, str], bytes | None]:
    state: dict[tuple[str, str], bytes | None] = {}
    tracked = {id(o) for o in objs.values()}
    for oname, obj in objs.items():
        for attr, value in vars(obj).items():
            if id(value) in tracked or (
                isinstance(value, dict) and any(id(v) in tracked for v in value.values())
            ):
                continue
            try:
                state[(oname, attr)] = pickle.dumps(value, protocol=5)
            except Exception:
                state[(oname, attr)] = None
    return state


def _key(ctx: Any, before: dict[str, bytes]) -> str:
    from apm_cli.version import get_version

    h = hashlib.sha256()
    h.update(get_version().encode())
    h.update(repr(sorted((k, str(v)) for k, v in (ctx.package_hashes or {}).items())).encode())
    h.update(repr([d.get_unique_key() for d in ctx.deps_to_install]).encode())
    h.update(repr(sorted(getattr(t, "name", str(t)) for t in (ctx.targets or []))).encode())
    h.update(repr((str(ctx.scope), str(ctx.project_root), bool(ctx.force))).encode())
    h.update(repr(sorted(ctx.managed_files or [])).encode())
    for rel in sorted(before):
        h.update(rel.encode() + b"\0" + hashlib.sha256(before[rel]).digest())
    return h.hexdigest()


def _store() -> Path:
    from apm_cli.cache.paths import get_cache_root

    return get_cache_root() / "poc-deploy-plans"


class Recorder:
    def __init__(self, ctx: Any) -> None:
        self.root = Path(ctx.project_root)
        self.before = _tree(self.root)
        self.key = _key(ctx, self.before)
        self.path = _store() / f"{self.key}.pickle"
        self.objs = _objects(ctx)
        self.state = None

    def try_replay(self, ctx: Any) -> bool:
        if not self.path.is_file():
            return False
        try:
            plan = pickle.loads(self.path.read_bytes())
        except Exception as exc:
            _log(f"unreadable plan: {exc}")
            return False
        for rel, data in plan["write"].items():
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if data.startswith(b"\0link:"):
                os.symlink(data[6:].decode(), dest)
            else:
                dest.write_bytes(data)
        for rel in plan["delete"]:
            (self.root / rel).unlink(missing_ok=True)
        for (oname, attr), blob in plan["attrs"].items():
            obj = self.objs.get(oname)
            if obj is None:
                continue
            new = pickle.loads(blob)
            cur = vars(obj).get(attr)
            if isinstance(cur, dict) and isinstance(new, dict):
                cur.clear()
                cur.update(new)
            elif isinstance(cur, list) and isinstance(new, list):
                cur[:] = new
            elif isinstance(cur, set) and isinstance(new, set):
                cur.clear()
                cur.update(new)
            else:
                setattr(obj, attr, new)
        _log(f"replayed {self.key[:12]}: {len(plan['write'])} files, {len(plan['attrs'])} attrs")
        return True

    def start(self) -> None:
        self.state = _state(self.objs)

    def finish(self, ctx: Any) -> None:
        if self.state is None:
            return
        after_state = _state(_objects(ctx))
        attrs: dict[tuple[str, str], bytes] = {}
        for k, blob in after_state.items():
            if blob is None:
                if self.state.get(k, b"") is not None or k not in self.state:
                    _log(f"unpicklable changed-or-new attr {k}: not recording")
                    return
                continue
            if self.state.get(k) != blob:
                attrs[k] = blob
        unknown = [k for k, v in self.state.items() if v is None]
        _log(f"unpicklable (assumed unchanged): {unknown}")
        after = _tree(self.root)
        write = {rel: data for rel, data in after.items() if self.before.get(rel) != data}
        delete = [rel for rel in self.before if rel not in after]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(pickle.dumps({"write": write, "delete": delete, "attrs": attrs}, protocol=5))
        os.replace(tmp, self.path)
        _log(f"recorded {self.key[:12]}: {len(write)} files, {len(attrs)} attrs")
