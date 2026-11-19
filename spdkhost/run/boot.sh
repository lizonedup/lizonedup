#!/bin/bash
# boot.sh -- boot the lzd guest with the FEMU lizonedup ZNS device.
#
# usage: boot.sh NAME LZ_MODE LZ_FALLBACK LZ_MAX_OPEN [EXTRA_QEMU...]
#   NAME       run name (output dir + ssh port derived)
#   LZ_MODE    lizone mode (6 = FULL design; 0 = plain ZNS)
#   LZ_FALLBACK 1 = host-routed zone_append dedup (fallback path)
#   LZ_MAX_OPEN 0 = unlimited AOR, else the open/active zone limit
#
# Env: MEM (default 4G), SMP (default 8), DEVSZ_MB (96), NBLK (48)
set -u
NAME=$1; MODE=$2; FB=$3; MAXOPEN=$4; shift 4

ROOT=~/lzd
QEMU=$ROOT/FEMU_lzd/build/qemu-system-x86_64
BASE=$ROOT/lzd-base.qcow2
OUT=$ROOT/runs/$NAME
PORT=$((23000 + $(echo "$NAME" | cksum | cut -d' ' -f1) % 2000))
MEM=${MEM:-4G}; SMP=${SMP:-8}
DEVSZ=${DEVSZ_MB:-96}; NBLK=${NBLK:-48}

mkdir -p "$OUT"
qemu-img create -f qcow2 -b "$BASE" -F qcow2 "$OUT/guest.qcow2" > /dev/null

OPT="-device femu,devsz_mb=${DEVSZ},namespaces=1"
OPT="$OPT,zns_num_ch=1,zns_num_lun=1,zns_num_plane=1,zns_num_blk=${NBLK}"
OPT="$OPT,zns_flash_type=2,femu_mode=3"
OPT="$OPT,lz_mode=${MODE},lz_r=2,lz_sha_hw=1,lz_fallback=${FB}"
[ "$MAXOPEN" != "0" ] && OPT="$OPT,lz_max_open_zones=${MAXOPEN}"

cd "$ROOT/FEMU_lzd/build"
nohup "$QEMU" \
    -name "lzd-$NAME" \
    -accel kvm -cpu host -smp "$SMP" -m "$MEM" \
    -drive file="$OUT/guest.qcow2",if=virtio,format=qcow2 \
    $OPT "$@" \
    -net user,hostfwd=tcp::${PORT}-:22 \
    -net nic,model=virtio \
    -nographic > "$OUT/qemu.log" 2>&1 &
echo $! > "$OUT/qemu.pid"
echo "$PORT" > "$OUT/port"
echo "boot: name=$NAME mode=$MODE fallback=$FB maxopen=$MAXOPEN port=$PORT pid=$(cat "$OUT/qemu.pid")"
