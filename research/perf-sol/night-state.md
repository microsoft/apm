# PERF-SOL state at night-mode stop (21:30 CET, 2026-11-24). Resume 07:00 CET.

All heads are on the plain stack e62f520f unless noted. Pop: anon, t50, 6 pairs, rtt 30. Tool: tools/phase_del.py <run ids>.

## Landed, NOT yet reported to the lead
Locked-warm heads (cold / warm / locked-warm paired):
- warn 368f20e0 (top-level scalar memo uncapped): L 1.002/0.996/0.998 [45474986775]; W 0.992/0.999/1.003 [45475039791] -> nothing.
- nocopy 58b99387 (LockFile.read memo, no copy; E2 lane upper bound): L 0.976/0.984/0.955 [45475113029]; W 0.990/0.992/0.982 [45475164406].
  = E2 ceiling ~2.5% cold, ~4.5% locked-warm on L; ~1-2% W. E2 NOT yet told (lead asked to cc E2).
- Earlier: cacheverify 81184d9a L 0.973/0.952/0.941, W 0.976/0.970/0.972; reach 88924b5a ~1%; lockread (deepcopy) worse 1.04.
- No single head explains locked minus warm (+20-29%). NEXT: whole-phase deletion on the locked path (verify-pins phase vs resolve-skip), per the lead.
Shared-remainder / skill sub-splits (install-cold t50):
- records 45587862 (journal append + entry_state + ledger.record + deploy_store.put/put_file): W 0.905 [45476295384], L 0.946 [45476597311]  -> ~1.1 s W, ~0.3 s L. BIGGEST named sub-block (>= 1 s W bar).
- skpreflight 66b252a9 (find_unmanaged_conflicts + _ownership_maps + report_stale_kept_copies): W 0.941 [45476385376], L 0.955 [45476716940] -> ~0.7 s W, 0.27 s L.
- skpre cde547ee (skill_tree_baselines + tree_file_hashes): W 1.009 [45476519018], L 1.001/0.998 [45476876454, 45477238752] -> ~0.
- sklinks a1d2c71d: first runs FAILED (SHA not yet pushed); re-requested: perf-bench/a1d2c71d-1795551885-4207fb (W), -1795551899-b1f963 (L).
Warm cache-hit cell (base be4e127d = main 0a2460a7 + #5366 b2e72a4a; stub f552839c skips _sparse_checkout_is_complete ls-tree + require_verified_copy cat-file):
- L warm 0.960, warm-auth 0.953 [45478299478]; W in flight 45478226232. Spawn-count oracle not yet read (perf-bench-result summary "spawns" column).

## In flight at stop (read at 07:00)
- W warm cache-hit 45478226232
- hooks/MCP exec-fixture cell (harness perf-research/arch-exec-fixture, fixture tree-50-exec): hooks deleted c28d6620 [45479951765], MCP deleted d4dd20a9 [45480021216]
- sklinks W/L (branches above; find ids via gh run list --workflow perf-bench.yml)

## Next steps at 07:00
1. Read the in-flight runs; send the lead: records = 1.1 s W named sub-block; skpreflight 0.7 s W; skill sub-split; warm cache-hit remainder (bar >= 1 s W warm); hooks/MCP fixture cell.
2. cc E2 (ab60b9b2) with the nocopy upper bound.
3. Locked-warm: whole-phase deletion (verify-pins vs resolve-skip).
4. Record F20/F21 in findings.md.
