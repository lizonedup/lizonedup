#!/bin/bash
# boot_zs.sh NAME LZ_MODE LZ_FALLBACK ZS_MB [MEM_GB]
#
# Boots the lzd guest with a GB-scale zone geometry.  Zone size =
# ch x lun x plane x num_page x 16 KiB; num_page is capped at 65536
# (PG_BITS), so zones above 1 GiB span multiple planes:
#   ZS_MB=1024 -> 1 plane  x 65536 pages  = 1 GiB zones
#   ZS_MB=2048 -> 2 planes x 65536 pages  = 2 GiB zones
# Device = 8 zones (enough for the 32 route groups to share + rotate).
set -u
NAME=$1; MODE=$2; FB=$3; ZS=$4; MEM_GB=${5:-6}

ROOT=~/lzd
QEMU=$ROOT/FEMU_lzd/build/qemu-system-x86_64
BASE=$ROOT/lzd-base.qcow2
OUT=$ROOT/runs/$NAME
PORT=$((23000 + $(echo "$NAME" | cksum | cut -d' ' -f1) % 2000))

case $ZS in
    1024) PLANE=1; BLK=8 ;;   # 8 GiB device, 8 zones
    2048) PLANE=2; BLK=8 ;;   # 8 GiB device, 4 zones (num_page caps at 65536)
    *) echo "ZS_MB must be 1024 or 2048"; exit 1 ;;
esac
DEVSZ=$(( ZS * BLK / PLANE ))    # MB (a zone spans PLANE planes)

mkdir -p "$OUT"
qemu-img create -f qcow2 -b "$BASE" -F qcow2 "$OUT/guest.qcow2" > /dev/null

OPT="-device femu,devsz_mb=${DEVSZ},namespaces=1"
OPT="$OPT,zns_num_ch=1,zns_num_lun=1,zns_num_plane=${PLANE},zns_num_blk=${BLK}"
OPT="$OPT,zns_flash_type=2,femu_mode=3"
OPT="$OPT,lz_mode=${MODE},lz_r=2,lz_sha_hw=1,lz_fallback=${FB}"

cd "$ROOT/FEMU_lzd/build"
nohup "$QEMU" \
    -name "lzd-$NAME" \
    -accel kvm -cpu host -smp 8 -m ${MEM_GB}G \
    -drive file="$OUT/guest.qcow2",if=virtio,format=qcow2 \
    $OPT \
    -net user,hostfwd=tcp::${PORT}-:22 \
    -net nic,model=virtio \
    -nographic > "$OUT/qemu.log" 2>&1 &
echo $! > "$OUT/qemu.pid"
echo "$PORT" > "$OUT/port"
echo "boot: name=$NAME mode=$MODE fallback=$FB zs=${ZS}MB devsz=${DEVSZ}MB port=$PORT"
