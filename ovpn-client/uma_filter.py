import base64
import io
import os
import re
import socket
import subprocess
import time
from typing import Optional

import pandas as pd
from urllib.parse import urlparse
from curl_cffi import requests
from curl_cffi.requests.errors import RequestsError

COUNTRY_CODES = ["JP"]
IP_FILTERED = [
    r"^219\.100\.37\.*"
]
TMP_CLIENT_PATH = "/tmp/client-test.ovpn"

class OvpnTester:

    def __init__(
        self,
        ovpn_path: str,
        dev_name: str = "tun99",
        physical_iface: Optional[str] = None,
        physical_gw: Optional[str] = None,
    ):
        """參數說明:

        - ovpn_path: .ovpn 設定檔路徑
        - dev_name: 獨立指定的虛擬網卡名稱 (預設 tun99，避免與既有 tun0 衝突)
        - physical_iface: 本地實體網卡 (如 eth0, enp3s0, wlan0)，留空則自動偵測
        - physical_gw: 本地實體閘道 IP (如 192.168.1.1)，留空則自動偵測
        """
        self.ovpn_path = os.path.abspath(ovpn_path)
        self.dev_name = dev_name
        self.process: Optional[subprocess.Popen] = None
        self.server_ip: Optional[str] = None
        self.server_port: Optional[str] = None

        if not os.path.exists(self.ovpn_path):
            raise FileNotFoundError(f"找不到檔案: {self.ovpn_path}")

        # 1. 解析目標 OpenVPN Server 的 IP 與 Port
        self._parse_ovpn_remote()

        # 2. 獲取本地實體介面與 Gateway
        self.iface, self.gw = physical_iface, physical_gw
        if not self.iface or not self.gw:
            self.iface, self.gw = self._detect_physical_route()

    def _parse_ovpn_remote(self):
        """解析 .ovpn 中的 remote 欄位並進行 DNS 解析"""
        with open(self.ovpn_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        match = re.search(r"^\s*remote\s+(\S+)\s+(\d+)", content, re.MULTILINE)
        if not match:
            raise ValueError("無法在 .ovpn 檔案中解析到有效的 remote 設定")

        host, port = match.group(1), match.group(2)
        self.server_ip = socket.gethostbyname(host)
        self.server_port = port

    def _detect_physical_route(self) -> tuple[str, str]:
        """自動偵測本地實體網卡與閘道 (排除 tun, tap, docker 等虛擬介面)"""
        res = subprocess.run(
            ["ip", "route", "show"],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in res.stdout.splitlines():
            # 尋找經由實體介面的預設路由或區域網路網段
            if "default via" in line and not any(
                v in line for v in ["tun", "tap", "docker", "veth", "wg"]
            ):
                parts = line.split()
                gw = parts[parts.index("via") + 1]
                iface = parts[parts.index("dev") + 1]
                return iface, gw

        raise RuntimeError(
            "無法自動識別本地實體網卡，請手動傳入 physical_iface 與 physical_gw"
        )

    def _add_bypass_route(self):
        """將 VPN 伺服器 IP 強制指向本地實體閘道，防止握手封包被既有 VPN 劫持"""
        subprocess.run([
            "ip", 
            "route",
            "replace",
            self.server_ip,
            "via",
            self.gw,
            "dev",
            self.iface
        ], check=True) # type: ignore

    def _del_bypass_route(self):
        """清除伺服器 IP 靜態路由"""
        if self.server_ip:
            subprocess.run(
                ["ip", "route", "del", self.server_ip, "dev", str(self.iface)],
                stderr=subprocess.DEVNULL,
                check=False,
            ) # type: ignore

    def start(self, timeout: int = 100) -> bool:
        """啟動 OpenVPN 行程並等待通道建立完成"""
        self._add_bypass_route()

        cmd = [
            "openvpn",
            "--config",
            self.ovpn_path,
            "--dev",
            self.dev_name,
            "--route-noexec",  # 關鍵：禁止 OpenVPN 修改系統路由
            "--verb",
            "3",
        ]

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        # 監聽 stdout 等待連線成功標誌
        start_time = time.time()
        while time.time() - start_time < timeout:
            line = self.process.stdout.readline() # type: ignore
            if not line and self.process.poll() is not None:
                break

            if "Initialization Sequence Completed" in line:
                return True

            time.sleep(1)
        else:
            self.stop()
            raise TimeoutError(f"OpenVPN 未能在 {timeout} 秒內建立連線")

        return False

    def check(self, api: str = "https://api.games.umamusume.jp") -> bool:
        """融入檢測邏輯：

        1. 解析目標 URL 所有可能的 IPv4
        2. 將這些 IP 綁定到該測試用 tun 網卡
        3. 發出 HTTP/2 請求並針對 Akamai 特有的 RST_STREAM (code 92) 判定
        4. 清理臨時路由
        """
        print(f"[測試] api {api} 測試中...")

        if not self.process or self.process.poll() is not None:
            print("[狀態: 錯誤] OpenVPN 程序未運行")
            return False

        parsed = urlparse(api)
        host = parsed.hostname or api
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        routed_ips = []
        try:
            # Akamai 具有多組 Anycast IP，需取得該域名所有解析結果以防請求漏逸
            addr_info = socket.getaddrinfo(
                host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
            )
            routed_ips = list({item[4][0] for item in addr_info})
        except socket.gaierror as e:
            print(f"[狀態: DNS 解析失敗] {e}")
            return False

        # 臨時新增路由導向測試網卡
        for rip in routed_ips:
            subprocess.run(
                ["ip", "route", "replace", str(rip), "dev", self.dev_name], check=True
            ) # type: ignore

        try:
            # 使用 impersonate 模擬瀏覽器 TLS 指紋，避免被直接攔截
            response = requests.get(
                api, timeout=10, impersonate="chrome124", verify=True
            )

            # 情況一：成功拿到回應（伺服器正常運作時該路徑預設回傳 HTTP 404）
            print(f"[狀態: 正常回應] HTTP {response.status_code}")
            return True

        except RequestsError as e:
            # 情況二：Akamai CDN RST_STREAM 拒絕連線 (curl error 92: CURLE_HTTP2_STREAM)
            if getattr(e, "code", None) == 92 or "INTERNAL_ERROR" in str(e):
                print(
                    f"[狀態: HTTP/2 被拒絕/Stream 錯誤] code: {getattr(e, 'code', 'N/A')},"
                    f" msg: {e}"
                )
            else:
                print(f"[狀態: 其他網路錯誤] {e}")
            return False

        except Exception as e:
            print(f"[狀態: 未知異常] {e}")
            return False

        finally:
            # 還原暫存路由
            for rip in routed_ips:
                subprocess.run(
                    ["ip", "route", "del", str(rip), "dev", self.dev_name],
                    stderr=subprocess.DEVNULL,
                    check=False,
                ) # type: ignore

    def stop(self):
        """終止行程並還原路由"""
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None

        self._del_bypass_route()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

def vpn_selector(raw_csv: str, count: int = 1) -> list[str]:
    """Input `raw_csv`: raw data of vpn list (csv) (vpngate form)

    Output: list of full ovpn config data"""
    config_list: list[str] = []

    buffer = io.StringIO(raw_csv)

    df = pd.read_csv(buffer)

    # Filter by Country Code
    for code in COUNTRY_CODES:
        df = df[df['CountryShort'] == code]
    # Filter by IP
    for pattern in IP_FILTERED:
        df = df[~df['IP'].str.contains(pattern, regex=True, na=False)]

    for index, row in df.iterrows():
        if len(config_list) >= count:
            break
        config_base64: str = row['OpenVPN_ConfigData_Base64']
        config = base64.b64decode(config_base64).decode()
        with open(TMP_CLIENT_PATH, mode="w") as fp:
            fp.write(config)
        with OvpnTester(TMP_CLIENT_PATH) as tester:
            if tester.check():
                config_list.append(config)

    if os.path.isfile(TMP_CLIENT_PATH):
        os.remove(TMP_CLIENT_PATH)

    return config_list

