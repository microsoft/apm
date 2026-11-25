# Post-landing profiles of main (research lead; input to the 20:00 / 01:00 re-rank)

## 16:25 CET main 6472a85a (has #5330, #5333 via #5355, #5341, #5325, #5340; NOT #5351/#5354/#5361), cold t50, 1 profiled sample/OS (harness perf-research/arch-sol-attr-auth), runs L 45423090012 / W 45423192135
| cell | process CPU | children | top leaf kinds | top apm owners (self) | realpath calls |
|---|---|---|---|---|---|
| L anon | 7.04 s (A/A p50 ~8.0 s) | 0.02 s | python_other 2.77, file_io 0.96, yaml_emit 0.60, stat 0.55, import 0.52, realpath 0.51, walk 0.40 | _kernel_realpath 1.51, yaml_to_str 0.77, journal append_private 0.27, parse_primitive_file 0.20, _reflink_copy_file 0.19, content_hash._package_files 0.16 | 5,126 |
| L auth | 7.81 s | 5.00 s | python_other 2.87, file_io 0.97, stat 0.66, realpath 0.61, yaml_emit 0.59 | (git CLI path: B1/A' not engaged for auth) | - |
| W anon | 15.73 s (A/A p50 ~38.7 s incl children) | 12.97 s (B1 NOT engaged: #5351 not on main) | realpath 5.38, stat 2.69, python_other 1.64, file_io 1.55, thread_wait 1.11, walk 0.89, yaml_emit 0.69 | resolve_path 5.34, run_git_text 0.75, yaml_to_str 0.75, append_private 0.42, _package_files 0.38, _is_link 0.30 | 39,524 |
| W auth | 16.25 s | 16.89 s | realpath 5.52, stat 2.78, file_io 2.05 | - | - |
Readout: L = realpath family ~2.1 s (_kernel_realpath + leaf + missing) is still #1, then YAML emit ~0.8 s, journal ~0.3 s. W = #5351 not yet on main (git children 13 s); realpath 5.4 s (39.5k calls) + stat 2.7 s are the levers after it lands.

## 16:45 CET main d62aebe0 (post-#5351; NOT #5354/#5361/#5353/#5352), cold t50, A/A p50 incl children (3 pairs) + 1 profiled sample, runs L 45427128507 / W 45427206298
| cell | A/A p50 | process / children | top leaf kinds | top owners (self) | realpath calls |
|---|---|---|---|---|---|
| L anon | 10.5 s (slow runner class; earlier runs 7-8 s) | 9.47 / 0.02 | python_other 3.71, file_io 1.19, yaml_emit 0.86, realpath 0.77, stat 0.75, walk 0.66 | _kernel_realpath 2.37, yaml_to_str 1.06, append_private 0.28, _reflink_copy_file 0.21, _package_files 0.21 | 5,127 |
| L auth | 15.7 s | 10.39 / 5.93 | git clone x100 3.17, ls-remote x50 2.21 (git CLI path: A' not landed) | _kernel_realpath 2.38, yaml_to_str 1.08, run_git_text 0.36 | 5,471 |
| W anon | 15.9 s (B1 engaged) | 18.31 / 0.34 | realpath 6.52, stat 3.83, file_io 2.14, python_other 2.09, walk 0.97, yaml_emit 0.94 | resolve_path 6.56, yaml_to_str 1.05, _package_files 0.55, append_private 0.45, _is_link 0.36, inprocess _write_file 0.33 | 38,875 |
| W auth | 29.9 s | 18.66 / 18.44 | git clone x100 3.97, ls-tree x100 2.83, checkout x50 1.36, cat-file x50 0.88, ls-remote x50 0.72 (A'/lever C not landed) | resolve_path 6.52, run_git_text 1.00, yaml_to_str 0.94 | 39,469 |
Floor gap now: W anon 15.9 / 1.5 = 10.6x (target 7.5 s: needs -8.4 s); W auth 29.9 s (A' #5353 is worth ~-15 s); L anon ~7-10.5 s depending on runner (target 3.5 s).
Biggest remaining buckets: W anon realpath 6.5 s (38.9k calls) + stat 3.8 s = 10.3 s; W auth git children 18.4 s; L realpath family ~3.1 s + YAML emit ~1.1 s; L auth git children 5.9 s.

## 18:35 CET main c0296fab (post-#5354/#5361), cold t50, 3 pairs + 1 profiled sample; L 45446046183 (W 45446187888 queued)
| cell | paired CPU vs 2ff53cdf | process / children | top leaf kinds | top owners (self) | realpath calls |
|---|---|---|---|---|---|
| L anon | 0.923 (8.50 -> 7.16 s) | 7.92 / 0.03 | python_other 3.08, file_io 1.06, realpath 1.05, stat 0.74, import 0.62, walk 0.51, yaml_emit 0.05 | _kernel_realpath 1.69, append_private 0.30, parse_primitive_file 0.24, machine_yaml_to_str 0.23, _reflink_copy_file 0.21, portable_relpaths 0.21 | 7,727 (+2,600 portable_relpaths) |
| L auth | 0.952 (9.97 -> 9.77 s) | 8.87 / 6.00 | git clone x100 3.19, ls-remote x50 2.24 | _kernel_realpath 1.90, append_private 0.37, run_git_text 0.34 | 8,071 |
FINDING: #5354 portable_relpaths (paths.py:123) uses raw path.resolve(), bypassing resolve_path (kernel realpath / form B): +2,600 calls, +0.27 s L. Fix = resolve_path in both places (identical result). Sent to VP 18:35; default fold into Windows-paths PR (3c0e465f).
- 18:36 VP: raw .resolve() deliberate (M warm 1.033/1.087 on unconditional swap); L fixed in IFP #5372 (L cold 0.876); E1 folds W-only gated resolve_path into #5374. E1 corrected.
W (45446187888, OpenSSL+CA bench world, NOT GfW-default schannel):
| cell | paired CPU vs 2ff53cdf | process / children | top leaf kinds | top owners (self) | realpath calls |
|---|---|---|---|---|---|
| W anon | 0.931 (20.05 -> 18.64 s) | 18.77 / 0.31 | realpath 6.52, stat 3.67, python_other 2.86, file_io 2.84, walk 0.95, import 0.66 | resolve_path 6.13, append_private 0.59, portable_relpaths 0.58, _package_files 0.53, inprocess _write_file 0.44, _is_link 0.39 | 34,904 (resolve_for_containment 7.5k, ensure_path_within_resolved 5.1k, portable_relpath 4.7k, ensure_path_within 3.0k, portable_relpaths 2.6k) |
| W auth | 0.954 (29.51 -> 28.06 s) | 19.91 / 19.70 | realpath 6.55, stat 4.08, file_io 2.31, python_other 2.13, thread_wait 1.47 | resolve_path 6.22, run_git_text 1.25, append_private 0.61, portable_relpaths 0.53 | 35,498; git clone x100 4.11, ls-tree x100 2.95, checkout x50 1.73, cat-file 0.81, ls-remote 0.77 |
W-only portable_relpaths fold (#5374) worth ~0.55 s self on W. W anon remaining: realpath+stat 10.2 s is the dominant bucket (form B/A #5374); W auth: git children 19.7 s (A' #5353 / lever C).

## 20:25 CET main 9ba3589d (post-#5380) vs c0296fab, cold t50, 6 pairs + profile: L anon 0.980 / auth 0.991 [45468182412], realpath 7,727 -> 5,127 (portable_relpaths gone; #5354 regression fixed on L). W anon 1.004 / auth 1.002 [45468305112] (neutral; W fold pending in #5374, portable_relpaths 2,600 calls remain).

## 21:05 CET main 0f397094 (+#5367 small cars, #5370 B1 residual, #5364) vs 9ba3589d, t50, 6 pairs
L cold 0.924 / cold-auth 0.974 / warm 0.945 [45472888786]; W cold 0.930 / cold-auth 0.965 / warm 0.936 [45473003923]. Counts unchanged (warm spawns 116 -> 115).

## 21:15 CET main 0a2460a7 (+#5353 A', #5205) vs 0f397094, t50, 6 pairs
L: cold 0.994, cold-auth 0.530 (spawns 363->20, requests 250->100), warm-auth 0.796 (requests 100->50, ls-remote 50->0) [45475047324]. W: cold 1.004, cold-auth 0.456 (365->19), warm-auth 0.866 [45475248050]. W warm-auth children 3.53 s = cat-file x50 + ls-tree x50 (cache-hit verify via git) -> next warm candidate.

## 01:40 CET main 1e4a208e (+#5366 C, #5376 schannel B1, #5390, #5394) vs 0a2460a7, t50, 6 pairs
L: cold 0.905, cold-auth 0.909, warm-auth 0.931 [45514042000]. W default-schannel (JH harness 60799936): cold 0.461 (37.1->16.7 s, spawns 366->18), cold-auth 0.448 (spawns 369->20) [45514102212]. W OpenSSL profile 45514072084 pending.
W OpenSSL world same range [45514072084]: cold 0.978, cold-auth 0.977, warm-auth 0.964 (counts unchanged).
