# Windows paths re-open prep (PERF-SOL, 14:4x CET)

## Sizing on the landing stack 8eca5c07 (main + #5330 + #5351 + #5352 + #5353 + #5354), W cold t50
Harness perf-research/arch-sol-attr (realpath calls by path location x innermost apm caller, skipping the realpath helper frame).

| location | anonymous calls (45401938583) | auth calls (45401973325) | holdable under A7? |
|---|---|---|---|
| project/apm_modules (package dirs) | 17,238 (49%) | 17,238 (49%) | YES, after the package's final placement (this design) |
| project deploy targets (.github/... etc.) | 11,135 (32%) | 11,135 (32%) | yes (already held in F11's form) |
| project root and parents ("other") | 6,004 (17%) | 6,006 (17%) | yes (the root is held once) |
| APM cache | 450 (1%) | 450 (1%) | no (APM renames it) |
| staging (.inc./.tmp) | 50 (0%) | 50 (0%) | no |
| APM_HOME | 26 | 26 | no |
| total | 34,903; realpath leaf 7.47 s of 20.47 s | 34,905; 6.47 s of 20.30 s | ~98% holdable |

Top callers:
- resolve_for_containment: 4.3k deploy, 3.2k root.
- ensure_path_within_resolved: 3.8k in apm_modules.
- portable_relpath(s): 5.5k in apm_modules.
- skill_adoption._inactive_skill_roots: 2.1k in deploy.
- find_files_by_glob: 1.8k in apm_modules.
- init_link_resolver: 1.6k.

Why F11's faithful form gave only 0.996: it held deploy targets and the root but never apm_modules, which is 49% of calls.

Ceiling: the A7-compatible form reaches ~98% of the calls the memo ceiling removes, so its ceiling is about the memo's: 0.716-0.728 on 0cee8358, and W auth 0.908.
- Expected faithful W anon on post-queue main: ~0.80-0.85. The per-call cost left is one lstat of the leaf (~3 us) instead of CreateFileW; plus one CreateFileW per held dir.
- Target (VP): <= 0.85.

## Design for W3d: hold package dirs only after their final placement (A7-compatible form A)
A package directory under `apm_modules/<owner>/<repo>` becomes HOLDABLE at its final placement: the moment the downloader returns it, after materialize or copy into the target. Nothing on the cold path renames it after that point; cache checkouts use `.inc.` staging plus an atomic rename, and those dirs are never held. From then on, the first resolve under it opens the directory with CreateFileW: FILE_SHARE_READ|WRITE and no FILE_SHARE_DELETE (A1/A2); BACKUP_SEMANTICS|OPEN_REPARSE_POINT (A3), refusing a reparse attribute; final path read from that same handle. Every later resolve under it costs one lstat of the leaf plus a reparse check, and a missing leaf joins the held parent. The holdable set is an explicit registry: the project root, deploy target dirs, and package dirs registered at final placement. Path prefixes are not enough. APM's own destructive operations release before they act (A7): every rename or delete of a directory goes through `file_ops.robust_rmtree` / the atomic-replace helpers, which call `release_held(under=path)`. That closes every held handle at or below the path and drops its entries, so the next resolve re-proves from scratch. No other code path may rename or delete a registered dir; a static guard and a cell prove it. Required cells:
- (1) A differential vs Path.resolve under APM_DEBUG.
- (2) Junction swap of a held package dir from another process is refused (sharing violation), and that install still completes.
- (3) uninstall, prune, update, a re-install that replaces a package, and a failed install with rollback: all with 0 sharing violations.
- (4) A case-collision refusal.
- (5) Handles are released at command end (count back to 0).
Anything not registered (cache, staging, APM_HOME, temp) keeps form B.

## Plan (kill 16:30 CET)
1. Base: current main (has #5330) + #5351 head merged; SHAs stated in the report. Re-check on main once #5351 merges.
2. Build on the F11 code (perf-research/arch-win-paths-on-5354 06e728ad):
   - Registry instead of prefix rules.
   - Register at the downloader's final placement.
   - Release hook in the rmtree/replace helpers.
3. Bench: W anon + auth cold t50, 6 pairs, L/M sanity, differential SHA. Probe: uninstall, prune, update, warm, no-op.
