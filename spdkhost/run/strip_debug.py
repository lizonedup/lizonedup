#!/usr/bin/env python3
"""Strip all temporary debug prints from the FEMU tree (final state)."""
import re

files = [
    "/home/ubuntu/lzd/FEMU_lzd/hw/femu/zns/lizone.c",
    "/home/ubuntu/lzd/FEMU_lzd/hw/femu/zns/zns.c",
    "/home/ubuntu/lzd/FEMU_lzd/hw/femu/zns/zftl.c",
    "/home/ubuntu/lzd/FEMU_lzd/hw/femu/nvme-io.c",
]

# femu_log("LZV ...", ...);  possibly multi-line, ends with ");\n"
pat = re.compile(r'[ \t]*femu_log\("LZ[VPSDFTQWM][^;]*?\);\n', re.DOTALL)

for p in files:
    s = open(p).read()
    before = s.count('femu_log("LZ')
    s2 = pat.sub("", s)
    after = s2.count('femu_log("LZ')
    open(p, "w").write(s2)
    print(f"{p.split('/')[-1]}: {before} -> {after} debug prints")

# sanity: no debug markers remain
for p in files:
    s = open(p).read()
    for marker in ("LZV", "LZP", "LZD ", "LZFT", "LZSQ", "LZDSM", "LZF ", "LZW"):
        assert marker not in s, (p, marker)
print("clean")
