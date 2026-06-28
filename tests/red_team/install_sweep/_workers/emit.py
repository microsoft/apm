import sys

_ = sys.stdin.read()
n = int(sys.argv[1])
chunk = "A" * (1 << 20)
w = sys.stdout.write
full, rem = divmod(n, len(chunk))
for _ in range(full):
    w(chunk)
w("A" * rem)
sys.stdout.flush()
