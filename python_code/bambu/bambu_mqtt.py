from umqtt.simple import MQTTClient
from machine import Pin
import network
import socket
import time
import ujson
from bambu.bambu_commands import banbu_start,START_PUSH
import uasyncio as asyncio
from network_model import network_model
from logout import logout
from bambu.get_event_info import get_is_change_ams,get_status,get_hms_code,get_HMS_info,get_print_error,get_nozzle_temper
from info_load import read_json_file

# ---------------------------------------------------------------------------
# MQTT 存活探测节流间隔
# ---------------------------------------------------------------------------
# keepalive 是 60 秒，本来每 5 秒探一次就足够。
# 旧代码每次调用 check_mqtt_connection() 都真的发一个 PINGREQ，
# 而状态灯任务每秒调一次、网页接口也在调 —— 既浪费带宽，也在 SSL 上产生
# 阻塞写。现在按时间节流，两次探测之间直接返回上次的结果。
MQTT_PING_INTERVAL_MS = 5000

# ★ ping 的 socket 超时（秒）。
#   SSL 上的 ping() 是阻塞写；如果打印机那边连接已经半死（拔电、换网、
#   打印机休眠），这个写会一直阻塞到 TCP/TLS 自己超时 —— **几十秒**。
#   而 /status 每 2 秒被轮询一次，于是整个网页会周期性地"死"几十秒。
#   给个 0.8 秒的硬上限，超时就算作"不存活"，下一轮重连。
MQTT_PING_TIMEOUT_S = 0.8

# ★ MQTT 连接前的 TCP 预探测超时（秒）。
#   打印机没开机 / 不在同一网段时，SSL 连接会一直卡到系统超时。
#   先用一个普通 socket 花最多 1 秒确认"这个 IP:端口能不能握手"，
#   探不通就直接跳过这次重连 —— 把"每 10 秒冻一次、每次好几秒"变成
#   "每 10 秒探 1 秒就放弃"。
MQTT_PREFLIGHT_TIMEOUT_S = 1.0


class Bambu_mqtt_cliet(network_model):
    def __init__(self,mqtt_server,DEVICE_SERIAL,password,username="bblp",client_id="mqttx_3c73cd31",mqtt_port=8883):
        super().__init__()
        self.mqtt_server = mqtt_server  # 服务器地址
        self.DEVICE_SERIAL = DEVICE_SERIAL # 服务器序列好
        self.username = username # 用户
        self.password = password # 服务器密码
        self.client_id = client_id
        self.mqtt_port = mqtt_port
        # 订阅和发送的主题
        self.TOPIC_SUBSCRIBE = "device/" + self.DEVICE_SERIAL + "/report"
        self.TOPIC_PUBLISH = "device/" + self.DEVICE_SERIAL + "/request"
        self.new_message = '{}' # 订阅的消息
        self.client = None
        # 存活探测缓存
        self._mqtt_alive = False
        self._mqtt_checked_at = None
    def mqtt_update_info(self,mqtt_server,DEVICE_SERIAL,password,username="bblp",client_id="mqttx_3c73cd31",mqtt_port=8883):
        self.mqtt_server = mqtt_server  # 服务器地址
        self.DEVICE_SERIAL = DEVICE_SERIAL # 服务器序列好
        self.username = username # 用户
        self.password = password # 服务器密码
        self.client_id = client_id
        self.mqtt_port = mqtt_port
        # 订阅和发送的主题
        self.TOPIC_SUBSCRIBE = "device/" + self.DEVICE_SERIAL + "/report"
        self.TOPIC_PUBLISH = "device/" + self.DEVICE_SERIAL + "/request"
        
    def sub_cb(self,topic,msg):
        #del self.new_message
        self.new_message = msg.decode()
        
    def auto_conent_MQTT(self,file_path):
        """开机自动连打印机：从 config.json 读配置。

        ★ 一律用 .get() 带默认值。老版本的配置模板把访问码存在 "password" 这个
          键上，而网页一直写的是 "mqtt_password"；直接下标取会 KeyError，
          被整个 except 吞掉 → 表现就是"配置明明保存了，却永远不自动连接"。
        """
        try:
            data = read_json_file(file_path) or {}
        except Exception as e:
            logout("读取打印机配置失败: " + str(e), is_error=True)
            return False

        server = str(data.get("mqtt_server") or "").strip()
        serial = str(data.get("DEVICE_SERIAL") or "").strip()
        password = str(data.get("mqtt_password") or data.get("password") or "").strip()
        if not (server and serial and password):
            logout("打印机 MQTT 尚未配置完整（IP / 序列号 / 访问码），跳过自动连接")
            return False

        self.mqtt_update_info(mqtt_server=server,
                              DEVICE_SERIAL=serial,
                              password=password,
                              username=str(data.get("username") or "bblp").strip() or "bblp",
                              client_id=str(data.get("client_id") or "mqttx_3c73cd31").strip()
                                        or "mqttx_3c73cd31",
                              mqtt_port=str(data.get("mqtt_port") or "8883").strip() or "8883")
        return self.conent_and_subscribe()
    def update_print_info(self):
        # 处理信息
        data = ujson.loads(self.new_message).get("print",{}) # 加载数据
        change_info = get_is_change_ams(data)   # 获取换料信息
        status_info = get_status(data)          # 获取打印机运行状态
        HMS_info = get_HMS_info(data)           # 获取错误信息
        print_error_info=get_print_error(data)  # 获取打印错误码
        
        return {"change_info":change_info,"status_info":status_info,"HMS_info":HMS_info,"print_error_info":print_error_info}
    
    # 连接和订阅
    def close_client(self):
        """把当前 MQTT 连接彻底放掉（断开 + 置空）。

        ★ 旧代码每次重连都是 `self.client = MQTTClient(...)` 直接覆盖，
          老对象连同它的 SSL socket **从来没被关闭过** —— 每重连一次就漏一个
          socket。板子跑几个小时之后 socket 用光，表现就是"什么都连不上、
          网页也像死了一样"。所以每次重连前必须先放掉旧的。
        """
        old = self.client
        self.client = None
        self._mqtt_alive = False
        if old is None:
            return False
        try:
            old.disconnect()
        except Exception:
            pass
        # 有些版本的 umqtt 只把 socket 挂在 sock 上，disconnect 失败时手动兜底
        try:
            sock = getattr(old, "sock", None)
            if sock is not None:
                sock.close()
        except Exception:
            pass
        return True

    def tcp_reachable(self, timeout_s=None):
        """快探一下打印机 IP:端口能不能握手（最多 ~1 秒）。

        ★ 目的只有一个：**别让"打印机没开机"这件事把整个网页冻住**。
          SSL 连接到不可达的地址会一直卡到系统超时（好几秒甚至几十秒），
          而主循环每 10 秒就试一次 —— 网页就变成周期性假死。
          先用普通 socket 花 1 秒探路，探不通就直接放弃这一轮。

        探测失败不代表配置写错了，所以只返回布尔值，不抛异常。
        """
        timeout_s = timeout_s or MQTT_PREFLIGHT_TIMEOUT_S
        host = str(self.mqtt_server or "").strip()
        if not host:
            return False
        try:
            port = int(str(self.mqtt_port or "8883"))
        except Exception:
            port = 8883
        sock = None
        try:
            addr = socket.getaddrinfo(host, port)[0][-1]
        except Exception:
            return False
        try:
            sock = socket.socket()
            sock.settimeout(timeout_s)
            sock.connect(addr)
            return True
        except Exception:
            return False
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    def conent_and_subscribe(self, preflight=False):
        try:
            if self.wlan_sta.isconnected():
                if preflight and not self.tcp_reachable():
                    logout("打印机 %s 暂时连不上（TCP 预探测未通过），本轮跳过"
                           % self.mqtt_server)
                    self._mqtt_alive = False
                    return False
                logout("正在连接mqtt...")
                # ★ 先把旧连接放掉，避免每重连一次漏一个 socket
                self.close_client()
                self.client = MQTTClient(self.client_id, self.mqtt_server, user=self.username, password=self.password, ssl=True, keepalive=60)
                #self.client.disconnect()
                self.client.set_callback(self.sub_cb)
                self.client.connect()
                self.client.subscribe(self.TOPIC_SUBSCRIBE)
                logout("成功连接到 %s, 订阅地址为 %s topic" % (self.mqtt_server, self.TOPIC_SUBSCRIBE))
                self._mqtt_alive = True
                self._mqtt_checked_at = time.ticks_ms()
                return True
            else:
                logout("未连接 WiFi，暂不连打印机")
                self._mqtt_alive = False
                return False
        except Exception  as e:
            logout("连接MQTT失败"+str(e),is_error = True)
            self._mqtt_alive = False
            return False
    
    # 连接和订阅
    async def conent_and_subscribe_asyncio(self):
        try:
            logout("(异步)正在连接mqtt...")
            self.client = MQTTClient(self.client_id, self.mqtt_server, user=self.username, password=self.password, ssl=True, keepalive=30)
            #self.client.disconnect()
            self.client.set_callback(self.sub_cb)
            while not self.client.connect():
                await asyncio.sleep(1)
                print("...",end="")
            self.client.subscribe(self.TOPIC_SUBSCRIBE)
            logout("成功连接到 %s, 订阅地址为 %s topic" % (self.mqtt_server, self.TOPIC_SUBSCRIBE))
            return True
        except Exception  as e:
            logout("连接MQTT失败"+str(e),is_error = True)
            return False
        
        
    def mqtt_alive_cached(self):
        """★ 只读缓存，**一次网络 I/O 都不做**。

        这是"网页一卡几十秒"的正面修法：旧代码让 /status 直接调
        check_mqtt_connection()，而它每隔几秒就真的在 SSL 上 ping 一次；
        连接一旦半死，这个阻塞写会把整个事件循环按住，直到 TCP 自己超时。
        网页每 2 秒轮询一次 /status，于是周期性假死。

        真实探测交给主循环（run_ams_loop）去做，网页只读它留下的结论。
        """
        if self.client is None:
            return False
        return bool(self._mqtt_alive)

    def _ping_guarded(self):
        """带硬超时的 ping。

        SSL socket 上的 ping() 是阻塞写。给 socket 设一个 0.8 秒的 recv/send
        超时，超了就当"不存活"，绝不会让调用方卡住几十秒。
        """
        sock = getattr(self.client, "sock", None)
        restored = False
        if sock is not None:
            try:
                sock.settimeout(MQTT_PING_TIMEOUT_S)
                restored = True
            except Exception:
                restored = False
        try:
            self.client.ping()
            return True
        except Exception:
            return False
        finally:
            # 后面 publish 需要阻塞写，所以把超时还原成"不限制"
            if restored:
                try:
                    sock.settimeout(None)
                except Exception:
                    pass

    def check_mqtt_connection(self, force=False):
        """MQTT 是否还活着（会真的发一次 PINGREQ）。

        ★ 两层保护，缺一不可：
          1. 按时间节流：默认 MQTT_PING_INTERVAL_MS 内只真的探测一次。
             旧实现每次调用都真的发 PINGREQ，而状态灯任务每秒调它一次，
             网页也在调 —— 白白拖慢网页。
          2. ping 有 0.8 秒硬超时（_ping_guarded）。没有它的话，连接半死时
             这一个调用就能把整个事件循环按住几十秒。

        ⚠️ 网页接口**不要**调它 —— 请用 mqtt_alive_cached()。
           只有主循环和状态灯才需要真正探测。
        force=True 时跳过节流，用于"刚改完配置要立刻确认结果"的场景。
        """
        if not self.client:
            self._mqtt_alive = False
            return False

        now = time.ticks_ms()
        if (not force) and self._mqtt_checked_at is not None \
                and time.ticks_diff(now, self._mqtt_checked_at) < MQTT_PING_INTERVAL_MS:
            return self._mqtt_alive

        self._mqtt_alive = self._ping_guarded()
        self._mqtt_checked_at = now
        return self._mqtt_alive

    def poll_msg(self):
        """非阻塞地收一条消息；没有消息就立刻返回 None。

        ★ 这是本版修"网页卡死"的关键：
          旧代码在主循环里调 wait_msg()，它是阻塞读 —— 没有消息时会把整个
          uasyncio 事件循环按住不放，Web 任务、状态灯任务全都饿死。
          check_msg() 会先把 socket 设成非阻塞，没数据就返回 None（并且
          内部会把 socket 恢复成阻塞模式，所以后续 publish 仍然安全）。
        """
        if not self.client:
            return None
        try:
            return self.client.check_msg()
        except OSError:
            self._mqtt_alive = False
            return None
        except Exception:
            return None

    def wait_msg_timeout(self, timeout_ms=2000):
        """非阻塞地等一条消息，最多等 timeout_ms 毫秒。

        换料流程里需要"发完命令等一下打印机的回应"，但绝不能用无超时的
        wait_msg()——打印机掉线时会把整个程序挂死。这里给个上限。
        返回 True 表示期间收到了消息。
        """
        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            if self.poll_msg() is not None:
                return True
            time.sleep_ms(20)
        return False


    async def loop_updata(self):
        logout("mqtt信息更新启动")
        while True:
            if self.check_mqtt_connection():
                try:
                    await asyncio.sleep_ms(100)
                    self.piblish(banbu_start)
                    self.client.check_msg()
                except Exception as e:
                    self.new_message = {}
                    await asyncio.sleep(10)
                    logout("发布出错了",e)
            else:
                await asyncio.sleep(3)
        logout("结束")
        
    def piblish(self,operation_code):
        self.client.publish(self.TOPIC_PUBLISH, operation_code,)
        
    def piblish_gcode(self,g_code):
        operation_code = '{"print": {"sequence_id": "0", "command": "gcode_line", "param": "'+g_code+'"}}'
        self.client.publish(self.TOPIC_PUBLISH, operation_code)

if __name__ == "__main__":
    client = Bambu_mqtt_cliet("192.168.0.103","0309AA411201130","21433590")
    #client.do_connect("Mr","15816728266")
    client.conent_and_subscribe()
    