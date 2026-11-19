#!/usr/bin/env python3
"""dup_append is page-granular: cdw10 = nr 4 KiB pages - 1 (like liobench),
not LBA count. Convert pages -> LBAs for the ZNS machinery."""
p = "/home/ubuntu/lzd/FEMU_lzd/hw/femu/zns/zns.c"
s = open(p).read()

old = """    NvmeRwCmd *rw = (NvmeRwCmd *)&req->cmd;
    uint32_t nlb = le32_to_cpu(cmd->cdw10) + 1;
    uint64_t data_size = zns_l2b(ns, nlb);
    uint64_t data_offset;
    NvmeZone *zone;
    NvmeZonedResult *res = (NvmeZonedResult *)&req->cqe;
    uint32_t lsh, zone_idx;
    uint16_t status;

    if (!lz_is_enabled(n)) {
        return NVME_INVALID_OPCODE | NVME_DNR;
    }
    if (nlb != 1) {
        return NVME_INVALID_FIELD | NVME_DNR;
    }
"""
new = """    NvmeRwCmd *rw = (NvmeRwCmd *)&req->cmd;
    uint32_t npages = le32_to_cpu(cmd->cdw10) + 1;   /* 4 KiB pages */
    uint32_t spp = 4096 / n->zns->lbasz;             /* LBAs per page */
    uint32_t nlb = npages * spp;                     /* in 512 B LBAs */
    uint64_t data_size = (uint64_t)npages * 4096;
    uint64_t data_offset;
    NvmeZone *zone;
    NvmeZonedResult *res = (NvmeZonedResult *)&req->cqe;
    uint32_t lsh, zone_idx;
    uint16_t status;

    if (!lz_is_enabled(n)) {
        return NVME_INVALID_OPCODE | NVME_DNR;
    }
    if (npages != 1) {
        return NVME_INVALID_FIELD | NVME_DNR;   /* page-granular primitive */
    }
"""
assert s.count(old) == 1, "dup_append header not found"
s = s.replace(old, new)

old2 = """    if (zns_l2b(ns, nlb) > ((uint64_t)n->page_size << n->zasl)) {
        return NVME_INVALID_FIELD | NVME_DNR;
    }"""
new2 = """    if (data_size > ((uint64_t)n->page_size << n->zasl)) {
        return NVME_INVALID_FIELD | NVME_DNR;
    }"""
assert s.count(old2) == 1, "zasl check not found"
s = s.replace(old2, new2)

open(p, "w").write(s)
print("page-granular fix applied")
