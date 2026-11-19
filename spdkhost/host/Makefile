#  LiZoneDedup SPDK host-side suite (built inside an SPDK tree).
#  Install: cp -r host <spdk>/examples/nvme/lzd && make -C examples/nvme/lzd
#
SPDK_ROOT_DIR := $(abspath $(CURDIR)/../../..)
NVME_DIR := $(SPDK_ROOT_DIR)/lib/nvme

include $(SPDK_ROOT_DIR)/mk/spdk.common.mk
include $(SPDK_ROOT_DIR)/mk/spdk.modules.mk

APP = lzdtest
C_SRCS = lzd.c lzdtest.c

SPDK_LIB_LIST += $(SOCK_MODULES_LIST) nvme vmd

include $(SPDK_ROOT_DIR)/mk/spdk.app.mk
