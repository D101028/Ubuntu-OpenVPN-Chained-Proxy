import sys
import signal
import subprocess
import threading
import time
from collections import Counter

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

class SafeCounter:
    def __init__(self):
        self.value = 0
        self.lock = threading.Lock()

    def increment(self):
        with self.lock:
            self.value += 1
            return self.value

def keep_alive(target_ip: str = "8.8.8.8", interval: int = 20, timeout: int = 20, max_retry: int = 3):
    """建立一個背景執行緒，週期性對指定 IP 發送 Ping 請求以保持 OpenVPN 連線。

    Args:
        target_ip (str): 要 Ping 的目標 IP 位址。預設為 '8.8.8.8'。
        interval (int): 每次 Ping 的間隔時間（秒）。預設為 20 秒。
        timeout (int): 等待 Ping 的時間（秒）上限。預設為 20 秒。
        max_retry (int): 容忍連續 Ping 失敗的最高次數

    Returns:
        tuple: (threading.Thread, threading.Event)
               - thread: 執行的執行緒物件。
               - stop_event: 用於通知執行緒停止的 Event 物件。
    """
    stop_event = threading.Event()
    retry_counter = SafeCounter()

    def ping_worker():
        print(f"[Keep-Alive] 背景保持連線程式已啟動，目標：{target_ip}")

        # 當 stop_event 沒有被設定時，持續循環
        while not stop_event.is_set():
            try:
                # 執行 Ping 指令
                subprocess.run(
                    ["ping", "-c", "1", target_ip],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                    timeout=timeout,
                )
                print(
                    f"[Keep-Alive] 已發送保持連線訊號至 {target_ip} ({time.strftime('%X')})"
                )
            except Exception as e:
                print(f"[Keep-Alive] 發送失敗: {e}")
                if retry_counter.value >= max_retry:
                    stop_event.set()
                    return 
                retry_counter.increment()

            # 使用 stop_event.wait(interval) 代替 time.sleep(interval)
            # 這樣當主程式要求停止時，執行緒可以「立刻」反應，不用白白等待 interval 秒
            if stop_event.wait(interval):
                break

        print("[Keep-Alive] 背景保持連線程式已安全停止。")

    # 建立執行緒，並將 daemon 設為 True（確保主程式異常結束時，此執行緒也會跟著結束）
    alive_thread = threading.Thread(target=ping_worker, daemon=True)
    alive_thread.start()

    return alive_thread, stop_event

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
    _, stop_event = keep_alive()

    while True:
        ret = VPN_PROCESS.poll()
        if ret is not None:
            print(f"[!] OpenVPN 異常終止 (代碼: {ret})，5 秒後嘗試重連...")
            stop_event.set()
            time.sleep(5)
            break
        if time.monotonic() >= next_monitor or stop_event.is_set():
            print("[*] VPN 健康檢查中...")
            next_monitor = time.monotonic() + MONITOR_INTERVAL * 60
            if not vpn_is_ok(): # 大檢查
                print("[!] VPN 健康檢查失敗，正在終止連線以重新連線...")
                VPN_PROCESS.terminate()
                stop_event.set()
            else:
                if stop_event.is_set():
                    # Restart the alive thread
                    _, stop_event = keep_alive()
                print("[*] VPN 健康檢查成功")
        time.sleep(5)

if __name__ == "__main__":
    main()
