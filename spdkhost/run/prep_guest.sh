#!/bin/bash
# prep_guest.sh -- one-time guest base image preparation (on the gpu host).
#   1. make an ssh keypair for guest access
#   2. build the NoCloud seed ISO (user-data + meta-data)
#   3. boot the image once: cloud-init sets password+key, hugepages, uio
#   4. snapshot the powered-off image as lzd-base.qcow2
set -eu
ROOT=~/lzd

# 1. keypair
if [ ! -f "$ROOT/lzd_key" ]; then
    ssh-keygen -t ed25519 -N "" -f "$ROOT/lzd_key" -C lzd > /dev/null
fi
PUB=$(cat "$ROOT/lzd_key.pub")

# 2. seed ISO
mkdir -p "$ROOT/seed"
cat > "$ROOT/seed/user-data" << EOF
#cloud-config
hostname: lzdguest
users:
  - name: ubuntu
    plain_text_passwd: lzdpass
    lock_passwd: false
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - $PUB
ssh_pwauth: true
runcmd:
  - [ sysctl, -w, vm.nr_hugepages=1024 ]
  - [ sh, -c, 'echo vm.nr_hugepages=1024 >> /etc/sysctl.d/99-lzd.conf' ]
  - [ sh, -c, 'echo uio_pci_generic >> /etc/modules-load.d/lzd.conf' ]
  - [ modprobe, uio_pci_generic ]
  - [ touch, /boot/guest-ready ]
power_state:
  mode: poweroff
  timeout: 30
  condition: true
EOF
cat > "$ROOT/seed/meta-data" << 'EOF'
instance-id: lzdguest-001
local-hostname: lzdguest
EOF
genisoimage -output "$ROOT/seed/seed.iso" -volid cidata -joliet -rock \
    "$ROOT/seed/user-data" "$ROOT/seed/meta-data" > /dev/null

# 3. resize the base image a bit and boot once with the seed + FEMU dev
qemu-img resize "$ROOT/noble.img" 8G > /dev/null 2>&1 || true
rm -rf "$ROOT/runs/prep"; mkdir -p "$ROOT/runs/prep"
cd "$ROOT/FEMU_lzd/build"
nohup ./qemu-system-x86_64 \
    -name lzd-prep \
    -accel kvm -cpu host -smp 8 -m 4G \
    -drive file="$ROOT/noble.img",if=virtio,format=qcow2 \
    -drive file="$ROOT/seed/seed.iso",if=virtio,media=cdrom,readonly=on \
    -device femu,devsz_mb=96,namespaces=1,zns_num_ch=1,zns_num_lun=1,zns_num_plane=1,zns_num_blk=48,zns_flash_type=2,femu_mode=3,lz_mode=6,lz_r=2,lz_sha_hw=1,lz_fallback=0 \
    -net user,hostfwd=tcp::23999-:22 \
    -net nic,model=virtio \
    -nographic > "$ROOT/runs/prep/qemu.log" 2>&1 &
echo $! > "$ROOT/runs/prep/qemu.pid"

# wait for cloud-init to finish and power off (max 10 min)
for i in $(seq 1 120); do
    kill -0 $(cat "$ROOT/runs/prep/qemu.pid") 2>/dev/null || break
    sleep 5
done
kill $(cat "$ROOT/runs/prep/qemu.pid") 2>/dev/null || true
sleep 2

grep -a "Cloud-init .* finished" "$ROOT/runs/prep/qemu.log" | tail -1 || true

# 4. convert to the base snapshot (compressed qcow2 backing for runs)
qemu-img convert -O qcow2 "$ROOT/noble.img" "$ROOT/lzd-base.qcow2"
echo "prep: $ROOT/lzd-base.qcow2 ready ($(du -h "$ROOT/lzd-base.qcow2" | cut -f1))"
