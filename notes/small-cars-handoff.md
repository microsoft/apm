# Small cars: hand-off (PERF-BETS -> F&C or L5)

Branch: perf-research/arch-small-cars @ 13aeab94, on CURRENT main a235f590.
- #5333 (realpath kernel) is NOT in: still OPEN.
- #5330 (B1 in-process fetch) is NOT in: still OPEN. The B1 pack+idx car (W3d K1-K6) therefore CANNOT ride on main. It folds into #5330 or its Windows follow-up; the research version is perf-research/arch-writes-faithful bead9b9f.
- Population: all. Diff: 4 files, +98/-3 src lines.

## Car 1: S1, skip the second commit-gate planning pass when no gated dest dir holds a file
- What: commit_gate._refuse_unproven_overwrites (:600) returns early (:616) when _gated_dest_dirs_empty (:634) proves that every dest dir any gated primitive (prompts/agents/commands/instructions) would deploy into is absent or empty.
  - Dirs come from each plan's own target_selection.targets, as _planned_keeps visits them, at root_dir|deploy_root/subdir. Cost: one scandir each.
  - Then no planned write can meet an existing file, so _planned_keeps provably returns [] and overwrite_preflight_refused = False.
  - Any doubt plans as today: user scope, copilot-app target, absolute or managed (external) deploy root, unreadable dir, or a non-empty dir.
- W3d: S1 (row w3d:ruling:n1-ceiling). The OverwriteGate per-file backstop and the O_EXCL/journal rollback (I7) are unchanged.
- Cells to add (F&C):
  - (a) fresh project: refusal parity with today when a gated dest file pre-exists (the proof answers False);
  - (b) an empty pre-created .github/instructions still skips;
  - (c) a package-level `targets:` differing from the project's.
- Old research measure: L t50 ~0.93 together with S5 (vs f251f785, run 45384894338).

## Car 2: S5, one private journal fd per store
- What:
  - deploy_snapshot.DeployJournal._append_line (:156-167) opens the journal once per store via the new journal_modes.open_private_append (:221). The flags are unchanged: O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW, 0600 + fchmod. It then os.write()s one line per record.
  - append_private (:235) now uses the same helper.
  - The fd closes in discard() (:366) and BEFORE the _retain rename (:331): Windows cannot rename a directory holding an open file.
- W3d: S5. Same record order (one write per line, O_APPEND). Same durability (no fsync before or after; one write syscall per line as before).
- Removes ~1,795 open/close per t50 install.
- Cells: the existing tests/integration/test_abandoned_deploy_journal_replay_lifecycle.py (kill mid-deploy, replay), plus a retain-on-Windows cell.

## Car 3: GC gen-0 threshold. PROCESS-WIDE BEHAVIOUR, named explicitly for W3d/QL
- What: entrypoint.main (:8-12) calls gc.set_threshold(100_000, 50, 50) for the whole apm process: every command, every thread.
- Effect: young collections are 1/140 as frequent. Cyclic garbage lives longer within one command (bounded by the 100k-allocation window), so peak RSS may rise slightly; it was NOT measured.
- No security surface: no I/O, no trust decision.
- Measured alone: t50 L GC 685 collections / 0.307 s -> 39 / 0.065 s; L 0.985, W 0.993 (runs 45387568098 / 45387588614).

## Measurements (combined head 13aeab94 vs main a235f590; 6 pairs; paired CPU incl. children)
Runs: L 45389129843, macOS 45389161218, W 45389181318. Engagement profiles (writes-attr harness): base 45389212701, new 45389237880. Cells: L 45389591021, W 45389631111.

### Combined 13aeab94 vs a235f590 (6 pairs, paired new/base; CPU p50 s)
| OS | cold t10 | cold t50 | warm t10 | warm t50 | run |
|---|---|---|---|---|---|
| Linux | 0.972 | 16.45 -> 15.98, 0.960 | 0.941 | 12.05 -> 11.30, 0.943 | 45389129843 |
| macOS | 0.910 | 18.23 -> 18.32, 0.954 | 0.899 | 9.28 -> 9.01, 0.915 | 45389161218 |
| Windows | 0.962 | 44.13 -> 42.29, 0.966 | 0.946 | 23.07 -> 21.47, 0.942 | 45389181318 |
- Engagement, per car (L t50 profiles; base 45389212701 -> new 45389237880):
  - S1: _planned_keeps 2.10 s -> absent.
  - S5: journal write-opens 1,804 -> 8.
  - GC: 539 collections / 0.155 s -> 39 / 0.058 s.
  - Spawns 673 -> 673.
- GATE (VP 13:16): Linux cold t50 must be >= ~10%. It is 4.0% (0.960) -> FAIL, so the cars stay PARKED. Every cell is <= 1.010, so there is no regression; warm gains ~6%.
- Cells at 13aeab94:
  - L 261 passed / 4 skipped x3 [45389591021].
  - W 12 FAILED x3 [45389631111]: the held journal fd blocked an in-process replay's rename/delete (PermissionError).
  - FIXED at 21269a24: S5 is POSIX-only, so Windows keeps open-per-line. Re-run: cells W 45391055848 / L 45391099776; W bench 45391124928.
