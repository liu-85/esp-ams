from umqtt.simple import MQTTClient
from machine import Pin
import network
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
    def conent_and_subscribe(self):
        try:
            if self.wlan_sta.isconnected():
                logout("正在连接mqtt...")
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
                print("未连接wifi")
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
        
        
    def check_mqtt_connection(self, force=False):
        """MQTT 是否还活着。

        ★ 这里做了节流：默认 MQTT_PING_INTERVAL_MS 内只真的探测一次。
          旧实现每次调用都真的发 PINGREQ，而状态灯任务每秒调它一次，
          网页 /get_mqtt_info 也在调 —— SSL 上的写是阻塞的，白白拖慢网页。

        force=True 时跳过节流，用于"刚改完配置要立刻确认结果"的场景。
        """
        if not self.client:
            self._mqtt_alive = False
            return False

        now = time.ticks_ms()
        if (not force) and self._mqtt_checked_at is not None \
                and time.ticks_diff(now, self._mqtt_checked_at) < MQTT_PING_INTERVAL_MS:
            return self._mqtt_alive

        try:
            self.client.ping()
            self._mqtt_alive = True
        except Exception:
            self._mqtt_alive = False
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
    