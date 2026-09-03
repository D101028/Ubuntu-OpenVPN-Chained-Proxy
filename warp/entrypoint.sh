#!/bin/bash
set -e

# 1. 確保網絡反向路徑過濾模式放寬 (允許轉發)
sysctl -w net.ipv4.conf.all.rp_filter=2 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf.default.rp_filter=2 >/dev/null 2>&1 || true
sysctl -w net.ipv4.conf.eth0.rp_filter=2 >/dev/null 2>&1 || true

# 2. 啟用 NAT 轉發
iptables -t nat -A POSTROUTING ! -o eth0 -j MASQUERADE
iptables -A FORWARD -i eth0 -j ACCEPT
iptables -A FORWARD -o eth0 -j ACCEPT

# 3. 清理舊有 PID 避免重啟失敗，並啟動 D-Bus
mkdir -p /var/run/dbus /run/dbus
rm -f /var/run/dbus/pid /run/dbus/pid /run/dbus/system_bus_socket

dbus-daemon --system --fork

# 4. 啟動 WARP Service
warp-svc &
WARP_PID=$!

echo "[WARP] 等待 warp-svc 守護行程就緒..."
# 輪詢檢查 daemon 通訊端是否已建立
for i in {1..30}; do
    if ! warp-cli --accept-tos status 2>&1 | grep -q "Unable to connect"; then
        echo "[WARP] warp-svc 已就緒。"
        break
    fi
    sleep 1
done

# 5. 註冊並建立連線
warp-cli --accept-tos registration new >/dev/null 2>&1 || true
warp-cli --accept-tos mode warp
warp-cli --accept-tos connect

echo "[WARP] 等待連線完成..."
for i in {1..20}; do
    if warp-cli --accept-tos status 2>/dev/null | grep -q "Connected"; then
        echo "[WARP] 連線成功 (TUN 模式已啟用)。"
        break
    fi
    sleep 1
done

wait $WARP_PID
