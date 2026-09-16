"""
tools/preview_server.py —— 网页在电脑上的本地预览服务
======================================================

为什么需要它？
    python_code/index.html 是给 ESP32-C3 上的微型 HTTP 服务用的，靠
    /status、/log、/get_mqtt_info、/wifi_scan 这些接口拿数据。直接双击
    打开 index.html 的话，那些接口全部 404，页面只会显示默认值，
    没法检查布局和交互（尤其是通道颜色对话框和右侧日志面板）。

    这个小服务用假数据把接口补齐，让你在电脑浏览器里就能把页面完整跑起来。

用法：
    python tools/preview_server.py              # 默认 8099 端口，联网模式
    python tools/preview_server.py 9000         # 指定端口
    python tools/preview_server.py 9000 ap      # ★ 以"配置热点"模式启动：
                                                #   左侧菜单会多出 WiFi 配置
然后浏览器打开 http://127.0.0.1:8099

注意：它只读 python_code/index.html，不会写任何文件，也不参与固件构建。
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "python_code", "index.html")

# 一份"看起来像真机"的假状态，方便肉眼检查各种情况的显示效果
STATUS = {
    "ok": True,
    "ip": "192.168.1.66",
    "ap_ip": "192.168.4.1",
    "ap_on": False,
    "ap_ssid": "AMS_WIFI",
    "wifi_isconnected": True,
    "wifi_ssid": "四叶草的家",
    "wifi_status_text": "已获取 IP",
    "is_mqtt_con": True,
    "mqtt_configured": True,
    "ssids": ["四叶草的家", "CNX-Software", "TP-LINK_5G", "Xiaomi_A1B2",
              "CMCC-8f3k", "HUAWEI-9x2p"],
    "color_list": ["#E53935", "#1E88E5", "#43A047", "#FB8C00"],
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
        "ok": True,
        "problems": [],
    },
    # 空闲内存：故意给个偏低的值，方便检查"偏低"告警样式
    "mem_free": 18432,
}

# 页面右下角日志面板的假数据。刻意混了正常行和 ★ 报错行，
# 方便检查"错误标红"有没有生效。
LOGS = [
    "00:00.001 配置页面 index.html（56445 字节）按 1024 字节分块发送，空闲内存 47832 字节",
    "00:00.412 Web 服务已启动，监听 0.0.0.0:80",
    "00:00.430 共享电机已初始化: IN1=GPIO4 IN2=GPIO5，4 路电磁离合 [6, 7, 10, 3]",
    "00:00.688 引脚配置自检通过：全部输出脚都在安全引脚上。",
    "00:01.204 WiFi 连接成功: 四叶草的家  IP=192.168.1.66",
    "00:02.910 扫描到 6 个 WiFi",
    "00:03.045 MQTT 配置已写入 config.json（IP=192.168.1.103 序列号=0309AA411201130）",
    "00:03.688 成功连接到 192.168.1.103, 订阅地址为 device/0309AA411201130/report topic",
    "00:06.120 请求: mqtt_connect",
    "00:06.340 网页手动点动: 通道3 方向1 1000ms",
    "00:07.402 通道3 点动结束，仍吸合的离合: 无",
    "00:09.882 ★ 连接MQTT失败[Errno 104] ECONNRESET",
    "00:10.110 未连接 WiFi，暂不连打印机",
    "00:12.330 状态字段 hardware 取值失败: 模拟故障",
    "00:14.006 复位原因 : BROWN_OUT_RESET（★ 欠压复位：供电电压掉到了阈值以下）",
]

MQTT_INFO = {
    "mqtt_server": "192.168.1.103",
    "DEVICE_SERIAL": "0309AA411201130",
    "mqtt_password": "21433590",
    "client_id": "mqttx_3c73cd31",
    "username": "bblp",
    "mqtt_port": "8883",
    "is_mqtt_con": True,
}


def set_ap(on):
    """切换预览用的"配置热点"状态"""
    STATUS["ap_on"] = bool(on)
    if on:
        # 真机上开热点时 STA 通常是断的（就是连不上才开的热点）
        STATUS["wifi_isconnected"] = False
        STATUS["wifi_ssid"] = ""
        STATUS["ip"] = ""
        STATUS["wifi_status_text"] = "空闲"
    else:
        STATUS["wifi_isconnected"] = True
        STATUS["wifi_ssid"] = "四叶草的家"
        STATUS["ip"] = "192.168.1.66"
        STATUS["wifi_status_text"] = "已获取 IP"


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
        with open(INDEX, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/":
            self._html()
        elif path == "/status":
            self._json(STATUS)
        elif path == "/log":
            self._json({"log": LOGS[-24:], "count": min(24, len(LOGS)),
                        "mem_free": STATUS["mem_free"]})
        elif path == "/get_mqtt_info":
            self._json(MQTT_INFO)
        elif path == "/wifi_scan":
            self._json({"ssids": STATUS["ssids"]})
        elif path == "/boot_clear":
            STATUS["reset"]["boot_count"] = 0
            self._json({"boot_count": 0})
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self._json({"info": "not found: " + path}, 404)

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        data = self._body()

        if path == "/wifi_scan":
            self._json({"ssids": STATUS["ssids"]})
        elif path == "/access_set":
            STATUS["access_list"] = data.get("access_list", STATUS["access_list"])
            STATUS["color_list"] = data.get("color_list", STATUS["color_list"])
            LOGS.append("%s 改变映射通道及其颜色 %s"
                        % (time.strftime("%H:%M:%S"), data.get("color_list")))
            self._json(info="切换通道成功")
        elif path == "/mqtt_connect":
            # 真机上是"先落盘，再连接"，这里照样演一遍，方便检查两种提示
            LOGS.append("%s MQTT 配置已写入 config.json（IP=%s）"
                        % (time.strftime("%H:%M:%S"), data.get("mqtt_server")))
            STATUS["mqtt_configured"] = True
            connected = bool(STATUS["wifi_isconnected"])
            if connected:
                self._json({"info": "配置已保存，MQTT 连接成功",
                            "saved": True, "connected": True})
            else:
                self._json({"info": "配置已保存；设备当前没联网，联网后会自动连接打印机",
                            "saved": True, "connected": False})
        elif path == "/wifi_connect":
            LOGS.append("%s 配网成功，已保存到 wifi.dat: %s"
                        % (time.strftime("%H:%M:%S"), data.get("name")))
            set_ap(False)          # 真机上配网成功会自动关热点
            self._json({"info": "%s 连接成功，IP = 192.168.1.66（热点已关闭）"
                                % data.get("name"),
                        "ip": "192.168.1.66", "wifi_ssid": data.get("name"),
                        "ap_on": False})
        elif path == "/hardware_test":
            LOGS.append("%s 网页手动点动: 通道%s 方向%s %sms"
                        % (time.strftime("%H:%M:%S"), data.get("channel"),
                           data.get("direction"), data.get("times_ms")))
            self._json(info="通道%s 动作完成，离合已全部断开" % data.get("channel"))
        elif path == "/ap_set":
            set_ap(data.get("on"))
            self._json({"info": "配置热点已%s" % ("打开，手机连上 AMS_WIFI 后访问 http://192.168.4.1"
                                              if STATUS["ap_on"] else "关闭"),
                        "ap_on": STATUS["ap_on"]})
        else:
            self._json({"info": "not found: " + path}, 404)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    if len(sys.argv) > 2 and sys.argv[2].lower() == "ap":
        set_ap(True)
        print("预览模式: 配置热点（左侧菜单会显示 WiFi 配置）")
    print("预览地址: http://127.0.0.1:%d" % port)
    print("页面来源: %s" % INDEX)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
