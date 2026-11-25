# LANE N: native fs-op core, research plan (PERF-SOL, 25 Nov 03:40 CET; deadline FRI 27 Nov 03:30 CET)

## Where the CPU is at the stacked Python ceiling (L, run 45530101839, process 4.9 s on the bench runner)
Leaf kinds: realpath 19.9%, walk_scan 18.1%, file_io 15.3%, stat 11.6% (65% filesystem-syscall Python),
imports 13.3%. The top owners spread out; no single one is above 6%:
- package-tree walks: base_integrator.find_files_by_glob 5.7%, content_hash._package_files 5.0%,
  carried_license._skill_dirs 3.3%, agent_plugins.loader 1.5%, format_detection 1.1%, download_package 2.3%.
  The same ~50 package trees are walked 5-6 times: ~18% of L process CPU.
- direct Path.resolve/realpath callers the lexical ceiling does not cover: skill_preflight.find_unmanaged_conflicts 5.3%,
  skill_integrator._copy_skill_bundle 2.9%, discovery.find_primitive_files 2.8%, scope.is_user_scope_root 1.9%,
  mcp_config_view 1.9%: ~15%.

## 1. Which loop: chosen by a zero-cost oracle first (Wed, CI, no native code yet)
A native loop can't beat a zero-cost version of itself, so the bound is measured before any C is written.
- O-walk: one walk + lstat per package tree per invocation, served to every caller from an invocation memo
  (same file set, order and kinds; research only).
- O-resolve: the remaining direct Path.resolve/os.path.realpath callers on the install path made lexical
  (the ceiling already does resolve_path).
- Each is benched on top of the stacked ceiling head (L/M d3881480, W 6d67f5aa), cold t50, 6 pairs.
- Lane N takes the ONE oracle with the larger bound. If both bounds are < 0.5 s on L at 9V74, the lane is
  KILLED on Wed: no native work can clear the CEO gate.

## 2. Language and binding
- A C extension, one source file, CPython abi3 (limited API), built by setuptools on a research branch
  (`perf-research/lane-n-*`).
  - POSIX: openat/fdopendir/fstatat with O_NOFOLLOW and AT_SYMLINK_NOFOLLOW.
  - Windows: FindFirstFileExW (FIND_FIRST_EX_LARGE_FETCH) with reparse tags from the find data.
  - Realpath, if chosen: realpath(3) / GetFinalPathNameByHandleW on a handle opened without following.
- Not Rust/PyO3 for the bound: it would need a toolchain and build-backend change for a 48-hour upper bound.
  The SHIP memo covers Rust vs C, the abi3 wheel matrix (Linux x86_64/arm64 manylinux+musllinux, macOS
  universal2, Windows x64/arm64) and supply-chain review.
- Pure-Python fallback always present. The native module is optional: if the import fails, today's code runs.

## 3. Proving the W3d invariants (IR / MS / MD)
- Native returns DATA only (entries, kinds, modes, sizes, dev/ino, reparse flag). Every decision (refuse a
  link/junction, containment, mode policy) stays in the existing Python owners. So the invariant surface is
  "same data as the Python walk", not new policy.
- DIFFERENTIAL build: a flag runs Python and native side by side and raises on any difference (file set,
  order, kind, mode, size, reparse/symlink flag). Cells: symlink, junction (W), read-only, replaced-between,
  non-ASCII names, long paths (W \\?\), case-only rename (M/W). Lifecycle suites on L/W/M on the
  differential SHA, plus the A7 probe (uninstall, prune, update, install-warm, no-op). 0 differential errors
  is the gate.
- W3d ruling at design time (before code), with a cost line per call.

## 4. Bench
- CI only, bench_ci.sh, cold t50 (+ t10, warm), 6 pairs, L / W (default schannel) / M.
- Two comparisons: native vs the stacked Python ceiling (the lane deliverable), and native-on-main vs main
  (the shippable delta for the CEO gate: >= 0.5 s on L at 9V74, default-on, all 3 OS).
- Count oracle: Python walk/resolve calls on the chosen loop -> 0; native calls == package trees.

## 5. Timeline and kill rules
- Wed 12:00: oracles O-walk / O-resolve read. Pick the loop or KILL (< 0.5 s L bound).
- Wed 24:00: native prototype + differential green on L/M; W junction cells.
- Thu 12:00: paired benches L/W/M vs the ceiling and vs main.
- Thu 24:00: bound report.
- Fri 03:30: deadline. No measured bound means the lane is killed.
- Additional kills: native measured < 50% of its oracle (binding overhead eats it), or any differential error
  that can't be closed by data parity.
