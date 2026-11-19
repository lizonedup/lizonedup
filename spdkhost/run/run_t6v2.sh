#!/bin/bash
# run_t6v2.sh -- crash/durability validation on the gpu host.
# A: process crash (kill -9 lzdtest mid-write) -> t6p2 verifies ACKed set
# B: VM crash (kill QEMU mid-write) -> reboot -> device sane + t1 passes
set -u
ROOT=~/lzd
KEY=$ROOT/lzd_key
SSH="ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15"

bash $ROOT/scripts/boot.sh t6v2 6 0 0 > /dev/null
P=$(cat $ROOT/runs/t6v2/port)
for i in $(seq 1 60); do $SSH -p $P ubuntu@127.0.0.1 true 2>/dev/null && break; sleep 2; done

echo "--- T6A: process crash ---"
$SSH -p $P ubuntu@127.0.0.1 '(setsid nohup sudo stdbuf -oL /usr/local/bin/lzdtest -m native -t t6p1 > /home/ubuntu/t6a.txt 2>&1 < /dev/null &); echo started' 2>/dev/null
sleep 4
$SSH -p $P ubuntu@127.0.0.1 'sudo pkill -9 lzdtest; sleep 1; sudo rm -f /var/tmp/spdk_pci_lock*; sudo stdbuf -oL /usr/local/bin/lzdtest -m native -t t6p2 >> /home/ubuntu/t6a.txt 2>&1; grep -a "T6P2\|LZDTEST\|vrdbg" /home/ubuntu/t6a.txt' 2>/dev/null | grep -av Warning

echo "--- T6B: VM crash + reboot ---"
$SSH -p $P ubuntu@127.0.0.1 '(setsid nohup sudo stdbuf -oL /usr/local/bin/lzdtest -m native -t t6p1 > /home/ubuntu/t6b_run.txt 2>&1 < /dev/null &); echo started' 2>/dev/null
sleep 3
kill -9 $(cat $ROOT/runs/t6v2/qemu.pid) 2>/dev/null
sleep 3
# reboot the same disk
cd $ROOT/FEMU_lzd/build
nohup ./qemu-system-x86_64 -name lzd-t6v2-b \
    -accel kvm -cpu host -smp 8 -m 4G \
    -drive file=$ROOT/runs/t6v2/guest.qcow2,if=virtio,format=qcow2 \
    -device femu,devsz_mb=96,namespaces=1,zns_num_ch=1,zns_num_lun=1,zns_num_plane=1,zns_num_blk=48,zns_flash_type=2,femu_mode=3,lz_mode=6,lz_r=2,lz_sha_hw=1,lz_fallback=0 \
    -net user,hostfwd=tcp::${P}-:22 -net nic,model=virtio \
    -nographic >> $ROOT/runs/t6v2/qemu.log 2>&1 &
echo $! > $ROOT/runs/t6v2/qemu.pid
for i in $(seq 1 60); do $SSH -p $P ubuntu@127.0.0.1 true 2>/dev/null && break; sleep 2; done
$SSH -p $P ubuntu@127.0.0.1 'sudo stdbuf -oL /usr/local/bin/lzdtest -m native -t info,t1 > /home/ubuntu/t6b.txt 2>&1; grep -a "LZDTEST\|FAIL" /home/ubuntu/t6b.txt' 2>/dev/null | grep -av Warning

kill -9 $(cat $ROOT/runs/t6v2/qemu.pid) 2>/dev/null
echo T6-DONE
