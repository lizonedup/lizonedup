#!/usr/bin/env python3
"""dup_append: rewrite the command's slba/nlb to the device-picked zone so
zns_finalize_zoned_write advances the CORRECT zone's host-visible WP."""
p = "/home/ubuntu/lzd/FEMU_lzd/hw/femu/zns/zns.c"
s = open(p).read()

old = """    /* zns_finalize_zoned_write reads the command's nlb: keep it in sync
     * so the host-visible write pointer advances by the full page      */
    rw->nlb = cpu_to_le16(nlb - 1);
"""
new = """    /* zns_finalize_zoned_write locates the zone via the command's slba
     * and advances the host-visible WP from its nlb: rewrite both to
     * the device-picked zone (the host sent neither for a 0xB0).      */
    rw->slba = zone->d.zslba;
    rw->nlb = cpu_to_le16(nlb - 1);
"""
assert s.count(old) == 1
s = s.replace(old, new)
open(p, "w").write(s)
print("slba sync fix applied")
