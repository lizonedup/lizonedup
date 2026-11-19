#!/usr/bin/env python3
"""
apply_patch.py -- integrate LiZoneDedup (spdkhost edition) into a FEMU tree.

Usage: python3 apply_patch.py <FEMU_TREE>

Two inputs live next to this script:
  lizone.c / lizone.h : the controller-side dedup module (copied verbatim
                        into hw/femu/zns/)
  tree.patch          : unified diff (relative paths) wiring the module
                        into the build + the NVMe/ZNS hooks

Refuses to run twice (checks for hw/femu/zns/lizone.h).  Apply the patch
with `patch -p1` semantics; a hunk failure aborts loudly.
"""
import os
import shutil
import subprocess
import sys

TREE = sys.argv[1] if len(sys.argv) > 1 else "."
HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    if os.path.exists(os.path.join(TREE, "hw/femu/zns/lizone.h")):
        raise SystemExit("already patched (lizone.h present); refusing")

    # 1. overlay the lizone module
    shutil.copy(os.path.join(HERE, "lizone.c"),
                os.path.join(TREE, "hw/femu/zns/lizone.c"))
    shutil.copy(os.path.join(HERE, "lizone.h"),
                os.path.join(TREE, "hw/femu/zns/lizone.h"))
    print("copied lizone.c/h -> hw/femu/zns/")

    # 2. apply the wiring patch (paths are relative to the tree root)
    r = subprocess.run(["patch", "-d", TREE, "-p1", "--no-backup-if-mismatch",
                        "-i", os.path.join(HERE, "tree.patch")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("patch failed:\n" + r.stdout + r.stderr)
    print(r.stdout.strip())
    print("all patches applied")


if __name__ == "__main__":
    main()
