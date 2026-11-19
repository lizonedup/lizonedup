#!/usr/bin/env python3
"""dup_append must set the command's nlb field (zns_finalize_zoned_write
reads rw->nlb) and enforce the zone write boundary itself."""
p = "/home/ubuntu/lzd/FEMU_lzd/hw/femu/zns/zns.c"
s = open(p).read()

old = """    /* zone state + zasl checks, mirroring zns_check_zone_write(append) */
    status = zns_check_zone_state_for_write(zone);
    if (status) {
        return status | NVME_DNR;
    }
    if (data_size > ((uint64_t)n->page_size << n->zasl)) {
        return NVME_INVALID_FIELD | NVME_DNR;
    }
"""
new = """    /* zone state + zasl checks, mirroring zns_check_zone_write(append) */
    status = zns_check_zone_state_for_write(zone);
    if (status) {
        return status | NVME_DNR;
    }
    if (data_size > ((uint64_t)n->page_size << n->zasl)) {
        return NVME_INVALID_FIELD | NVME_DNR;
    }
    if (zone->w_ptr + nlb > zns_zone_wr_boundary(zone)) {
        return NVME_ZONE_BOUNDARY_ERROR | NVME_DNR;
    }

    /* zns_finalize_zoned_write reads the command's nlb: keep it in sync
     * so the host-visible write pointer advances by the full page      */
    rw->nlb = cpu_to_le16(nlb - 1);
"""
assert s.count(old) == 1
s = s.replace(old, new)
open(p, "w").write(s)
print("nlb sync fix applied")
