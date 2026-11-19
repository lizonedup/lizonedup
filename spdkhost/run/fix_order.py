#!/usr/bin/env python3
"""Fix the qsg lifetime bug: lz_decide_write* must run BEFORE backend_rw
(which destroys the sglist). Also strips the temporary LZDBG lines."""
p = "/home/ubuntu/lzd/FEMU_lzd/hw/femu/zns/zns.c"
s = open(p).read()

# --- 1. strip debug lines -------------------------------------------------
strips = [
    '    femu_log("LZDBG map: status=%u nsg=%d prp1=0x%lx prp2=0x%lx\\n",\n'
    '             status, req->qsg.nsg, (unsigned long)req->cmd.dptr.prp1,\n'
    '             (unsigned long)req->cmd.dptr.prp2);\n',
    '    femu_log("LZDBG bisect1: after-route nsg=%d lsh=0x%x\\n", req->qsg.nsg, lsh);\n',
    '    femu_log("LZDBG bisect2: after-pick nsg=%d zone=%d\\n", req->qsg.nsg, zone_idx);\n',
    '    femu_log("LZDBG bisect3: after-open nsg=%d status=%u\\n", req->qsg.nsg, status);\n',
    '    femu_log("LZDBG dup_append: nsg=%d slba=%lu prp1=0x%lx psdt=%d nlb=%u\\n",\n'
    '             req->qsg.nsg, (unsigned long)req->slba,\n'
    '             (unsigned long)req->cmd.dptr.prp1, (int)req->cmd.psdt,\n'
    '             (unsigned)req->nlb);\n',
    '    femu_log("LZDBG post-decide: hit=%d mdlat=%lu\\n", req->lz_hit, (unsigned long)req->lz_md_lat_ns);\n',
]
for frag in strips:
    while frag in s:
        s = s.replace(frag, "")

# --- 2. native dup_append: decide BEFORE backend_rw ------------------------
old = """    req->is_write = 1;
    req->nlb = nlb;
    req->slba = zone->w_ptr;                 /* the assigned LBA */
    res->slba = zns_advance_zone_wp(ns, zone, nlb);

    /* data lands in the memory backend: reads stay correct even for
     * descriptor-backed slots; the NAND program is suppressed later in
     * the FTL thread when req->lz_hit is set.                              */
    data_offset = zns_l2b(ns, req->slba);
    backend_rw(n->mbe, &req->qsg, &data_offset, req->is_write);
    zns_finalize_zoned_write(ns, req, false);
    n->zns->active_zone = zone_idx;

    req->lz_md_lat_ns = lz_decide_write(n, req, zone_idx);
    lz_note_dup_append(n);
    return NVME_SUCCESS;"""
new = """    req->is_write = 1;
    req->nlb = nlb;
    req->slba = zone->w_ptr;                 /* the assigned LBA */
    res->slba = zns_advance_zone_wp(ns, zone, nlb);

    /* dedup decision FIRST: it reads the payload through the qsg, and
     * backend_rw destroys the sglist when it copies the data           */
    req->lz_md_lat_ns = lz_decide_write(n, req, zone_idx);

    /* data lands in the memory backend: reads stay correct even for
     * descriptor-backed slots; the NAND program is suppressed later in
     * the FTL thread when req->lz_hit is set.                              */
    data_offset = zns_l2b(ns, req->slba);
    backend_rw(n->mbe, &req->qsg, &data_offset, req->is_write);
    zns_finalize_zoned_write(ns, req, false);
    n->zns->active_zone = zone_idx;

    lz_note_dup_append(n);
    return NVME_SUCCESS;"""
assert s.count(old) == 1, "native dup_append block not found"
s = s.replace(old, new)

# --- 3. fallback hook in zns_nvme_rw: move BEFORE backend_rw ---------------
old2 = """    req->slba = slba;
    req->status = NVME_SUCCESS;
    req->nlb = nlb;

    backend_rw(n->mbe, &req->qsg, &data_offset, req->is_write);

    if(req->is_write)
    {
        zns_finalize_zoned_write(ns, req, false);
        if (append && lz_fallback_active(n)) {
            /* fallback dedup: dedup on the host-routed zone_append; the
             * host-chosen zone is the lookup hint (probed first)      */
            uint32_t hint = zns_zone_idx(ns, zone->d.zslba);
            req->lz_md_lat_ns = lz_decide_write_hint(n, req, hint, (int)hint);
        }
    }
"""
new2 = """    req->slba = slba;
    req->status = NVME_SUCCESS;
    req->nlb = nlb;

    /* spdkhost: fallback dedup must read the payload through the qsg
     * BEFORE backend_rw destroys the sglist                              */
    if (req->is_write && append && lz_fallback_active(n)) {
        uint32_t hint = zns_zone_idx(ns, zone->d.zslba);
        req->lz_md_lat_ns = lz_decide_write_hint(n, req, hint, (int)hint);
    }

    backend_rw(n->mbe, &req->qsg, &data_offset, req->is_write);

    if(req->is_write)
    {
        zns_finalize_zoned_write(ns, req, false);
    }
"""
assert s.count(old2) == 1, "fallback hook block not found"
s = s.replace(old2, new2)

open(p, "w").write(s)
print("reordered + debug stripped")
