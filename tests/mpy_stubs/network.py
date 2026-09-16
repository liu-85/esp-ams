"""
tests/mpy_stubs/network.py
==========================
`network` 模块的桌面端桩实现，只在 PC / CI 上跑测试用。
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
        self._ip = "192.168.4.1" if interface == AP_IF else "0.0.0.0"

    def active(self, *args):
        if args:
            self._active = bool(args[0])
            return None
        return self._active

    def isconnected(self):
        return self._connected

    def status(self):
        return STAT_GOT_IP if self._connected else STAT_IDLE

    def scan(self):
        return []

    def connect(self, ssid, password=None, *args, **kwargs):
        self._ssid = ssid
        self._connected = True

    def disconnect(self):
        self._connected = False

    def config(self, **kwargs):
        if "essid" in kwargs:
            self._ssid = kwargs["essid"]
        return None

    def ifconfig(self, *args, **kwargs):
        if args:
            return None
        return (self._ip, "255.255.255.0", self._ip, "8.8.8.8")

    def mac(self):
        return b"\x00\x00\x00\x00\x00\x00"
