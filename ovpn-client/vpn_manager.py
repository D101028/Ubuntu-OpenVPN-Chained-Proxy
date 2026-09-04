import sys
import signal
import subprocess
import threading
import time

import requests

from config import Config
from uma_filter import vpn_selector

VPN_PROCESS = None
CONFIG_PATH = "/tmp/client.ovpn"
RECONNECT_DELAY_SECONDS = 5
FETCH_RETRY_DELAY_SECONDS = 10
PROCESS_STOP_TIMEOUT_SECONDS = 5
LOOP_INTERVAL_SECONDS = 1

def handle_exit(signum, frame):
    print("[*] 正在終止 OpenVPN 行程...")
    stop_vpn_process()
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_exit)
signal.signal(signal.SIGINT, handle_exit)


def stop_vpn_process() -> None:
    """停止目前的 OpenVPN 子行程；必要時強制結束。"""
    global VPN_PROCESS

    if VPN_PROCESS is None or VPN_PROCESS.poll() is not None:
        return

    VPN_PROCESS.terminate()
    try:
        VPN_PROCESS.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        print("[!] OpenVPN 未能及時停止，強制結束行程。")
        VPN_PROCESS.kill()
        VPN_PROCESS.wait()

def fetch_and_select_ovpn() -> str:
    url = "https://www.vpngate.net/api/iphone/"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    raw = response.text.strip()
    header_end = raw.find("#")
    if header_end == -1:
        raise ValueError("VPNGate 回應格式不正確：找不到 CSV 標頭")
    raw = raw[header_end + 1:].strip().rstrip("*")

    selected = vpn_selector(raw)
    return selected[0] if selected else ""

def vpn_is_ok() -> bool:
    try:
        response = requests.get(Config.CHECK_URL, timeout=10)
        if Config.CHECK_AVAILABLE_STATUS != "default":
            return str(response.status_code) == str(Config.CHECK_AVAILABLE_STATUS)
        else:
            try:
                response.raise_for_status()
                return True
            except requests.HTTPError:
                return False
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

def keep_alive(
    target_ip: str | None = None,
    interval: int | None = None,
    timeout: int = 20, max_retry: int = 3):
    """建立一個背景執行緒，週期性對指定 IP 發送 Ping 請求以保持 OpenVPN 連線。

    Args:
        target_ip (str | None): 要 Ping 的目標 IP 位址；未指定時使用 Config.PING_HOST。
        interval (int | None): 每次 Ping 的間隔時間（秒）；未指定時使用 Config.PING_INTERVAL。
        timeout (int): 等待 Ping 的時間（秒）上限。預設為 20 秒。
        max_retry (int): 容忍連續 Ping 失敗的最高次數

    Returns:
        tuple: (threading.Thread, threading.Event)
               - thread: 執行的執行緒物件。
               - stop_event: 用於通知執行緒停止的 Event 物件。
    """
    target_ip = target_ip or Config.PING_HOST or "8.8.8.8"
    interval = interval if interval is not None else _positive_int(
        Config.PING_INTERVAL, "PING_INTERVAL", 20
    )
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
                # print(
                #     f"[Keep-Alive] 已發送保持連線訊號至 {target_ip} ({time.strftime('%X')})"
                # )
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

def _positive_int(value: str, name: str, default: int) -> int:
    """讀取正整數設定；格式錯誤時使用預設值並留下明確訊息。"""
    try:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        print(f"[!] {name} 必須是正整數，將使用預設值 {default}。")
        return default


def _auto_refresh_interval() -> int | None:
    value = Config.AUTO_REFRESH_INTERVAL
    if value is None or value.lower() == "disabled":
        return None
    return _positive_int(value, "AUTO_REFRESH_INTERVAL", 0) or None


def main():
    global VPN_PROCESS
    print("[*] OpenVPN 客戶端管理器啟動...")
    check_interval = _positive_int(Config.CHECK_INTERVAL, "CHECK_INTERVAL", 30)
    refresh_interval = _auto_refresh_interval()

    while True:
        try:
            ovpn_data = fetch_and_select_ovpn()
        except Exception as exc:
            print(f"[!] 無法取得或驗證 VPN 設定：{exc}，{FETCH_RETRY_DELAY_SECONDS} 秒後重試。")
            time.sleep(FETCH_RETRY_DELAY_SECONDS)
            continue

        if not ovpn_data:
            print(f"[!] 找不到可用的 VPN 設定，{FETCH_RETRY_DELAY_SECONDS} 秒後重試。")
            time.sleep(FETCH_RETRY_DELAY_SECONDS)
            continue

        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as config_file:
                config_file.write(ovpn_data)

            print("[*] 正在啟動 OpenVPN 連線...")
            VPN_PROCESS = subprocess.Popen([
                "openvpn", "--config", CONFIG_PATH, "--dev", "tun0",
                "--script-security", "2", "--redirect-gateway", "def1",
            ])
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[!] 無法啟動 OpenVPN：{exc}，{RECONNECT_DELAY_SECONDS} 秒後重試。")
            time.sleep(RECONNECT_DELAY_SECONDS)
            continue

        _, stop_event = keep_alive()
        now = time.monotonic()
        next_monitor = now + check_interval * 60
        next_refresh = now + refresh_interval * 60 if refresh_interval else None

        try:
            while VPN_PROCESS.poll() is None:
                now = time.monotonic()
                if next_refresh is not None and now >= next_refresh:
                    print("[*] 已到達定期重連時間，正在重新連線...")
                    break

                if stop_event.is_set() or now >= next_monitor:
                    print("[*] VPN 健康檢查中...")
                    next_monitor = now + check_interval * 60
                    if not vpn_is_ok():
                        print("[!] VPN 健康檢查失敗，正在重新連線...")
                        break

                    if stop_event.is_set():
                        _, stop_event = keep_alive()
                    print("[*] VPN 健康檢查成功")

                time.sleep(LOOP_INTERVAL_SECONDS)
            else:
                print(f"[!] OpenVPN 異常終止 (代碼: {VPN_PROCESS.returncode})。")
        finally:
            stop_event.set()
            stop_vpn_process()

        print(f"[*] {RECONNECT_DELAY_SECONDS} 秒後嘗試重新連線...")
        time.sleep(RECONNECT_DELAY_SECONDS)

if __name__ == "__main__":
    main()
