#!/usr/bin/env python3
"""FTL thread: never run the timing model on failed requests -- their
slba/nlb are stale (the base-FEMU bug only surfaces because the fallback
retry loop sends appends that fail zone checks)."""
p = "/home/ubuntu/lzd/FEMU_lzd/hw/femu/zns/zftl.c"
s = open(p).read()

old = """            ftl_assert(req);
            switch (req->cmd.opcode) {"""
new = """            ftl_assert(req);
            if (unlikely(req->status != NVME_SUCCESS)) {
                /* failed request: skip the timing model entirely (the
                 * error paths leave slba/nlb stale)                    */
                req->reqlat = 0;
                rc = femu_ring_enqueue(zns->to_poller[i], (void *)&req, 1);
                if (rc != 1) {
                    ftl_err("FTL to_poller enqueue failed\\n");
                }
                continue;
            }
            switch (req->cmd.opcode) {"""
assert s.count(old) == 1
s = s.replace(old, new)
open(p, "w").write(s)
print("ftl guard applied")
