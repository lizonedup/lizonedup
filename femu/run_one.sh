#!/bin/bash
# run_one.sh CFG LZ_MODE LZ_R GEO WORKLOAD [BENCH_ARGS...]
# Boots the prepped VM; lz-autorun reads /dev/vdb for args, runs the bench,
# prints to serial; FEMU logs LZSTATS on stderr.  Both parsed from qemu.log.
set -u
CFG=$1; MODE=$2; R=$3; GEO=$4; WL=$5; shift 5
BARGS="$@"

# Location of the built FEMU tree; defaults to this script's directory.
ROOT="${FEMU_ROOT:-$(cd "$(dirname "$0")" && pwd)}"
BUILD=$ROOT/build
BASEIMG=$ROOT/lzguest.qcow2
OUT=$ROOT/lzruns/$CFG
PORT=$((22000 + $(echo "$CFG-lz" | cksum | cut -d' ' -f1) % 9000))

mkdir -p $OUT
IMG=$OUT/guest.qcow2
qemu-img create -f qcow2 -b $BASEIMG -F qcow2 $IMG > /dev/null 2>&1

if [ "$GEO" = "small" ]; then
    DEVSZ=1920; CH=2; LUN=2; PLANE=2; BLK=240; FT=2; ZPAGES=4096
else
    DEVSZ=2048; CH=8; LUN=4; PLANE=2; BLK=16; FT=2; ZPAGES=32768
fi

FEMU_OPT="-device femu,devsz_mb=${DEVSZ},namespaces=1"
FEMU_OPT="$FEMU_OPT,zns_num_ch=${CH},zns_num_lun=${LUN},zns_num_plane=${PLANE},zns_num_blk=${BLK}"
FEMU_OPT="$FEMU_OPT,zns_flash_type=${FT},femu_mode=3"
FEMU_OPT="$FEMU_OPT,lz_mode=${MODE},lz_r=${R},lz_sha_hw=${SHAHW:-1}"

# args disk: raw text consumed by /opt/lzautorun.sh via /dev/vdb
printf "%s\n" "-w $WL -z $ZPAGES $BARGS" > $OUT/args.raw
truncate -s 4k $OUT/args.raw

cd $BUILD
nohup ./qemu-system-x86_64 \
    -name "lz-$CFG" \
    -accel kvm \
    -cpu host -smp 4 -m ${MEM:-2G} \
    -device virtio-scsi-pci,id=scsi0 \
    -device scsi-hd,drive=hd0 \
    -drive file=$IMG,if=none,aio=native,cache=none,format=qcow2,id=hd0 \
    -drive file=$OUT/args.raw,if=virtio,format=raw,id=args0 \
    $FEMU_OPT \
    -device virtio-rng-pci \
    -net user,hostfwd=tcp::${PORT}-:22 \
    -net nic,model=virtio \
    -nographic > $OUT/qemu.log 2>&1 &
QPID=$!
echo "qemu pid=$QPID port=$PORT" > $OUT/info

for i in $(seq 1 720); do
    if grep -aq "RUN_RC=" $OUT/qemu.log 2>/dev/null; then break; fi
    if ! kill -0 $QPID 2>/dev/null; then break; fi
    sleep 5
done
sleep 8
kill $QPID 2>/dev/null
sleep 2

grep -a "LZSTATS" $OUT/qemu.log | tail -1 > $OUT/lzstats.txt
grep -a "^BENCH " $OUT/qemu.log | tail -1 > $OUT/bench_summary.txt
grep -a "AUTORUN_ARGS\|liobench wl=\|RUN_RC" $OUT/qemu.log | tail -3 >> $OUT/bench_summary.txt
echo "done $CFG" >> $OUT/info
