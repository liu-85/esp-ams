"""
AMS_WEB.py —— Web 配置页面 + 任务调度
=====================================

本文件负责：
    · 起一个极简 HTTP 服务，给手机/电脑提供配置页面
    · 处理配网、MQTT 配置、通道映射与颜色、硬件手动调试这些接口
    · 用 uasyncio 把「状态灯 / Web 服务 / 换料主循环」三个任务并发跑起来

--------------------------------------------------------------------------
性能上踩过的坑（这一版重点修的就是这些）
--------------------------------------------------------------------------
1. **index.html 不能整份读进内存（这一版最重要的修复）**
   页面有 40 KB 左右。ESP32-C3 的空闲堆只有几十 KB 且碎片化严重，
   `f.read()` 要一次性拿到连续的 36~42 KB —— 直接
   `memory allocation failed, allocating 36096 bytes`。
   更坑的是：原来第二次请求想复用缓存，但缓存根本没写进去，
   于是**每个请求都在同一处再失败一次**，页面永远打不开（串口日志疯狂刷错）。
   现在的做法：`os.stat()` 取文件长度写进 Content-Length，
   然后每次只读 FILE_CHUNK（1 KB）字节直接 sendall，块间 await 让步。
   单次最大分配从 40 KB 降到 1 KB，也不再有第二份 `.encode()` 拷贝。

   （历史上还曾经是一行一行发的：`for line in f: sendall(line); sleep_ms(10)`，
   400 多行光发 HTML 就要 4 秒以上，而且每行一个 TCP 小包。）

2. **accept 循环里 `await asyncio.sleep_ms(500)`**
   每个请求平均要多等 250ms 才能被受理；浏览器一次要发好几个请求，
   串起来就是好几秒。现在改成 20ms 轮询。

3. **请求头读取用的是阻塞 recv + 3 秒超时**
   浏览器会开"预连接"套接字却什么都不发，服务端就傻等 3 秒，
   整个事件循环被卡住 → 表现就是"网页偶尔打不开"。
   现在：非阻塞读 + 让出 CPU，总等待上限 0.6 秒，等不到就丢掉这个连接。

4. **一次页面加载要发 5 个请求**
   （ip / wifi / mqtt / access / hardware）→ 单线程服务端串行处理，累计很慢。
   现在：新增 /status 聚合接口，一次返回全部状态；前端只发这一个，之后每 2 秒轮询。

5. **MQTT ping 每次都真的发**
   旧代码 `check_mqtt_connection()` 直接 ping，而它被状态灯任务每秒调一次，
   同时也被网页接口调。现在按 MQTT_PING_INTERVAL_MS 节流。

6. **GC 阈值曾经被设成 1KB**
   等于"每分配 1KB 就做一次垃圾回收"，在解析 JSON、发网页时开销极大。
   现在放宽到 16KB。

--------------------------------------------------------------------------
接口一览
--------------------------------------------------------------------------
    GET  /                  配置页面（index.html）
    GET  /status            ★ 聚合状态：IP/WiFi/MQTT/通道/颜色/硬件/复位诊断
    GET  /wifi_scan         强制重新扫描 WiFi（阻塞约 2 秒，仅用户点击时调用）
    GET  /boot_clear        把"启动次数"清零（排查复位循环时用）
    POST /wifi_connect      {"name":ssid,"password":pwd}
    POST /mqtt_connect      {"mqtt_server":...,"DEVICE_SERIAL":...,...}
    POST /access_set        {"access_list":[...],"color_list":[...]}
    POST /hardware_test     {"channel":1,"direction":1,"times_ms":1000}

兼容保留（老页面还能用）：/get_ip_info /get_wifi_info /get_mqtt_info
                          /get_access_info /get_hardware_info
"""

import os
import socket
import ure
import time
import ujson
import gc
import uasyncio as asyncio

from logout import logout, recent as recent_logs
from AMS_MODEL import AMS
from device_processing import BOOT_SAFETY
from machine import Pin, PWM
from info_load import read_profiles, write_profiles, read_json_file, write_json_file
from hardware_config import LED_PIN, CONFIG_FILE
from motor_clutch import MotorBusError, ClutchConflictError
import reset_info

# ---------------------------------------------------------------------------
# 垃圾回收
# ---------------------------------------------------------------------------
gc.enable()
# 旧值是 1024（每分配 1KB 就回收一次），发个网页要触发几十次 GC。
# 放宽到 16KB，内存占用仍然安全（ESP32-C3 空闲堆有一百多 KB），
# 但解析 JSON / 发页面时几乎不再触发 GC。
try:
    gc.threshold(16384)
except Exception:
    pass

json_file = CONFIG_FILE
INDEX_FILE = "index.html"

# ---------------------------------------------------------------------------
# Web 服务参数
# ---------------------------------------------------------------------------
WEB_PORT = 80
WEB_POLL_MS = 20            # accept 轮询间隔；越小网页响应越快，20ms 兼顾性能与开销
HEADER_WAIT_MS = 600        # 读请求头的**最长等待**；浏览器预连接会空等，不能设太长
BODY_WAIT_MS = 800          # 读完请求头后，再给请求体这么多时间（POST 才有）
SEND_TIMEOUT_S = 3.0        # 发送阶段超时，防止客户端半死不活把服务端拖住
# 请求（头 + 体）总量上限。MQTT 配置这份 JSON 约 300 字节，
# 5120 足够宽裕，异常请求会在这里被截断丢弃。
MAX_REQUEST_BYTES = 5120
SEND_CHUNK = 2048           # 内存里已有的二进制响应分片发送的块大小
# ★ 字符串响应是"切块 → 逐块 encode"发出去的，这是单块字符数。
#   512 个中文字符最多 1.5 KB，保证不会一次要一大块连续内存。
ENC_CHUNK = 512
# ★ 从 flash 读文件时的单块大小。这个值直接决定了"服务网页时的最大单次内存分配"，
#   必须远小于空闲堆：1 KB 在任何情况下都能分配出来，而且块间 await 让步，
#   发 40 KB 页面时其它任务不会饿死。**不要调大！**
FILE_CHUNK = 1024
MQTT_PING_INTERVAL_MS = 5000  # MQTT 存活探测节流（keepalive=60s，5 秒一次足够）
LOG_TAIL = 24               # /log 一次给网页多少行（和 logout.LOG_MAX_LINES 对齐）

_REASON = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    404: "Not Found",
    500: "Internal Server Error",
    503: "Service Unavailable",
}

# 需要请求体的写接口。这些路径收到空请求体时应该回 400 说明情况，
# 绝不能掉进 404 —— 那会让用户以为是"接口不存在"，实际上只是请求体没读到。
WRITE_ROUTES = ("wifi_connect", "mqtt_connect", "access_set", "hardware_test")

# 内存不够时的兜底页面：故意做得极小（几百字节，任何时候都发得出去），
# 至少让浏览器有内容可显示，而不是一片空白让人摸不着头脑。
_OOM_PAGE = (
    "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>内存不足</title></head><body style='font-family:sans-serif;padding:24px'>"
    "<h2 style='color:#d33'>设备内存不足</h2>"
    "<p>配置页面无法发送：ESP32-C3 没能分配到足够的连续内存。</p>"
    "<p>把板子断电重插再试；若反复出现，请把串口日志里带『空闲内存』的那几行发出来。</p>"
    "</body></html>"
)


# ---------------------------------------------------------------------------
# 内存 / 文件长度小工具
# ---------------------------------------------------------------------------
def file_size(path):
    """取文件字节数；文件不存在就返回 None。

    ★ MicroPython 的 os.stat() 返回的是元组，**没有 st_size 属性**，
      只能按下标取第 7 项（size）。
    """
    try:
        return os.stat(path)[6]
    except OSError:
        return None


def mem_free():
    """当前空闲堆字节数。

    MicroPython 有 gc.mem_free()，桌面端 CPython 没有 —— 返回 -1 表示"不知道"。
    """
    try:
        return gc.mem_free()
    except Exception:
        return -1


def mem_note():
    """给日志用的一小段内存提示，OOM 时就是靠它判断余量"""
    return "空闲内存 %d 字节" % mem_free()


# ---------------------------------------------------------------------------
# 请求解析 / 状态组装的小工具
# ---------------------------------------------------------------------------
def _content_length(head):
    """从请求头里取出 Content-Length（没有就返回 0）。

    ★ 不能用 ure 直接搜 bytes：不同 MicroPython 版本对 bytes + 正则的
      支持不一致，而且这里只是找一个十进制数，手工扫更稳更省。
    """
    try:
        lower = head.lower()
        pos = lower.find(b"content-length:")
        if pos < 0:
            return 0
        end = lower.find(b"\r\n", pos)
        if end < 0:
            end = len(head)
        return int(head[pos + 15:end].decode().strip())
    except Exception:
        return 0


def _safe(getter, default, field):
    """取状态字段失败时不要连累整个 /status。

    /status 是网页上所有卡片的唯一数据来源，任何一个字段抛异常都会让
    "运行状态全是 - 、通道一直加载中"。所以逐字段兜底，并在日志里点名是谁。
    """
    try:
        value = getter()
        return default if value is None else value
    except Exception as e:
        logout("状态字段 %s 取值失败: %s" % (field, e), is_error=True)
        return default


def boot_safety_lite():
    """只把网页真正用到的自检信息取出来。

    ★ 绝对不要把 BOOT_SAFETY["report"] 塞进 /status：那是整段中文接线表，
      ujson.dumps 之后体积要翻好几倍，空闲堆小的板子上直接
      `memory allocation failed` —— 现象就是"页面能开、状态接口全挂"。
    """
    problems = []
    try:
        for line in (BOOT_SAFETY.get("problems") or [])[:4]:
            problems.append(str(line)[:120])
    except Exception:
        pass
    return {"ok": bool(BOOT_SAFETY.get("ok", True)), "problems": problems}


# 写接口字段的中文名，报错时给用户看人话
MQTT_LABEL = {
    "mqtt_server": "打印机 IP",
    "DEVICE_SERIAL": "设备序列号",
    "mqtt_password": "访问码",
    "username": "服务端用户名",
    "client_id": "客户端名称",
    "mqtt_port": "MQTT 端口",
}
MQTT_DEFAULTS = {
    "username": "bblp",
    "client_id": "mqttx_3c73cd31",
    "mqtt_port": "8883",
}
# 这几项为空就不让保存（其余用默认值补齐）
MQTT_REQUIRED = ("mqtt_server", "DEVICE_SERIAL", "mqtt_password")


class AMS_WEB(AMS):
    def __init__(self):
        super().__init__()
        self.server_socket = None
        # 状态灯：LED_PIN 设成 None 就整套跳过。
        # （GPIO2 是 strapping 脚，想让出来 / 不想闪灯就把它设成 None）
        if LED_PIN is None:
            self.LED = None
        else:
            self.LED = PWM(Pin(LED_PIN))
            self.LED.freq(1000)
            self.LED.duty(0)
        # ★ 注意：这里**故意没有** index.html 的内存缓存。
        #   页面 40 KB，缓存进内存会让每个请求都要连续分配 40 KB → 必然 OOM。
        #   改成打开文件、分块 sendall（见 send_file）。
        self._sent = 0                # 本次连接已发出的字节数（OOM 兜底时判断还能不能回包）
        self._page_logged = False     # 页面首次发送的日志只打一次，别刷屏

    # ======================================================================
    # 配置读写
    # ======================================================================
    def updata_data(self, updata_data):
        """把 updata_data 合并进 config.json。

        注意：全新烧录的板子上还没有 config.json，read_json_file 会返回 None，
              这里必须兜底成空字典，否则首次配置会抛 AttributeError。
        """
        json_data = read_json_file(json_file) or {}
        json_data.update(updata_data)
        write_json_file(json_file, json_data)
        return json_data

    # ======================================================================
    # HTTP 基础
    # ======================================================================
    def _raw_send(self, client, data):
        """唯一的出口：记录已发字节数，供 OOM 兜底判断"还能不能回一段话"。

        响应发到一半再触发内存错误时，绝不能再补一个新的 HTTP 响应头
        （那会把响应体拼坏），所以必须知道当前连接是不是干净的。
        """
        client.sendall(data)
        self._sent += len(data)

    def send_header(self, client, status_code=200, content_length=None,
                    is_json=False, ctype=None, extra=None):
        if ctype is None:
            ctype = ("application/json; charset=UTF-8" if is_json
                     else "text/html; charset=UTF-8")

        head = "HTTP/1.1 %d %s\r\n" % (status_code, _REASON.get(status_code, "OK"))
        head += "Content-Type: %s\r\n" % ctype
        head += "Access-Control-Allow-Origin: *\r\n"
        head += "Cache-Control: no-store\r\n"
        head += "Connection: close\r\n"
        if content_length is not None:
            head += "Content-Length: %d\r\n" % content_length
        if extra:
            head += extra
        head += "\r\n"
        self._raw_send(client, head)

    @staticmethod
    def _str_chunks(text, size=ENC_CHUNK):
        """把长字符串切成小块、逐块 encode。"""
        for i in range(0, len(text), size):
            yield text[i:i + size].encode()

    @staticmethod
    def _str_bytes_len(text, size=ENC_CHUNK):
        """字符串编码成 UTF-8 之后的字节数。

        ★ 不能用 len(text) 当 Content-Length：中文一个字在 MicroPython 里是
          1 个字符、3 个字节（有些版本还会转义成 \\uXXXX 的 6 个字节）。
          这里按同样的切块方式累加，峰值仍然只有一小块。
        """
        total = 0
        for i in range(0, len(text), size):
            total += len(text[i:i + size].encode())
        return total

    def send_response(self, client, payload, status_code=200, is_json=False,
                      ctype=None, extra=None):
        """发送内存里已有的响应。

        ★ 字符串是"切块 → 逐块 encode → 逐块发"的。旧写法先整体
          `payload.encode()` 再做一份完整拷贝，等于峰值要两份响应体；
          /status 这种 JSON 一大就会把空闲堆吃光，于是"页面能打开、状态
          接口却一直失败"。现在峰值固定在一小块。
        """
        if isinstance(payload, str):
            self.send_header(client, status_code, self._str_bytes_len(payload),
                             is_json=is_json, ctype=ctype, extra=extra)
            for piece in self._str_chunks(payload):
                self._raw_send(client, piece)
            return True

        self.send_header(client, status_code, len(payload),
                         is_json=is_json, ctype=ctype, extra=extra)
        total = len(payload)
        offset = 0
        while offset < total:
            self._raw_send(client, payload[offset:offset + SEND_CHUNK])
            offset += SEND_CHUNK
        return True

    async def send_file(self, client, path, ctype=None, extra=None):
        """把磁盘上的文件分块流式发给客户端，返回是否成功。

        ★ 这是"页面打不开"的根治办法，也是本文件最不能改回整份读的地方：
          ESP32-C3 的空闲堆只有几十 KB 且碎片化，`f.read()` 一次要连续 40 KB
          → `memory allocation failed`。而且缓存写不进去，每个请求都会
          在同一个地方再失败一次，表现就是"AP 能连上、管理页永远打不开"。

          现在：文件长度用 os.stat 取（写进 Content-Length，浏览器才知道何时结束），
          正文每次只读 FILE_CHUNK 字节读一块发一块 —— 单次最大分配 1 KB，
          也没有第二份 `.encode()` 拷贝；块间 await 让步，发大页面时
          换料主循环和状态灯不会被饿死。

        ctype：默认 text/html；读出来的是二进制原文，不做任何解码。
        """
        size = file_size(path)
        if size is None:
            logout("读取 %s 失败：文件不存在或无法访问（%s）" % (path, mem_note()),
                   is_error=True)
            self.send_response(client, "文件缺失: %s" % path, status_code=500)
            return False

        gc.collect()                 # 先把碎片收拢，再开始发
        self.send_header(client, 200, size, ctype=ctype, extra=extra)

        sent = 0
        with open(path, "rb") as f:  # 必须 rb：按原始字节发，避免任何解码缓冲
            while True:
                chunk = f.read(FILE_CHUNK)
                if not chunk:
                    break
                self._raw_send(client, chunk)
                sent += len(chunk)
                await asyncio.sleep_ms(0)   # ★ 让出事件循环，别把其它任务饿死

        if sent != size:
            logout("%s 实际发出 %d 字节，与 Content-Length %d 不一致" % (path, sent, size),
                   is_error=True)
        return True

    def oom_respond(self, client):
        """内存不足时的兜底：只在"一个字都还没发出去"时才补一个 503。

        否则（响应已发一半）宁可断开，也不能把半个响应拼上去。
        """
        if self._sent:
            return False
        try:
            self.send_response(client, _OOM_PAGE, status_code=503)
            return True
        except Exception:
            return False

    def process_json(self, request):
        """从原始请求里抠出 JSON 请求体"""
        json_start = request.find(b'{')
        if json_start == -1:
            return None
        try:
            return ujson.loads(request[json_start:])
        except ValueError as e:
            logout("解析请求 JSON 失败: " + str(e), is_error=True)
            return None

    def handle_not_found(self, client, url):
        self.send_response(client, "Path not found: %s" % url, status_code=404)

    # ======================================================================
    # 状态聚合（网页主接口）
    # ======================================================================
    def _hardware_dict(self):
        data = self.motor_bus.status()   # active_channel / engaged / conflicts / motor_direction
        data["limits"] = [mat.has_limit for mat in self.meterial_list]
        data["channels_pin"] = list(self.motor_bus.clutches.keys())
        return data

    def _status_dict(self):
        """一次把页面需要的所有状态凑齐，避免前端发 5 个请求。

        ★ 两条硬规矩，改这个函数前先读一遍接口头的第 8 条坑：
          1. 每个字段都要用 _safe() 兜住 —— 一个字段抛异常就让整页空白，
             太不划算（而且现象是"页面能打开、数据全是 -"，极难排查）。
          2. 只放网页真正要用的东西。中文越长、JSON 越大，板子越吃不消。
             boot_safety 只给 ok + problems，不给那一整段 report。

        另外 ssids 走缓存：真正的扫描只在用户点「重新扫描」时才做，
        否则每次刷新页面都会因为 scan() 阻塞 2 秒。
        """
        return {
            "ok": True,
            "ip": _safe(self.sta_ip, "", "ip"),
            "ap_ip": _safe(self.ap_ip, "", "ap_ip"),
            "ap_on": _safe(self.ap_is_on, False, "ap_on"),
            "ap_ssid": _safe(lambda: self.ap_ssid, "AMS_WIFI", "ap_ssid"),
            "wifi_isconnected": _safe(self.wlan_sta.isconnected, False, "wifi_isconnected"),
            "wifi_ssid": _safe(self.current_ssid, "", "wifi_ssid"),
            "wifi_status_text": _safe(self.status_text, "", "wifi_status_text"),
            "is_mqtt_con": _safe(self.check_mqtt_connection, False, "is_mqtt_con"),
            # 只给前 12 个：SSID 一多，这段 JSON 就会明显变胖
            "ssids": _safe(lambda: list(self._scan_cache[:12]), [], "ssids"),
            "color_list": _safe(lambda: self.color_list, [], "color_list"),
            "access_list": _safe(lambda: self.access_list, [], "access_list"),
            "current_access": _safe(lambda: self.filament_current, 0, "current_access"),
            "hardware": _safe(self._hardware_dict, {}, "hardware"),
            # 复位诊断：接负载后"一直重启"到底是欠压还是引脚接错，看这两个字段
            "reset": _safe(reset_info.summary, {}, "reset"),
            "boot_safety": _safe(boot_safety_lite, {"ok": True, "problems": []},
                                 "boot_safety"),
            # 内存余量：出现「页面打不开 / memory allocation failed」时先看它
            "mem_free": mem_free(),
        }

    async def get_status(self, client):
        self.send_response(client, ujson.dumps(self._status_dict()), is_json=True)

    async def get_log(self, client):
        """设备日志（网页右侧的「设备日志」面板）。

        ★ 单独开一个接口，不要塞进 /status：
          日志行数一变，/status 的体积就跟着变，而 /status 是要每 2 秒
          被打一次的 —— 它一旦变大，板子就会开始 OOM。
        """
        lines = recent_logs(LOG_TAIL)
        data = {"log": lines, "count": len(lines), "mem_free": mem_free()}
        self.send_response(client, ujson.dumps(data), is_json=True)

    def handle_bad_body(self, client, url):
        """写接口收到空/坏请求体。

        ★ 这里是"Mqtt设置保存提示404"的正面修法：旧代码请求体没读到时
          data 是 None，路由条件不成立，就落到 handle_not_found 回了 404。
          用户看到 404 只会以为接口不存在，其实只是请求体没读全。
        """
        logout("写接口 %s 收到空请求体（请求体可能没读全，或前端没带 JSON）" % url,
               is_error=True)
        self.send_response(client,
                           ujson.dumps({"ok": False, "saved": False, "connected": False,
                                        "info": "请求体为空或格式错误，请重试"}),
                           status_code=400, is_json=True)

    async def handle_boot_clear(self, client):
        """把启动计数清零。

        排查复位循环的用法：清零 → 拔电重插 → 再看数字。
        如果一次上电就涨了几十，说明板子在反复重启。
        """
        reset_info.clear_boot_count()
        self.send_response(client, ujson.dumps({"boot_count": 0}), is_json=True)

    async def handle_wifi_scan(self, client):
        """强制扫描（用户点「重新扫描 WiFi」）。会阻塞约 2 秒，先把响应头发出去。"""
        await asyncio.sleep_ms(20)
        ssids = self.scan_networks(force=True)
        self.send_response(client, ujson.dumps({"ssids": ssids}), is_json=True)

    # ======================================================================
    # 兼容老接口（逐个返回，老页面/调试脚本还能用）
    # ======================================================================
    async def get_wifi_info(self, client):
        data = {
            "wifi_isconnected": self.wlan_sta.isconnected(),
            "wifi_ssid": self.current_ssid(),
            "ap_on": self.ap_is_on(),
            "ssids": self._scan_cache,
        }
        self.send_response(client, ujson.dumps(data), is_json=True)

    async def get_mqtt_info(self, client):
        data = {
            "DEVICE_SERIAL": self.DEVICE_SERIAL,
            "mqtt_password": self.password,
            "mqtt_port": self.mqtt_port,
            "mqtt_server": self.mqtt_server,
            "client_id": self.client_id,
            "username": self.username,
            "is_mqtt_con": self.check_mqtt_connection(),
        }
        self.send_response(client, ujson.dumps(data), is_json=True)

    async def get_access_info(self, client):
        data = {
            "color_list": self.color_list,
            "access_list": self.access_list,
            "current_access": self.filament_current,
        }
        self.send_response(client, ujson.dumps(data), is_json=True)

    async def get_hardware_info(self, client):
        self.send_response(client, ujson.dumps(self._hardware_dict()), is_json=True)

    async def get_ip_info(self, client):
        data = {"ip": self.sta_ip(), "ap_ip": self.ap_ip(), "ap_on": self.ap_is_on()}
        self.send_response(client, ujson.dumps(data), is_json=True)

    # ======================================================================
    # 首页
    # ======================================================================
    async def hanld_rootv2(self, client):
        """发送配置页面（从 flash 分块流式发送）。

        ★ 千万不要改回"整份读进内存"：
          页面有 40 KB，ESP32-C3 的空闲堆只有几十 KB 且碎片化，`f.read()`
          必然报 `memory allocation failed, allocating 36096 bytes`；
          更糟的是缓存写不进去，于是每个请求都在同一处再失败一次，
          现象就是"AP 能连上、管理页怎么都打不开"。
          具体取舍见 send_file() 的注释。
        """
        if not self._page_logged:
            self._page_logged = True
            logout("配置页面 %s（%d 字节）按 %d 字节分块发送，%s"
                   % (INDEX_FILE, file_size(INDEX_FILE) or 0, FILE_CHUNK, mem_note()))
        await self.send_file(client, INDEX_FILE)

    # 语义更清楚的新名字
    handle_root = hanld_rootv2

    # ======================================================================
    # 手动点动调试（网页"硬件调试"用）
    # 请求体示例：{"channel":2,"direction":1,"times_ms":1000}
    # 该接口同样受"同一时刻只能 1 路离合吸合"的约束保护
    # ======================================================================
    def handle_hardware_test(self, client, data):
        dict_info = {"info": None}
        try:
            if not isinstance(data, dict):
                raise ValueError("请求体格式不正确")
            channel = int(data.get("channel", 1))
            direction = int(data.get("direction", 1))
            times_ms = int(data.get("times_ms", 1000))
            if direction not in (1, -1):
                raise ValueError("direction 只能是 1(进料) 或 -1(退料)")
            if times_ms <= 0:
                raise ValueError("times_ms 必须大于 0")
            if times_ms > 5000:
                times_ms = 5000          # 安全上限，防止网页误操作把料顶坏
            logout("网页手动点动: 通道%s 方向%s %sms" % (channel, direction, times_ms))
            self.motor_bus.run(channel, direction, times_ms,
                               release=True, owner="web-test")
            self.motor_bus.assert_single()   # 动作结束后复核一次
            dict_info["info"] = "通道%d 动作完成，离合已全部断开" % channel
            self.send_response(client, ujson.dumps(dict_info), is_json=True)
        except MotorBusError as e:
            dict_info["info"] = "总线拒绝执行: " + str(e)
            self.send_response(client, ujson.dumps(dict_info), status_code=400, is_json=True)
        except Exception as e:
            dict_info["info"] = "执行失败: " + str(e)
            self.send_response(client, ujson.dumps(dict_info), status_code=400, is_json=True)

    # ======================================================================
    # 连接 WiFi
    # ======================================================================
    def handle_wifi_cennect(self, client, data):
        logout("收到配网请求")
        dict_info = {"info": None}
        if data is None:
            dict_info["info"] = "参数缺失"
            self.send_response(client, ujson.dumps(dict_info), status_code=400, is_json=True)
            return False
        if "name" not in data:
            dict_info["info"] = "必须提供 WiFi 名称和密码"
            self.send_response(client, ujson.dumps(dict_info), status_code=400, is_json=True)
            return False

        ssid = data["name"]
        password = data.get("password", "")
        if not ssid:
            dict_info["info"] = "WiFi 名称不能为空"
            self.send_response(client, ujson.dumps(dict_info), status_code=400, is_json=True)
            return False

        if self.do_connect(ssid, password):
            ip = self.sta_ip()
            dict_info["info"] = "%s 连接成功，IP = %s" % (ssid, ip or "(等待分配)")
            dict_info["ip"] = ip
            dict_info["wifi_ssid"] = ssid
            dict_info["ap_on"] = self.ap_is_on()
            self.send_response(client, ujson.dumps(dict_info), is_json=True)

            # 把成功的账号密码记下来，下次开机直接连
            try:
                profiles = read_profiles()
            except OSError:
                profiles = {}
            profiles[ssid] = password
            write_profiles(profiles)
            # 顺手同步进 config.json，方便一眼看到当前用的 WiFi
            self.updata_data({"wifi_username": ssid, "wifi_password": password})
            logout("配网成功，已保存到 wifi.dat: %s" % ssid)

            # ★ 配网成功后关掉配置热点。
            #   这是"WiFi 连上之后就不再显示配网页面"能成立的前提：
            #   网页只在 AP 模式下显示 WiFi 配置卡片，热点一关它就消失了。
            #   先等一下再关，否则这句响应还压在缓冲里，客户端会直接断掉。
            if self.ap_is_on():
                self._sleep_ms(800)
                self.swcith_ap(0)
                logout("已关闭配置热点，请改用 http://%s 访问" % (ip or "新的 IP"))
                dict_info["ap_on"] = False
            return True

        dict_info["info"] = "连接失败（%s），请检查密码或确认路由器 2.4G 频段已开启" % self.status_text()
        self.send_response(client, ujson.dumps(dict_info), status_code=400, is_json=True)
        return False

    @staticmethod
    def _sleep_ms(ms):
        """阻塞一小会儿（只在"已经回完包"的收尾动作里用，比如关热点前等发包）。

        MicroPython 有 time.sleep_ms；桌面自测的桩里不一定有，所以兜一下。
        """
        try:
            time.sleep_ms(ms)
        except AttributeError:
            try:
                time.sleep(ms / 1000.0)
            except Exception:
                pass
        except Exception:
            pass

    # ======================================================================
    # 配置热点开关（网页「运行状态」上的按钮）
    # ======================================================================
    def handle_ap_set(self, client, data):
        """打开 / 关闭配置热点。

        联网之后网页上的 WiFi 配置卡片就藏起来了，所以必须留一个
        "把热点再打开"的入口，否则想换 WiFi 就只能重新刷机。
        """
        want = 1
        try:
            if isinstance(data, dict) and "on" in data:
                want = 1 if int(data["on"]) else 0
        except Exception:
            want = 1

        self.swcith_ap(want)
        on = self.ap_is_on()
        info = {
            "info": "配置热点已%s%s" % ("打开" if on else "关闭",
                                      ("，手机连上 %s 后访问 http://%s"
                                       % (self.ap_ssid, self.ap_ip())) if on else ""),
            "ap_on": on,
        }
        self.send_response(client, ujson.dumps(info), is_json=True)
        return on

    # ======================================================================
    # 改变映射通道 / 颜色
    # ======================================================================
    def handle_access_cenect(self, client, data):
        logout("改变映射通道及其颜色")
        dict_info = {"info": None}
        try:
            new_data = data["access_list"]
            new_colors = data.get("color_list", self.color_list)
        except Exception:
            dict_info["info"] = "参数缺失"
            self.send_response(client, ujson.dumps(dict_info), status_code=400, is_json=True)
            return False

        if sum(set(new_data)) != sum(self.access_list) or len(set(new_data)) != len(new_data):
            dict_info["info"] = "请确定料盘编号"
            self.send_response(client, ujson.dumps(dict_info), status_code=400, is_json=True)
            return False
        for value in new_data:
            if value not in self.access_list:
                dict_info["info"] = "请确定料盘编号"
                self.send_response(client, ujson.dumps(dict_info), status_code=400, is_json=True)
                return False

        self.color_list = new_colors
        if self.filament_current > 0:
            index = self.access_list.index(self.filament_current)
            self.filament_current = new_data[index]

        self.access_list = new_data
        self.updata_data({"access": new_data, "color_list": self.color_list})
        self.dianji_dict = {key: value for key, value in zip(self.access_list, self.meterial_list)}
        dict_info["info"] = "切换通道成功"
        self.send_response(client, ujson.dumps(dict_info), is_json=True)
        return True

    # ======================================================================
    # 连接 MQTT
    # ======================================================================
    def handle_mqtt_cennect(self, client, data):
        """保存打印机 MQTT 配置。

        ★ 和旧版最大的区别：**先落盘，再连接**。
          旧代码是 `if self.conent_and_subscribe(): ... write_json_file(...)`，
          也就是说只有 MQTT 当场连上才会写配置。可是在 AP 配置模式下根本
          没联网，MQTT 必然连不上 —— 于是"保存"永远失败，config.json 里
          永远空空如也，重启之后还得重填。现在：
            1. 参数校验 → 写进 config.json（这一步一定做）
            2. 再尝试连接；连不上只作为提示返回，配置已经存好了
            3. 返回 saved / connected 两个字段，让网页能给出准确的说法
        """
        logout("配置 MQTT")
        info = {"info": None, "saved": False, "connected": False}
        if not isinstance(data, dict):
            info["info"] = "请求体格式不正确"
            self.send_response(client, ujson.dumps(info), status_code=400, is_json=True)
            return False

        # 必填项：缺一个就没法连打印机。其余（用户名/客户端名/端口）用默认值补齐，
        # 旧代码要求 6 项全部非空，随手留空一个就报错，太苛刻。
        for key in MQTT_REQUIRED:
            if not str(data.get(key) or "").strip():
                info["info"] = MQTT_LABEL.get(key, key) + " 不能为空"
                self.send_response(client, ujson.dumps(info), status_code=400, is_json=True)
                return False

        clean = {}
        for key in MQTT_LABEL:
            text = str(data.get(key) or "").strip()
            clean[key] = text or MQTT_DEFAULTS.get(key, "")
        clean["DEVICE_SERIAL"] = clean["DEVICE_SERIAL"].upper()

        # ---- 1) 先保存，保证"点一下就有记录" ----
        try:
            self.updata_data(clean)
            info["saved"] = True
            logout("MQTT 配置已写入 %s（IP=%s 序列号=%s）"
                   % (json_file, clean["mqtt_server"], clean["DEVICE_SERIAL"]))
        except Exception as e:
            info["info"] = "配置写入失败: %s" % e
            logout("MQTT 配置写入失败: %s" % e, is_error=True)
            self.send_response(client, ujson.dumps(info), status_code=500, is_json=True)
            return False

        # ---- 2) 再尝试连接 ----
        self.mqtt_update_info(mqtt_server=clean["mqtt_server"],
                              DEVICE_SERIAL=clean["DEVICE_SERIAL"],
                              password=clean["mqtt_password"],
                              username=clean["username"],
                              client_id=clean["client_id"],
                              mqtt_port=clean["mqtt_port"])

        # 先把旧连接拆掉。这里**故意不用 check_mqtt_connection(force=True) 去探测**：
        # 旧连接如果是坏的，SSL 上的 ping 会阻塞很久，把整个事件循环按住。
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                pass
            self.client = None
            self._mqtt_alive = False

        if self.conent_and_subscribe():
            info["connected"] = True
            info["info"] = "配置已保存，MQTT 连接成功"
            logout("MQTT 连接成功: %s" % clean["mqtt_server"])
        else:
            if self.wlan_sta.isconnected():
                info["info"] = "配置已保存；MQTT 暂时连不上，请核对打印机 IP / 序列号 / 访问码"
            else:
                info["info"] = "配置已保存；设备当前没联网，联网后会自动连接打印机"
            logout("MQTT 暂未连接（%s）" % info["info"])

        self.send_response(client, ujson.dumps(info), is_json=True)
        return True

    # ======================================================================
    # 状态灯
    # ======================================================================
    async def status_lED(self):
        if self.LED is None:
            return                       # 没配状态灯，这个任务直接结束
        while True:
            if self.check_mqtt_connection():
                self.LED.duty(1000)          # 常亮：MQTT 已连上
                await asyncio.sleep_ms(1000)
            elif self.wlan_sta.isconnected():
                self.LED.duty(1000)          # 闪烁：只连上 WiFi
                await asyncio.sleep_ms(500)
                self.LED.duty(0)
                await asyncio.sleep_ms(500)
            else:
                self.LED.duty(0)             # 熄灭：没有网络
                await asyncio.sleep_ms(1000)

    # ======================================================================
    # 读请求头（非阻塞 + 让步，避免被浏览器的空闲连接拖死）
    # ======================================================================
    async def _read_request(self, client):
        """读一整个 HTTP 请求（请求头 + 请求体）。

        ★ 旧实现只读到 `\\r\\n\\r\\n` 就收手，而 POST 的 JSON 请求体往往在
          **下一个** TCP 段里。于是 process_json 找不到 `{`，返回 None，
          路由条件 `data is not None` 不成立 → 掉进 404 —— 网页上就是
          "保存提示 404，而且什么都没存进去"。
          现在按 Content-Length 把请求体也读完。

        整个过程都是非阻塞读 + await 让步：浏览器会开一个"预连接"套接字却
        什么都不发，阻塞等它会把整个事件循环按住（"网页偶尔打不开"的元凶）。
        """
        client.setblocking(False)
        deadline = time.ticks_add(time.ticks_ms(), HEADER_WAIT_MS)
        buf = b""
        head_end = -1
        body_len = 0
        body_deadline = None

        while True:
            if head_end < 0:
                pos = buf.find(b"\r\n\r\n")
                if pos >= 0:
                    head_end = pos + 4
                    body_len = _content_length(buf[:head_end])
                    if body_len > 0:
                        # 请求体已经属于"下一个阶段"，给它一份独立的超时预算
                        body_deadline = time.ticks_add(time.ticks_ms(), BODY_WAIT_MS)
                elif time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                    break                       # 请求头都没收全，放弃
            else:
                if len(buf) >= head_end + body_len:
                    break                       # ★ 请求体读齐了，收工
                if body_deadline is not None and \
                        time.ticks_diff(body_deadline, time.ticks_ms()) <= 0:
                    break
            if len(buf) > MAX_REQUEST_BYTES:
                break

            try:
                chunk = client.recv(512)
            except OSError:
                chunk = None                    # 没数据可读，正常
            if chunk is None:
                await asyncio.sleep_ms(10)      # ★ 让出 CPU，别把循环堵住
                continue
            if not chunk:
                break                           # 对端已关闭
            buf += chunk

        client.setblocking(True)
        return buf if buf else None

    # ======================================================================
    # Web 主循环
    # ======================================================================
    async def run_web_loop(self, port=WEB_PORT):
        addr = socket.getaddrinfo("0.0.0.0", port)[0][-1]
        self.server_socket = socket.socket()
        # 快速重启时避免 "Address in use"
        try:
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        self.server_socket.bind(addr)
        self.server_socket.listen(2)
        self.server_socket.setblocking(False)
        logout("Web 服务已启动，监听 %s:%d" % (addr[0], port))
        logout("连上同一网络后用浏览器访问 http://%s 或 http://192.168.4.1" % addr[0])

        while True:
            # ★ 旧值是 500ms，每个请求平均要多等 250ms；改 20ms，
            #   既不会空转烧 CPU，又保证网页几乎"秒开"。
            await asyncio.sleep_ms(WEB_POLL_MS)

            try:
                client, caddr = self.server_socket.accept()
            except OSError:
                continue                  # 没有待处理连接，正常
            except Exception as e:
                logout("accept 异常: " + str(e), is_error=True)
                continue

            try:
                request = await self._read_request(client)
                if request is None or b"HTTP" not in request:
                    continue              # 空连接 / 非法请求，直接丢掉

                client.settimeout(SEND_TIMEOUT_S)

                url = "404"
                match = ure.search("(?:GET|POST|OPTIONS) /(.*?)(?:\\?.*?)? HTTP", request)
                if match:
                    try:
                        url = match.group(1).decode("utf-8").rstrip("/")
                    except Exception:
                        url = match.group(1).rstrip("/")

                data = self.process_json(request)
                if url != "status" and url != "":
                    logout("请求: %s" % url)

                if url == "":
                    await self.hanld_rootv2(client)

                # ---- 聚合状态 / 日志 / 强制扫描 ----
                elif url == "status":
                    await self.get_status(client)
                elif url == "log":
                    await self.get_log(client)
                elif url == "wifi_scan":
                    await self.handle_wifi_scan(client)
                elif url == "boot_clear":
                    await self.handle_boot_clear(client)
                elif url == "ap_set":
                    self.handle_ap_set(client, data)

                # ---- 写操作 ----
                # ★ 统一的空请求体处理：旧代码是 `elif url == "xxx" and data is not None`，
                #   请求体没读到时条件不成立就落到 handle_not_found 回了 404，
                #   用户看到"保存提示404"却完全不知道是请求体的问题。
                elif url in WRITE_ROUTES:
                    if data is None:
                        self.handle_bad_body(client, url)
                    elif url == "wifi_connect":
                        self.handle_wifi_cennect(client, data)
                    elif url == "mqtt_connect":
                        self.handle_mqtt_cennect(client, data)
                    elif url == "access_set":
                        self.handle_access_cenect(client, data)
                    else:
                        self.handle_hardware_test(client, data)

                # ---- 兼容老接口 ----
                elif url == "get_wifi_info":
                    await self.get_wifi_info(client)
                elif url == "get_mqtt_info":
                    await self.get_mqtt_info(client)
                elif url == "get_access_info":
                    await self.get_access_info(client)
                elif url == "get_hardware_info":
                    await self.get_hardware_info(client)
                elif url == "get_ip_info":
                    await self.get_ip_info(client)

                elif url == "favicon.ico":
                    self.send_response(client, b"", status_code=204)
                else:
                    self.handle_not_found(client, url)

            except MemoryError as e:
                # ★ 内存不足：先记录现场，再回收碎片，最后尽量给浏览器一句人话，
                #   并把"还剩多少内存"打进日志 —— 下次再 OOM 才有据可查。
                before = mem_note()
                gc.collect()
                logout("处理请求出错: 内存不足（%s）；%s，回收后 %s"
                       % (e, before, mem_note()), is_error=True)
                self.oom_respond(client)
            except Exception as e:
                logout("处理请求出错: " + str(e) + "（" + mem_note() + "）", is_error=True)
            finally:
                try:
                    client.close()
                except Exception:
                    pass


# ==========================================================================
# 启动
# ==========================================================================
async def main_task():
    task = []
    AMS_WEB_MODEL = AMS_WEB()

    # ---- 联网：直连已保存的 WiFi，最多 3 次；失败就开配置热点 ----
    wlan = AMS_WEB_MODEL.auto_connection()
    if not wlan:
        logout("自动联网失败，打开配置热点 AMS_WIFI")
        AMS_WEB_MODEL.swcith_ap(1)
    else:
        logout("已联网，IP = %s" % AMS_WEB_MODEL.sta_ip())

    AMS_WEB_MODEL.auto_update_access(AMS_WEB_MODEL.config_file)   # 历史通道 / 当前料盘
    AMS_WEB_MODEL.auto_conent_MQTT(AMS_WEB_MODEL.config_file)

    task.append(asyncio.create_task(AMS_WEB_MODEL.status_lED()))
    task.append(asyncio.create_task(AMS_WEB_MODEL.run_web_loop()))
    task.append(asyncio.create_task(AMS_WEB_MODEL.run_ams_loop()))
    await asyncio.gather(*task)


if __name__ == "__main__":
    asyncio.run(main_task())
