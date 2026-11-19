#!/bin/bash
# robust fetcher: cloud image (resume) + spdk submodules (retry)
fetch_img() {
  for i in $(seq 1 90); do
    curl -sL -C - --max-time 180 -o ~/lzd/noble.img \
      https://mirrors.aliyun.com/ubuntu-cloud-images/noble/current/noble-server-cloudimg-amd64.img \
      && return 0
    sleep 3
  done
  return 1
}
fetch_submods() {
  cd ~/lzd/spdk || return 1
  for i in $(seq 1 60); do
    if git submodule update --init; then
      echo SUBMODULES_OK
      return 0
    fi
    sleep 3
  done
  return 1
}
case $1 in
  img) fetch_img ;;
  sub) fetch_submods ;;
esac
