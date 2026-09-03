#!/bin/bash
set -e

# 確保 eth0 與虛擬網卡允許不對稱路由轉發
sysctl -w net.ipv4.conf.all.rp_filter=2 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf.default.rp_filter=2 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf.eth0.rp_filter=2 >/dev/null 2>&1 || true

# 啟用 IPv4 轉發與 NAT
iptables -t nat -A POSTROUTING ! -o eth0 -j MASQUERADE
iptables -A FORWARD -i eth0 -j ACCEPT
iptables -A FORWARD -o eth0 -j ACCEPT

exec python vpn_manager.py
