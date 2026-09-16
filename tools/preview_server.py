"""
tools/preview_server.py —— 网页在电脑上的本地预览服务
======================================================

为什么需要它？
    python_code/index.html 是给 ESP32-C3 上的微型 HTTP 服务用的，靠
    /status、/get_mqtt_info、/wifi_scan 这些接口拿数据。直接双击打开
    index.html 的话，那些接口全部 404，页面只会一直显示"加载中"，
    没法检查布局和交互（尤其是通道颜色对话框）。

    这个小服务用假数据把接口补齐，让你在电脑浏览器里就能把页面完整跑起来。

用法：
    python tools/preview_server.py            # 默认 8099 端口
    python tools/preview_server.py 9000       # 指定端口
然后浏览器打开 http://127.0.0.1:8099

注意：它只读 python_code/index.html，不会写任何文件，也不参与固件构建。
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "python_code", "index.html")

# 一份"看起来像真机"的假状态，方便肉眼检查各种情况的显示效果
STATUS = {
    "ip": "192.168.1.66",
    "ap_ip": "192.168.4.1",
    "ap_on": False,
    "ap_ssid": "AMS_WIFI",
    "wifi_isconnected": True,
    "wifi_ssid": "四叶草的家",
    "wifi_status_text": "已获取 IP",
    "is_mqtt_con": True,
    "ssids": ["四叶草的家", "CNX-Software", "TP-LINK_5G", "Xiaomi_A1B2",
              "CMCC-8f3k", "HUAWEI-9x2p"],
    "color_list": ["#FF0000", "#00A0FF", "#2ECC40", "#FFDC00"],
    "access_list": [1, 2, 3, 4],
    "current_access": 2,
    "hardware": {
        "active_channel": None,
        "owner": None,
        "engaged": [],
        "conflicts": 0,
        "motor_direction": 0,
        "channels": [1, 2, 3, 4],
        "limits": [False, False, False, False],
    },
    # 刻意做成"接上负载后一直重启"的场景，方便肉眼检查诊断卡片的告警样式
    "reset": {
        "cause": "BROWN_OUT_RESET",
        "cause_desc": "★ 欠压复位：供电电压掉到了阈值以下。负载浪涌把电源拉塌了，"
                      "先查电源功率、线径、共地和 VM 的滤波电容",
        "boot_count": 37,
        "uptime_ms": 83000,
        "power_suspect": True,
    },
    "boot_safety": {
        "ok": False,
        "report": "---- 硬件配置 ----\n共享电机 : IN1=GPIO2  IN2=GPIO3",
        "problems": [
            "GPIO2（电机 IN1）是 strapping 启动模式脚，绝对不能做电机输出！"
            "AT8236 的 IN1/IN2 内置下拉电阻，上电瞬间会把它拉低，"
            "芯片将进不了正常启动模式。请改到 [0, 1, 4, 5, 6, 7, 10] 里的引脚",
            "GPIO3（电机 IN2）是 strapping 启动模式脚，绝对不能做电机输出！"
            "AT8236 的 IN1/IN2 内置下拉电阻，上电瞬间会把它拉低，"
            "芯片将进不了正常启动模式。请改到 [0, 1, 4, 5, 6, 7, 10] 里的引脚",
        ],
    },
}

MQTT_INFO = {
    "mqtt_server": "192.168.1.103",
    "DEVICE_SERIAL": "0309AA411201130",
    "mqtt_password": "21433590",
    "client_id": "mqttx_3c73cd31",
    "username": "bblp",
    "mqtt_port": "8883",
    "is_mqtt_con": True,
}


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self):
        with open(INDEX, "r", encoding="utf-8") as f:
            body = f.read().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/":
            self._html()
        elif path == "/status":
            self._json(STATUS)
        elif path == "/get_mqtt_info":
            self._json(MQTT_INFO)
        elif path == "/wifi_scan":
            self._json({"ssids": STATUS["ssids"]})
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self._json({"info": "not found: " + path}, 404)

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}

        if path == "/wifi_scan":
            self._json({"ssids": STATUS["ssids"]})
        elif path == "/access_set":
            STATUS["access_list"] = data.get("access_list", STATUS["access_list"])
            STATUS["color_list"] = data.get("color_list", STATUS["color_list"])
            print("收到通道设置:", data.get("access_list"), data.get("color_list"))
            self._json({"info": "切换通道成功"})
        elif path in ("/wifi_connect", "/mqtt_connect"):
            print("收到", path, json.dumps(data, ensure_ascii=False))
            self._json({"info": "（预览模式）模拟成功"})
        elif path == "/hardware_test":
            print("收到点动:", data)
            self._json({"info": "通道%s 动作完成，离合已全部断开" % data.get("channel")})
        else:
            self._json({"info": "not found: " + path}, 404)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    print("预览地址: http://127.0.0.1:%d" % port)
    print("页面来源: %s" % INDEX)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
