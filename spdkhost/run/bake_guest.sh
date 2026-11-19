#!/bin/bash
# bake_guest.sh -- bake runtime bits into the guest base image:
#   uio_pci_generic module, spdk shared libs (ldconfig'd), lzdtest binary,
#   and a systemd unit that binds the FEMU device to uio at boot.
# Idempotent; run while NO test VM is using the base.
set -eu
ROOT=~/lzd
KEY=$ROOT/lzd_key
SSH="ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15"
RS="rsync -a -e \"ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null\""

rm -rf $ROOT/runs/bake; mkdir -p $ROOT/runs/bake
cd $ROOT/FEMU_lzd/build
nohup ./qemu-system-x86_64 -name lzd-bake \
    -accel kvm -cpu host -smp 8 -m 4G \
    -drive file=$ROOT/lzd-base.qcow2,if=virtio,format=qcow2,snapshot=off \
    -device femu,devsz_mb=96,namespaces=1,zns_num_ch=1,zns_num_lun=1,zns_num_plane=1,zns_num_blk=48,zns_flash_type=2,femu_mode=3,lz_mode=6,lz_r=2,lz_sha_hw=1,lz_fallback=0 \
    -net user,hostfwd=tcp::23777-:22 -net nic,model=virtio \
    -nographic > $ROOT/runs/bake/qemu.log 2>&1 &
echo $! > $ROOT/runs/bake/qemu.pid

for i in $(seq 1 90); do
    $SSH -p 23777 ubuntu@127.0.0.1 true 2>/dev/null && break
    sleep 2
done

# push runtime pieces
timeout 300 rsync -a -e "ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p 23777" \
    $ROOT/spdk/build/lib/ $ROOT/spdk/dpdk/build/lib/ ubuntu@127.0.0.1:spdklib-push/ 2>/dev/null || \
    { timeout 300 rsync -a -e "ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p 23777" $ROOT/spdk/build/lib/ ubuntu@127.0.0.1:spdklib-push/; \
      timeout 300 rsync -a -e "ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p 23777" $ROOT/spdk/dpdk/build/lib/ ubuntu@127.0.0.1:spdklib-push/; }
timeout 120 rsync -L -a -e "ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p 23777" \
    /usr/lib/x86_64-linux-gnu/libfdt.so.1 /usr/lib/x86_64-linux-gnu/libnuma.so.1 \
    ubuntu@127.0.0.1:spdklib-push/
timeout 120 rsync -a -e "ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p 23777" \
    $ROOT/spdk/build/examples/lzdtest ubuntu@127.0.0.1:lzdtest-push
timeout 60 scp -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -P 23777 \
    $ROOT/kmods/lib/modules/6.8.0-147-generic/kernel/drivers/uio/uio_pci_generic.ko.zst \
    ubuntu@127.0.0.1:uio_pci_generic.ko.zst

# install in-guest: libs, module, bind service
timeout 120 $SSH -p 23777 ubuntu@127.0.0.1 'bash -s' << 'EOS'
set -e
sudo rm -rf /usr/local/lib/spdklib /usr/local/bin/lzdtest
sudo cp -r ~/spdklib-push /usr/local/lib/spdklib
sudo cp ~/lzdtest-push /usr/local/bin/lzdtest
sudo chmod 755 /usr/local/bin/lzdtest
echo /usr/local/lib/spdklib | sudo tee /etc/ld.so.conf.d/spdklib.conf
sudo ldconfig
KREL=$(uname -r)
sudo mkdir -p /lib/modules/$KREL/kernel/drivers/uio
sudo cp ~/uio_pci_generic.ko.zst /lib/modules/$KREL/kernel/drivers/uio/uio_pci_generic.ko.zst
sudo depmod -a
cat << 'UNIT' | sudo tee /etc/systemd/system/lzd-bind.service
[Unit]
Description=Bind FEMU ZNS device to uio_pci_generic for SPDK
After=multi-user.target
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'modprobe uio_pci_generic; BDF=$$(grep -l 0x5845 /sys/bus/pci/devices/*/device 2>/dev/null | head -1 | xargs dirname | xargs basename); echo "8086 5845" > /sys/bus/pci/drivers/uio_pci_generic/new_id 2>/dev/null; echo uio_pci_generic > /sys/bus/pci/devices/$$BDF/driver_override 2>/dev/null; if [ -e /sys/bus/pci/drivers/nvme/$$BDF ]; then echo $$BDF > /sys/bus/pci/drivers/nvme/unbind; fi; if [ ! -e /sys/bus/pci/drivers/uio_pci_generic/$$BDF ]; then echo $$BDF > /sys/bus/pci/drivers/uio_pci_generic/bind; fi; exit 0'
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl enable lzd-bind.service
sudo rm -rf ~/spdklib-push ~/lzdtest-push
echo BAKE_DONE
EOS

timeout 60 $SSH -p 23777 ubuntu@127.0.0.1 'sudo systemctl start lzd-bind.service; sleep 1; systemctl is-active lzd-bind.service; ls /dev/uio0; lzdtest -m native -t info 2>/dev/null | head -3 || sudo LD_LIBRARY_PATH=/usr/local/lib/spdklib /usr/local/bin/lzdtest -m native -t info | head -3'

# shut down cleanly so the disk is consistent
timeout 90 $SSH -p 23777 ubuntu@127.0.0.1 'sudo sync; sudo poweroff' || true
sleep 15
kill $(cat $ROOT/runs/bake/qemu.pid) 2>/dev/null || true
echo BAKE_COMPLETE
