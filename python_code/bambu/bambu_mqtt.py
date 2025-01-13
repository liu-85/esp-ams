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
        try:
            data = read_json_file(file_path)
            self.mqtt_update_info(mqtt_server=data["mqtt_server"],DEVICE_SERIAL=data["DEVICE_SERIAL"],password=data["mqtt_password"],username=data["username"],client_id=data["client_id"],mqtt_port=data["mqtt_port"])
            return self.conent_and_subscribe()
        except Exception as e:
            logout("error:"+str(e),is_error = True)
            return False
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
                return True
            else:
                print("未连接wifi")
                return False
        except Exception  as e:
            logout("连接MQTT失败"+str(e),is_error = True)
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
        
        
    def check_mqtt_connection(self):
        try:
            if self.client:
                self.client.ping()  # 通过发送一个PING消息来检查连接状态
                return True
            else:
                return False
        except OSError:
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
    