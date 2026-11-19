#!/bin/bash
# bind_dev.sh -- move the FEMU ZNS device from the kernel nvme driver to
# uio_pci_generic so SPDK can claim it (run inside the guest).
set -e
modprobe uio_pci_generic 2>/dev/null || true

BDF=""
for d in /sys/bus/pci/devices/*; do
    ven=$(cat "$d/vendor")
    dev=$(cat "$d/device")
    if [ "$ven" = "0x8086" ] && [ "$dev" = "0x5845" ]; then
        BDF=$(basename "$d")
        break
    fi
done
[ -z "$BDF" ] && { echo "no FEMU device found"; exit 1; }

if [ -e "/sys/bus/pci/drivers/nvme/$BDF" ]; then
    echo "$BDF" > /sys/bus/pci/drivers/nvme/unbind
fi
if [ ! -e "/sys/bus/pci/drivers/uio_pci_generic/$BDF" ]; then
    echo "$BDF" > /sys/bus/pci/drivers/uio_pci_generic/bind
fi
echo "bound $BDF to uio_pci_generic"
ls -l "/sys/bus/pci/drivers/uio_pci_generic/$BDF/uio" 2>/dev/null || true
