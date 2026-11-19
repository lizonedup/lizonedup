#!/bin/bash
# arm_zs.sh NAME MODE FB ZS SCALE -- boot one GB-zone arm, run scaled t7w
set -u
ROOT=~/lzd
KEY=$ROOT/lzd_key
SSH="ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15"
NAME=$1; MODE=$2; FB=$3; ZS=$4; SCALE=$5

bash $ROOT/scripts/boot_zs.sh "$NAME" "$MODE" "$FB" "$ZS" > /dev/null
P=$(cat $ROOT/runs/$NAME/port)
for i in $(seq 1 120); do
    $SSH -p "$P" ubuntu@127.0.0.1 true 2>/dev/null && break
    sleep 2
done
timeout 120 scp -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -P "$P" \
    $ROOT/spdk/build/examples/lzdtest ubuntu@127.0.0.1:lzdtest-new > /dev/null 2>&1
timeout 60 $SSH -p "$P" ubuntu@127.0.0.1 'bash -s' << 'EOS' > /dev/null 2>&1
sudo cp ~/lzdtest-new /usr/local/bin/lzdtest
sudo modprobe uio_pci_generic 2>/dev/null
BDF=$(grep -l 0x5845 /sys/bus/pci/devices/*/device 2>/dev/null | head -1 | xargs dirname | xargs basename)
echo "8086 5845" | sudo tee /sys/bus/pci/drivers/uio_pci_generic/new_id > /dev/null 2>&1
echo uio_pci_generic | sudo tee /sys/bus/pci/devices/$BDF/driver_override > /dev/null
[ -e /sys/bus/pci/drivers/nvme/$BDF ] && echo $BDF | sudo tee /sys/bus/pci/drivers/nvme/unbind > /dev/null
[ -e /sys/bus/pci/drivers/uio_pci_generic/$BDF ] || echo $BDF | sudo tee /sys/bus/pci/drivers/uio_pci_generic/bind > /dev/null
EOS
MODEARG=native; [ "$FB" = "1" ] && MODEARG=fallback
timeout 3000 $SSH -p "$P" ubuntu@127.0.0.1 \
    "sudo stdbuf -oL /usr/local/bin/lzdtest -m $MODEARG -t t7w -s $SCALE" \
    > $ROOT/runs/$NAME/t7w.out 2>&1
grep -a "^T7 " $ROOT/runs/$NAME/t7w.out | tail -1 | sed "s/^/[$NAME] /"
kill $(cat $ROOT/runs/$NAME/qemu.pid) 2>/dev/null
