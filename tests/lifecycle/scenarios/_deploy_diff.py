"""Shared lane C differential kit: one change table and one state oracle.

Ported from W4's incremental-vs-fresh module (tests/lifecycle/security/
test_incremental_deploy_differential.py, branch w4-t7-lane-c-differential
@ b0cd0373) so that every lane C differential suite uses ONE oracle:

- ``CHANGES``: the user-visible changes between two installs (content,
  rename, delete, target +/-, stricter policy, revoked approval, and the
  out-of-band locked-file-missing, user edit and symlinked destination).
- ``comparable``: deployed state that can be compared across two worlds (the
  project and HOME bytes with exec bits, the parsed manifest and the lock),
  with paths and per-checkout identity normalized away.
- ``diff``: names every differing file and lock field, for the failure message.

Consumers: tests/lifecycle/scenarios/test_engine_parity.py (fast engine ==
full pipeline, same history) and W4's incremental == fresh deploy module.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from tests.lifecycle import driver
from tests.lifecycle.world import World
from tests.lifecycle.world.scopes import use_policy
from tests.lifecycle.world.services import ORG
from tests.utils.local_package import LocalPackageFactory

KIT = "c-kit"
KIT_SPEC = f"../../authored/{KIT}"
SKILL_PATH = ".agents/skills/c-alpha/SKILL.md"
_SKILL = "---\nname: {name}\ndescription: Lifecycle kit skill.\n---\n\n{body}\n"
_RULE = '---\napplyTo: "**"\n---\n\n{body}\n'

Run = Callable[[World, tuple[str, ...]], object]


@dataclass(frozen=True)
class Inputs:
    """Everything a user controls that can change deployed output."""

    skills: tuple[tuple[str, str], ...] = (("c-alpha", "Use alpha."),)
    rules: tuple[tuple[str, str], ...] = (("c-rule", "Follow the kit rule."),)
    targets: tuple[str, ...] = ("copilot",)
    policy: str = "allow"
    approved: bool = True


BASE = Inputs()
BOTH_TARGETS = replace(BASE, targets=("copilot", "claude"))


def author(world: World, inputs: Inputs) -> None:
    root = world.root / "authored" / KIT
    if root.exists():
        shutil.rmtree(root)
    factory = LocalPackageFactory(root.parent)
    package = factory.create(KIT, version="1.0.0")
    for name, body in inputs.skills:
        factory.add_skill(package, name, _SKILL.format(name=name, body=body))
    for name, body in inputs.rules:
        factory.add_instruction(package, name, _RULE.format(body=body))


def declare(world: World, inputs: Inputs) -> None:
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["targets"] = list(inputs.targets)
    data.setdefault("dependencies", {})["apm"] = [world.packages["p-exec:git"].spec, KIT_SPEC]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def set_inputs(world: World, inputs: Inputs) -> None:
    use_policy(world, inputs.policy)
    author(world, inputs)
    declare(world, inputs)


def _default_run(world: World, argv: tuple[str, ...]):
    return driver.run(world, argv)


def converge(world: World, inputs: Inputs, run: Run = _default_run) -> None:
    """Set *inputs*, install, approve p-exec at user scope when asked, install again."""
    set_inputs(world, inputs)
    steps = [("install",)]
    if inputs.approved:
        steps += [("approve", "--user", "p-exec"), ("install",)]
    for argv in steps:
        result = run(world, argv)
        assert result.returncode == 0, f"{argv}: {result.stdout}{result.stderr}"
    assert (world.project / SKILL_PATH).is_file(), "the kit skill must deploy"


def revoke_user_approval(world: World) -> None:
    config = world.apm_home / "config.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    allow = data.get("executables", {}).get("allow", {})
    ref = world.packages["p-exec:git"]
    identities = (ref.spec, ref.name, f"{ORG}/{ref.name}")
    dropped = [k for k in list(allow) if any(k == i or k.startswith(i + "#") for i in identities)]
    assert dropped, f"no user approval of p-exec to revoke in {allow}"
    for key in dropped:
        del allow[key]
    config.write_text(json.dumps(data, indent=2), encoding="utf-8")


def apply_inputs(world: World, start: Inputs, end: Inputs) -> None:
    """Move *world* from *start* to *end* the way a user would, between two installs."""
    if (end.skills, end.rules) != (start.skills, start.rules):
        author(world, end)
    if end.targets != start.targets:
        declare(world, end)
    if end.policy != start.policy:
        use_policy(world, end.policy)
    if start.approved and not end.approved:
        revoke_user_approval(world)


# -- out-of-band changes: return the paths exempt from the byte comparison ----------


def delete_deployed(world: World) -> tuple[str, ...]:
    (world.project / SKILL_PATH).unlink()
    return ()


def edit_deployed(world: World) -> tuple[str, ...]:
    path = world.project / SKILL_PATH
    path.write_bytes(path.read_bytes() + b"\nmy local note\n")
    return (SKILL_PATH,)


def symlink_destination(world: World) -> tuple[str, ...]:
    outside = world.root / "outside-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text("not APM's file\n", encoding="ascii")
    deployed = world.project / SKILL_PATH
    shutil.rmtree(deployed.parent)
    deployed.parent.symlink_to(outside, target_is_directory=True)
    return (Path(SKILL_PATH).parent.as_posix(),)


@dataclass(frozen=True)
class Change:
    start: Inputs
    end: Inputs
    out_of_band: Callable[[World], tuple[str, ...]] | None = None

    def apply(self, world: World) -> tuple[str, ...]:
        """Apply the change to a converged *world*; return the exempt paths."""
        if self.out_of_band is not None:
            return self.out_of_band(world)
        apply_inputs(world, self.start, self.end)
        return ()


CHANGES: dict[str, Change] = {
    "file-content": Change(BASE, replace(BASE, skills=(("c-alpha", "Use alpha, v2."),))),
    "file-rename": Change(BASE, replace(BASE, skills=(("c-beta", "Use alpha."),))),
    "file-delete": Change(BASE, replace(BASE, rules=())),
    "target-added": Change(BASE, BOTH_TARGETS),
    "target-removed": Change(BOTH_TARGETS, BASE),
    "policy-denies-executables": Change(BASE, replace(BASE, policy="exec-deny-all")),
    "approval-revoked": Change(BASE, replace(BASE, approved=False)),
    "locked-file-missing": Change(BASE, BASE, delete_deployed),
    "user-edited-deployed-file": Change(BASE, BASE, edit_deployed),
    "symlinked-destination": Change(BASE, BASE, symlink_destination),
}

# -- the state oracle ------------------------------------------------------------

# Caches (git objects, policy, deploy store) are not deployed state.
HOME_CACHES = (".cache", "Library/Caches", ".apm/cache", ".apm/logs")
# Per-checkout identity: a random installation id and receipts keyed by the project path.
IDENTITY = (".apm-local", ".apm/deployment-receipts")
_IDENTITY_KEY = re.compile(r"install(ation)?_?id|receipt|generated_at")


def rooted(text: str, marker: str) -> str:
    """Replace every spelling of the world root *marker* with ``<ROOT>``.

    Windows writes the same root as ``C:\\x``, ``C:/x`` or JSON-escaped ``C:\\\\x``;
    on POSIX all three spellings are the same string.
    """
    forms = {marker, marker.replace("\\", "/"), marker.replace("\\", "\\\\")}
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, "<ROOT>")
    return text


def digest(root: Path, skip: tuple[str, ...], world: World) -> dict[str, str]:
    """``rel path -> [x-]sha256`` of every file under *root*, symlinks by target."""
    marker = str(world.root)
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")) if root.exists() else ():
        rel = path.relative_to(root).as_posix()
        if any(rel == s or rel.startswith(s + "/") for s in skip):
            continue
        if path.is_symlink():
            files[rel] = "link:" + rooted(str(path.readlink()), marker)
        elif path.is_file():
            raw = path.read_bytes()
            try:
                data = rooted(raw.decode("utf-8"), marker).encode("utf-8")
            except UnicodeDecodeError:
                data = raw.replace(marker.encode(), b"<ROOT>")
            mode = "x" if path.stat().st_mode & 0o111 else "-"
            files[rel] = mode + hashlib.sha256(data).hexdigest()
    return files


def without_identity(node: object, marker: str) -> object:
    if isinstance(node, dict):
        return {
            k: without_identity(v, marker)
            for k, v in node.items()
            if not _IDENTITY_KEY.search(str(k))
        }
    if isinstance(node, list):
        return [without_identity(v, marker) for v in node]
    return rooted(node, marker) if isinstance(node, str) else node


def _yaml(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None


_IDENTITY_LINE = re.compile(r"^\s*(install(ation)?_?id|generated_at)\s*:.*$\n?", re.MULTILINE)
# A receipt's checkout binding; its records, destinations, bindings and history stay compared.
_RECEIPT_BINDING = ("root", "root_identity", "installation", "root_birth_time")


def _text(path: Path, marker: str) -> str | None:
    """Raw file text (bytes order, comments, formatting), only identity lines and paths normalized."""
    if not path.is_file():
        return None
    # Decode bytes: read_text would translate CRLF and hide a line-ending change.
    return _IDENTITY_LINE.sub("", rooted(path.read_bytes().decode("utf-8"), marker))


def receipts(world: World, *, history: bool = True) -> list[object]:
    """Every deployment receipt of *world*, checkout binding removed, file names (path keys) dropped."""
    marker = str(world.root)
    docs = []
    for folder in {
        world.home / ".apm" / "deployment-receipts",
        world.apm_home / "deployment-receipts",
    }:
        for path in sorted(folder.rglob("*.json")) if folder.is_dir() else ():  # incl. mcp/
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in _RECEIPT_BINDING:
                data.pop(key, None)
            for row in data.get("files", []) if isinstance(data.get("files"), list) else ():
                if isinstance(row, dict):
                    row.pop("identity", None)  # a config record's device and inode (#5189)
            if not history:
                data.pop("history", None)
            # The checkout-keyed file name is identity; its directory (mcp/ or top) is not:
            # each receipt reader looks only in its own directory.
            data["directory"] = path.parent.relative_to(folder).as_posix()
            data["mode"] = oct(path.lstat().st_mode & 0o7777)
            docs.append(without_identity(data, marker))
    return sorted(docs, key=lambda doc: json.dumps(doc, sort_keys=True))


def comparable(
    world: World, exempt: tuple[str, ...] = (), *, receipt_history: bool = False
) -> dict[str, object]:
    """Deployed state comparable across two worlds: identity fields and paths normalized.

    The manifest and lockfile are compared both parsed (readable diffs) and as raw
    text (formatting, comments, key order); receipts by their substantive records.
    Receipt ``history`` (hashes of earlier contents, #4428) depends on the world's past,
    so only same-history comparisons (``receipt_history=True``) include it.
    """
    project_skip = (".git", "apm.lock.yaml", "apm.yml", *IDENTITY, *exempt)
    marker = str(world.root)
    return {
        "project": digest(world.project, project_skip, world),
        "home": digest(world.home, HOME_CACHES + IDENTITY, world),
        "manifest": without_identity(_yaml(world.project / "apm.yml"), marker),
        "lock": without_identity(_yaml(world.project / "apm.lock.yaml"), marker),
        "manifest_text": _text(world.project / "apm.yml", marker),
        "lock_text": _text(world.project / "apm.lock.yaml", marker),
        "receipts": receipts(world, history=receipt_history),
    }


_MISSING = object()  # an absent key differs from an explicit null (lockfile.py: apm_version)


def structural(a: object, b: object, labels: tuple[str, str], path: str = "") -> list[str]:
    """Paths where two parsed documents differ, with both values."""
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[str] = []
        for key in sorted(a.keys() | b.keys(), key=str):
            out += structural(a.get(key, _MISSING), b.get(key, _MISSING), labels, f"{path}.{key}")
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [
            d
            for i, (x, y) in enumerate(zip(a, b, strict=True))
            for d in structural(x, y, labels, f"{path}[{i}]")
        ]
    if a is not _MISSING and b is not _MISSING and a == b:
        return []
    shown = ["<absent>" if v is _MISSING else repr(v) for v in (a, b)]
    return [f"{path}: {labels[0]}={shown[0]} {labels[1]}={shown[1]}"]


def _shown(world: World, root_id: str, rel: str) -> str:
    path = (world.project if root_id == "project" else world.home) / rel
    if not path.is_file() or path.stat().st_size > 2000:
        return "<absent>" if not path.exists() else "<large>"
    return rooted(path.read_text(encoding="utf-8", errors="replace"), str(world.root))


def diff(
    left: dict,
    right: dict,
    worlds: tuple[World, World],
    labels: tuple[str, str] = ("incremental", "fresh"),
) -> list[str]:
    """Every differing file (with both contents) and every differing manifest/lock field."""
    lines = []
    for key in sorted(left.keys() | right.keys()):
        a, b = left.get(key), right.get(key)
        if key in ("manifest_text", "lock_text"):
            if a != b:
                lines.append(f"{key}: raw bytes differ (formatting, comments or key order)")
        elif key == "receipts":
            if a != b:
                lines += [f"receipts{d}" for d in structural({"r": a}, {"r": b}, labels)]
        elif key in ("project", "home"):
            for path in sorted(a.keys() | b.keys()):
                if a.get(path) != b.get(path):
                    lines.append(
                        f"{key}:{path}: {labels[0]}={_shown(worlds[0], key, path)!r} "
                        f"{labels[1]}={_shown(worlds[1], key, path)!r}"
                    )
        else:
            lines += [f"{key}{d}" for d in structural(a, b, labels)]
    return lines
