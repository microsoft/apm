# Phase map on MAIN 1e4a208e (SoL, 02:20 CET 11-25), install-cold t50, 6 pairs, anon, W = default schannel. ~s at 9V74 (W ~13 s, L ~6 s).
| deleted | W ratio | W %/s | L ratio | L %/s | runs W / L |
|---|---|---|---|---|---|
| whole integrate | 0.499 | 50% / 6.5 | 0.551 | 45% / 2.7 | 45520602660 / 45520678153 |
| skills | 0.854 | 14.6% / 1.9 | 0.871 | 12.9% / 0.8 | 45522833176 / 45522946057 |
| instructions | 0.927 | 7.3% / 0.95 | 0.947 | 5.3% / 0.3 | 45522852235 / 45522962400 |
| prompts+commands | 0.944 | 5.6% / 0.7 | 0.956 | 4.4% / 0.3 | 45522894745 / 45522995966 |
| agents | 0.962 | 3.8% / 0.5 | 0.973 | 2.7% / 0.2 | 45522874001 / 45522981253 |
| sum families | | 31.3% / 4.1 | | 25.3% / 1.5 | |
| shared remainder | | 18.8% / 2.4 | | 19.6% / 1.2 | |
| . records | 0.914 | 3-9% / ~1.1 | 0.955 | 4.5% / 0.3 | 45522911104 / 45523017280 |
| . skill preflight/ownership | 0.959 | 4.1% / 0.5 | 0.968 | 3.2% / 0.2 | 45522927001 / 45523033171 |
| lockfile+ledger+journal | 0.789 | 21% / 2.7 | 0.770 | 23% / 1.4 | 45520632797 / 45520705563 |
| policy/audit/scan | 0.961 | 3.9% / 0.5 | 0.980 | 2.0% / 0.1 | 45520658679 / 45520729238 |
| integrate on warm | 0.523 | | 0.512 | | same |
Residual (boot+resolve+fetch+glue) ~25%. Per-shard +-2%.

CORRECTION (E2, 02:46): the lockfile+ledger+journal row is CONFOUNDED (deleting lockfile build/write makes downstream MCP/LSP update_lockfile, post_deps_local, claude_plugins skip). lockfile.py ~3% L inclusive [45521813477]; real = ledger _trusted_hash lstat chain (~3.3k lstat, W 0.91 s, containment family) + journal (L ~7%).
