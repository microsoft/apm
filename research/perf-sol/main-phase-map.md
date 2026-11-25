# Phase map re-grounded on MAIN 1e4a208e (PERF-SOL, 25 Nov ~02:20 CET). Replaces the e62f520f stack map.
install-cold t50, paired 6 pairs, anon; W = default schannel world (main harness). ~s at 9V74 (W ~13 s, L ~6 s).
| deleted (main) | W | L | stack W / L | runs W / L |
|---|---|---|---|---|
| whole integrate | 0.499 | 0.551 | 0.415 / 0.402 | 45520602660 / 45520678153 |
| skills family | 0.854 | 0.871 | 0.835 / 0.865 | 45522833176 / 45522946057 |
| instructions | 0.927 | 0.947 | 0.947 / 0.944 | 45522852235 / 45522962400 |
| prompts+commands | 0.944 | 0.956 | 0.947 / 0.953 | 45522894745 / 45522995966 |
| agents | 0.962 | 0.973 | 0.955 / 0.965 | 45522874001 / 45522981253 |
| shared remainder (integrate - families) | 18.8% | 19.6% | 27% / 32.5% | |
| records sub-block | 0.914 | 0.955 | 0.968 / 0.953 | 45522911104 / 45523017280 |
| skill preflight/ownership | 0.959 | 0.968 | 0.941 / 0.955 | 45522927001 / 45523033171 |
| lockfile+ledger+journal | 0.789 | 0.770 | 0.734 / 0.729 | 45520632797 / 45520705563 |
| policy/audit/scan | 0.961 | 0.980 | 0.969 / 0.975 | 45520658679 / 45520729238 |
| integrate on install-warm | 0.523 | 0.512 | | same |
Profiles (main, 1 sample; cpu_attr wrapper bypasses the fast path, so locked-warm/no-op rows show the FULL pipeline):
W cold 16.8 s: path containment 6.2 | writes 2.3 | rest 2.1 | discovery 1.7 | hash 1.0  [45520749499]
L cold 7.5 s: rest 1.3 | containment 1.3 | writes 1.2 | discovery 1.0 | parse 0.8  [45520769932]
W warm: +3.3 s git children (ls-tree link plan etc.).
Journey decisions on main [45523138212]: noop "no-op (desired state already deployed)" (proof run; first unchanged install after a full install, fresh APM home per sample); locked-warm "applied a verified diff (667 files)"; frozen-warm "full run (audit: kept-mcp-entries)" (diff_apply.py:480 <- ci_checks._kept_mcp_entries); warm "full run (no lockfile)".
frozen-warm kept-mcp deletion (afe0c87a): L frozen-warm 0.995, locked-warm 1.004 [45524432672] -> no cost in bench world; the decline seen in the sequential trace was real kept state from earlier journeys.
