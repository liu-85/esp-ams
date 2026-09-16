"""
tests/mpy_stubs/network.py
==========================
`network` 模块的桌面端桩实现，只在 PC / CI 上跑测试用。

比真机多出来的是"可注入性"：测试可以设置 scan_result / fail_connect
来模拟"扫到哪些 AP""连接会不会失败"，从而验证联网重试与热点回退逻辑。
"""

AP_IF = 1
STA_IF = 0

STAT_IDLE = 0
STAT_CONNECTING = 1
STAT_WRONG_PASSWORD = 2
STAT_NO_AP_FOUND = 3
STAT_CONNECT_FAIL = 4
STAT_GOT_IP = 5


class WLAN:
    def __init__(self, interface):
        self._interface = interface
        self._active = False
        self._connected = False
        self._ssid = ""
        self._status = STAT_IDLE
        self._ip = "192.168.4.1" if interface == AP_IF else "0.0.0.0"
        # 连上之后 DHCP 分到的地址
        self.sta_ip = "192.168.1.100"

        # ---- 测试用的注入点 ----
        self.scan_result = []      # scan() 返回的原始列表
        self.fail_connect = False  # True 时 connect() 永远失败
        self.fail_status = STAT_CONNECT_FAIL
        self.connect_calls = []    # 记录每次 connect 的 (ssid, password)
        self.scan_calls = 0

    def active(self, *args):
        if args:
            self._active = bool(args[0])
            return None
        return self._active

    def isconnected(self):
        return self._connected

    def status(self):
        return self._status

    def scan(self):
        self.scan_calls += 1
        return list(self.scan_result)

    def connect(self, ssid, password=None, *args, **kwargs):
        self.connect_calls.append((ssid, password))
        self._ssid = ssid
        if self.fail_connect:
            self._connected = False
            self._status = self.fail_status
        else:
            self._connected = True
            self._status = STAT_GOT_IP

    def disconnect(self):
        self._connected = False
        self._status = STAT_IDLE

    def config(self, *args, **kwargs):
        if "essid" in kwargs:
            self._ssid = kwargs["essid"]
        if args:
            # MicroPython 支持 wlan.config('ssid') 形式读取
            return self._ssid
        return None

    def ifconfig(self, *args, **kwargs):
        if args:
            return None
        if self._interface == STA_IF:
            if self._connected:
                return (self.sta_ip, "255.255.255.0", "192.168.1.1", "8.8.8.8")
            return ("0.0.0.0", "0.0.0.0", "0.0.0.0", "0.0.0.0")
        return (self._ip, "255.255.255.0", self._ip, "8.8.8.8")

    def mac(self):
        return b"\x00\x00\x00\x00\x00\x00"
