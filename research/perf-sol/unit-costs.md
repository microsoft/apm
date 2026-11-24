# Unit costs on hosted runners (run 45359600677, scripts/perf/sol_ubench.py @ perf-research/arch-sol-ubench; process CPU p50 of 5 repeats; Windows timer granularity 15.6 ms -> sub-ms rows read 0)
| op | Linux us | Windows us |
|---|---|---|
| write 1 KB: Path.write_bytes | 28 | 89 |
| write 1 KB: os.open O_EXCL|O_NOFOLLOW (dir fd on posix) | 16 | 67 |
| write: mkstemp + write + os.replace | 36 | 134 |
| write: realpath containment + atomic (today-like) | 75 | 223 |
| materialize: os.link from store | 20 | 56 |
| materialize: shutil.copyfile / copy2 | 53 / 67 | 179 / 190 |
| os.path.realpath (depth ~8) | 33.5 | 53 |
| lstat / stat | 2.5 | 3.1 |
| read_bytes 1 KB | 7.9 | 25 |
| sha256 1 KB object / per MB | 0.8 / 520 | ~0 / 977 |
| scandir 28-file dir | 15 | 22 |
| git spawn trivial: parent + child | 0.11 + 0.9-1.0 ms | 2.0-2.3 + 18-19 ms |
| python -c pass (interpreter boot) | 9.3 ms | 18.8 ms |
| TLS new conn + GET: local / github.com | 1.27 / 4.7 ms | 0.94 / 5.9 ms |
| keep-alive GET: local / github.com | 0.40 / 1.12 ms | <15 ms (timer) |
| ssl.create_default_context() system CAs | 11.8 ms | 94 ms |
Floor arithmetic, t50 (1,400 files): O_EXCL writes 0.02 s L / 0.09 s W; realpath x39.8k today = 1.33 s L / 2.11 s W pure syscall+Python (measured share 4.2 / 5.1 s incl. callers). 100 pooled github requests ~0.12 s L. 642 git spawns x (1.1 ms L / 21 ms W) = 0.7 s L / 13.5 s W.
