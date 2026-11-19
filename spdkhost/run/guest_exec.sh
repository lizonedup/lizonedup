#!/bin/bash
# guest_exec.sh PORT CMD  -- helper: run a command in the guest (gpu host side)
PORT=$1; shift
timeout 180 ssh -i ~/lzd/lzd_key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -p "$PORT" ubuntu@127.0.0.1 "$@"
