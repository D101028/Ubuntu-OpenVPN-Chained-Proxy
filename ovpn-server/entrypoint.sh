#!/bin/bash
set -e

CONFIG_DIR="/etc/openvpn"
CLIENTS_OUT="/etc/openvpn/clients"
CCD_DIR="$CONFIG_DIR/ccd"
SERVER_HOST="${SERVER_HOST:-127.0.0.1}"

mkdir -p "$CONFIG_DIR" "$CLIENTS_OUT" "$CCD_DIR"

# 1. 首次啟動：自動非互動產生憑證與金鑰 (PKI)
if [ ! -f "$CONFIG_DIR/server.key" ]; then
    echo "[Server] 正在自動生成 CA 與伺服端憑證..."
    openssl req -new -x509 -days 3650 -nodes -newkey rsa:2048 \
        -keyout "$CONFIG_DIR/ca.key" -out "$CONFIG_DIR/ca.crt" -subj "/CN=OpenVPN-CA"

    openssl req -new -nodes -newkey rsa:2048 \
        -keyout "$CONFIG_DIR/server.key" -out "$CONFIG_DIR/server.csr" -subj "/CN=server"
    openssl x509 -req -days 3650 -in "$CONFIG_DIR/server.csr" \
        -CA "$CONFIG_DIR/ca.crt" -CAkey "$CONFIG_DIR/ca.key" -CAcreateserial -out "$CONFIG_DIR/server.crt"

    echo "[Server] 正在生成 Diffie-Hellman 與 TLS-Auth 金鑰..."
    openssl dhparam -out "$CONFIG_DIR/dh.pem" 2048
    openvpn --genkey secret "$CONFIG_DIR/ta.key"

    # 建立客戶端憑證與 CCD 固定 IP 配置
    CLIENT_NAMES=("client" "client-warp" "client-ovpn")
    CLIENT_IPS=("10.8.0.100" "10.8.0.101" "10.8.0.102")

    for i in "${!CLIENT_NAMES[@]}"; do
        NAME="${CLIENT_NAMES[$i]}"
        IP="${CLIENT_IPS[$i]}"

        echo "[Server] 生成客戶端憑證: $NAME ($IP)..."
        openssl req -new -nodes -newkey rsa:2048 \
            -keyout "$CONFIG_DIR/$NAME.key" -out "$CONFIG_DIR/$NAME.csr" -subj "/CN=$NAME"
        openssl x509 -req -days 3650 -in "$CONFIG_DIR/$NAME.csr" \
            -CA "$CONFIG_DIR/ca.crt" -CAkey "$CONFIG_DIR/ca.key" -CAcreateserial -out "$CONFIG_DIR/$NAME.crt"

        # 寫入 CCD 固定 IP 規則
        echo "ifconfig-push $IP 255.255.255.0" > "$CCD_DIR/$NAME"

        # 封裝一體化 (Inline) .ovpn 設定檔
        cat <<EOF > "$CLIENTS_OUT/$NAME.ovpn"
client
dev tun
proto udp
remote $SERVER_HOST 46159
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
cipher AES-256-GCM
key-direction 1
verb 3
<ca>
$(cat "$CONFIG_DIR/ca.crt")
</ca>
<cert>
$(cat "$CONFIG_DIR/$NAME.crt")
</cert>
<key>
$(cat "$CONFIG_DIR/$NAME.key")
</key>
<tls-auth>
$(cat "$CONFIG_DIR/ta.key")
</tls-auth>
EOF
    done
    echo "[Server] 三個客戶端設定檔已輸出至 ./ovpn-server/clients/"
fi

# 2. 建立 OpenVPN 伺服端設定
cat <<EOF > "$CONFIG_DIR/server.conf"
port 46159
proto udp
dev tun
ca $CONFIG_DIR/ca.crt
cert $CONFIG_DIR/server.crt
key $CONFIG_DIR/server.key
dh $CONFIG_DIR/dh.pem
tls-auth $CONFIG_DIR/ta.key 0
topology subnet
server 10.8.0.0 255.255.255.0
client-config-dir $CCD_DIR
push "redirect-gateway def1"
push "dhcp-option DNS 1.1.1.1"
push "dhcp-option DNS 8.8.8.8"
keepalive 10 120
cipher AES-256-GCM
persist-key
persist-tun
verb 3
EOF

# 3. 策略路由 (PBR) 與防火牆設定
echo "[Server] 配置策略路由與轉發..."
# client-warp (10.8.0.101) 導向 WARP 容器 (172.20.0.10)
ip rule del from 10.8.0.101 lookup 101 2>/dev/null || true
ip rule add from 10.8.0.101 lookup 101
ip route replace default via 172.20.0.10 dev eth0 table 101

# client-ovpn (10.8.0.102) 導向 OVPN 客戶端容器 (172.20.0.20)
ip rule del from 10.8.0.102 lookup 102 2>/dev/null || true
ip rule add from 10.8.0.102 lookup 102
ip route replace default via 172.20.0.20 dev eth0 table 102

# NAT 偽裝：確保回包可循原路返回伺服端
iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
iptables -A FORWARD -j ACCEPT

exec openvpn --config "$CONFIG_DIR/server.conf"
