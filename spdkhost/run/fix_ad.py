#!/usr/bin/env python3
"""FTL DSM check: Deallocate is cdw11 bit 2 (NVME_DSMGMT_AD)."""
p = "/home/ubuntu/lzd/FEMU_lzd/hw/femu/zns/zftl.c"
s = open(p).read()
old = """                    if (lz_is_enabled(n) && req->dsm_ranges &&
                        (req->dsm_attributes & 0x1)) {"""
new = """                    if (lz_is_enabled(n) && req->dsm_ranges &&
                        (req->dsm_attributes & 0x4)) { /* NVME_DSMGMT_AD */"""
assert s.count(old) == 1
s = s.replace(old, new)
open(p, "w").write(s)
print("ok")
