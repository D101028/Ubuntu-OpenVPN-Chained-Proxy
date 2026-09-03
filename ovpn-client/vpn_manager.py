import sys
import time
import signal
import subprocess

import requests

from uma_filter import vpn_selector

VPN_PROCESS = None
CONFIG_PATH = "/tmp/client.ovpn"

MONITOR_INTERVAL = 30  # in minutes

def handle_exit(signum, frame):
    print("[*] 正在終止 OpenVPN 行程...")
    if VPN_PROCESS and VPN_PROCESS.poll() is None:
        VPN_PROCESS.terminate()
        VPN_PROCESS.wait(timeout=5)
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_exit)
signal.signal(signal.SIGINT, handle_exit)

def fetch_and_select_ovpn() -> str:
    url = "https://www.vpngate.net/api/iphone/"
    response = requests.get(url)
    raw = response.text.strip()
    raw = raw[raw.index("#")+1:-1]

    return vpn_selector(raw)[0]

def vpn_is_ok() -> bool:
    try:
        return requests.get("https://www.google.com/generate_204", timeout=10).status_code == 204
    except requests.RequestException:
        return False

def main():
    global VPN_PROCESS
    print("[*] OpenVPN 客戶端管理器啟動...")
    ovpn_data = fetch_and_select_ovpn()

    if not ovpn_data:
        print("[!] 尚未提供真實 .ovpn 內容，進入 Dummy 待機以維持容器運行...")
        while True:
            time.sleep(10)

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(ovpn_data)

    print("[*] 正在啟動 OpenVPN 連線...")
    cmd = [
        "openvpn",
        "--config", CONFIG_PATH,
        "--dev", "tun0",
        "--script-security", "2",
        "--redirect-gateway", "def1"
    ]
    VPN_PROCESS = subprocess.Popen(cmd)
    next_monitor = time.monotonic() + MONITOR_INTERVAL * 60

    while True:
        ret = VPN_PROCESS.poll()
        if ret is not None:
            print(f"[!] OpenVPN 異常終止 (代碼: {ret})，5 秒後嘗試重連...")
            time.sleep(5)
            break
        if time.monotonic() >= next_monitor:
            print("[*] VPN 健康檢查中...")
            next_monitor = time.monotonic() + MONITOR_INTERVAL * 60
            if not vpn_is_ok():
                print("[!] VPN 健康檢查失敗，正在終止連線以重新連線...")
                VPN_PROCESS.terminate()
            else:
                print("[*] VPN 健康檢查成功")
        time.sleep(5)

if __name__ == "__main__":
    main()
