import socket
from logout import logout
import network
import socket
import ure
import time
import ujson
import gc
from AMS_MODEL import AMS
from machine import Pin,PWM
import uasyncio as asyncio
from info_load import read_profiles,write_profiles,read_json_file,write_json_file
# 启用垃圾收集器
gc.enable()
# 设置触发垃圾收集的内存阈值为1字节
# 这意味着每次内存分配后都会立即进行垃圾收集
gc.threshold(1024)
json_file = "config.json"

class AMS_WEB(AMS):
    def __init__(self):
        super().__init__()
        self.server_socket = None
        self.LED = PWM(Pin(2))
        self.LED.freq(1000)
        self.LED.duty(0)
    
    def updata_data(self,updata_data):
        # 更新json中的数据
        json_data=read_json_file(json_file)
        json_data.update(updata_data)
        write_json_file(json_file,json_data)
        return json_data   
    # 发送请求头数据    
    def send_header(self,client,status_code=200, content_length=None,is_json=False):
        client.sendall("HTTP/1.0 {} OK\r\n".format(status_code))
        client.sendall("Access-Control-Allow-Origin: * \r\n")
        if is_json:
            client.sendall("Content-Type: application/json; charset=UTF-8\r\n")
        else:
            client.sendall("Content-Type: text/html; charset=UTF-8\r\n")
        
        if content_length is not None:
          client.sendall("Content-Length: {}\r\n".format(content_length))
        client.sendall("\r\n")
        
    
    # 发送相应数据    
    def send_response(self,client, payload, status_code=200,is_json=False):
        content_length = len(payload.encode())
        self.send_header(client, status_code, content_length,is_json=is_json)
        if content_length > 0:
            client.sendall(payload)
        client.close()
        
        
    # 处理jsons数据
    def process_json(self,json_data):
        # 找到 JSON 数据的起始位置
        json_start = json_data.find(b'{')
        if json_start != -1:
            # 从起始位置开始提取 JSON 数据
            json_str = json_data[json_start:]
            try:
                # 尝试解析 JSON 数据
                json_obj = ujson.loads(json_str)
                return json_obj
            except ValueError as e:
                print("Error parsing JSON: ", e)
        return None
    # 404处理
    def handle_not_found(self,client, url):
        self.send_response(client, "Path not found: {}".format(url), status_code=404)
    # 获取wifi信息
    async def get_wifi_info(self,client):
        self.wlan_sta.active(True)
        networks = self.wlan_sta.scan() 
        ssids = sorted(ssid.decode('utf-8') for ssid, *_ in networks if ssid)
        data = {
            "wifi_isconnected":self.wlan_sta.isconnected(),
            "ssids":list(ssids),
            }
        self.send_response(client,ujson.dumps(data),is_json=True)
    # 获取mqtt信息
    async def get_mqtt_info(self,client):
        #mqtt_info = read_json_file(json_file)
        mqtt_info = {
            "DEVICE_SERIAL": self.DEVICE_SERIAL,
            "mqtt_password": self.password,
            "mqtt_port": self.mqtt_port,
            "mqtt_server": self.mqtt_server,
            "client_id": self.client_id,
            "username": self.username,
            }
        data = {"is_mqtt_con":self.check_mqtt_connection()}
        data.update(mqtt_info)
        self.send_response(client,ujson.dumps(data),is_json=True)
    # 获取通道信息
    async def get_access_info(self,client):
        data = {
            "color_list":self.color_list,
            "access_list":self.access_list,
            "current_access":self.filament_current
            }
        self.send_response(client,ujson.dumps(data),is_json=True)
    
    async def hanld_rootv2(self,client):
        with open("index.html","r") as f:
            self.send_header(client)
            for line in f:
                client.sendall(line)
                await asyncio.sleep_ms(10)
    async def get_ip_info(self,client):
        data = {"ip":""}
        if self.wlan_sta:
            data["ip"] = self.wlan_sta.ifconfig()[0]
        self.send_response(client,ujson.dumps(data),is_json=True)
             
  
    # 连接wifi
    def handle_wifi_cennect(self,client,data):
        logout("连接WIFI")
        dict_info = {"info":None}
        if data is None:
            dict_info["info"] = "Parameters not found"
            self.send_response(client,ujson.dumps(dict_info),status_code=400,is_json=True)
            return False
        elif not "name" in data :
            dict_info["info"] = "SSID and password must be provided"
            self.send_response(client,ujson.dumps(dict_info),status_code=400,is_json=True)
            return False
        ssid = data["name"]
        password = data["password"]
        if self.do_connect(ssid, password):
            dict_info["info"] = "{},局域网地址为:{}".format(ssid,self.wlan_sta.ifconfig()[0])
            dict_info["ip"] = self.wlan_sta.ifconfig()[0]
            self.send_response(client,ujson.dumps(dict_info),is_json=True)
            try:
                profiles = read_profiles()
            except OSError:
                profiles = {}
            profiles[ssid] = password
            write_profiles(profiles)
            #await asyncio.sleep_ms(1000)
            time.sleep(1)
            return True
        else:
            dict_info["info"] = "连接失败，请重新设置密码或稍后从事"
            self.send_response(client,ujson.dumps(dict_info),status_code=400,is_json=True)
            return False
    # 改变映射通道
    def handle_access_cenect(self,client,data):
        logout("改变映射通道,及其颜色")
        dict_info = {"info":None}
        new_data = data["access_list"]
        if sum(set(new_data))!=sum(self.access_list) or len(set(new_data))!=len(new_data):
            dict_info["info"] = "请确定料盘编号"
            self.send_response(client,ujson.dumps(dict_info),status_code=400,is_json=True)
            return False
        for value in new_data:
            if value not in self.access_list:
                dict_info["info"] = "请确定料盘编号"
                self.send_response(client,ujson.dumps(dict_info),status_code=400,is_json=True)
                return False
        self.color_list = data["color_list"]
        if self.filament_current >0:
            index = self.access_list.index(self.filament_current)
            self.filament_current = new_data[index]
            
            
        self.access_list = new_data
        self.updata_data({"access":new_data,"color_list":self.color_list})
        self.dianji_dict = {key:value for key,value in zip(self.access_list,self.meterial_list)}
        dict_info["info"] = "切换通道成功"
        self.send_response(client,ujson.dumps(dict_info),is_json=True) 

        
    def handle_mqtt_cennect(self,client,data):
        logout("连接MQTT")
        dict_info = {"info":None}
        for key in data:
            if len(data[key]) == 0:
                dict_info["info"] = key+"参数不能为空"
                self.send_response(client,ujson.dumps(dict_info),status_code=400,is_json=True)
                return False
        if self.check_mqtt_connection():
            self.client.disconnect()
        self.mqtt_update_info(mqtt_server=data["mqtt_server"],DEVICE_SERIAL=data["DEVICE_SERIAL"],password=data["mqtt_password"],username=data["username"],client_id=data["client_id"],mqtt_port=data["mqtt_port"])
        
        if self.conent_and_subscribe():
            dict_info["info"] = "MQTT连接成功"
            self.send_response(client,ujson.dumps(dict_info),is_json=True)
            json_data=read_json_file(json_file)
            json_data.update(data)
            write_json_file(json_file,data)
            return True
        else:
            dict_info["info"] = "MQTT连接失败"
            self.send_response(client,ujson.dumps(dict_info),status_code=400,is_json=True)
            return False
    async def status_lED(self):
        while True:
            if self.check_mqtt_connection():
                #常亮
                self.LED.duty(1000)
                await asyncio.sleep_ms(1000)
            elif self.wlan_sta.isconnected():
                # 闪烁
                self.LED.duty(1000)
                await asyncio.sleep_ms(500)
                self.LED.duty(0)
                await asyncio.sleep_ms(500)
            else:
                #熄灭
                self.LED.duty(0)
                await asyncio.sleep_ms(1000)
        
    async def run_web_loop(self,port=80):
        domain = "0.0.0.0"
        addr = socket.getaddrinfo(domain, port)[0][-1] # 配置端口地址
        self.server_socket = socket.socket()
        self.server_socket.bind(addr)
        self.server_socket.listen(1)
        self.server_socket.setblocking(False) # 设置非堵塞
        logout('使用浏览器输入192.168.4.1访问网页')
        logout('开始监听:', addr[0])
        while True:
            await asyncio.sleep_ms(500)
            try:
                client, addr = self.server_socket.accept()
                # 处理连接
            except OSError as e:
                # 如果没有连接请求，会抛出OSError，可以在这里处理其他事务
                continue
            
            logout('客户端连接自{}'.format(addr[0]))
            try:
                client.settimeout(3.0)
                request = b""

                try:
                    while "\r\n\r\n" not in request:
                        request += client.recv(1024)
                except OSError:
                    pass
                #print("Request is: {}".format(request))
                if "HTTP" not in request:  # skip invalid requests
                    continue
                url = "404"
                if ure.search("(?:GET|POST|OPTIONS) /(.*?)(?:\\?.*?)? HTTP", request):
                    try:
                        url = ure.search("(?:GET|POST|OPTIONS) /(.*?)(?:\\?.*?)? HTTP", request).group(1).decode("utf-8").rstrip("/")
                    except Exception:
                        url = ure.search("(?:GET|POST|OPTIONS) /(.*?)(?:\\?.*?)? HTTP", request).group(1).rstrip("/")
                logout("请求地址:{}".format(url))
                data = self.process_json(request)   # 处理请求数据
                if url == "": # 首页
                    await self.hanld_rootv2(client)
                elif url == "wifi_connect" and data is not None: # 连接wifi
                    self.handle_wifi_cennect(client, data)
                elif url == "mqtt_connect" and data is not None:  # 连接MQTT
                    self.handle_mqtt_cennect(client, data)
                elif url == "access_set" and data is not None:    # 改变通道
                    self.handle_access_cenect(client, data)
                elif url == "get_wifi_info":
                    await self.get_wifi_info(client)
                elif url == "get_mqtt_info":
                    await self.get_mqtt_info(client)
                elif url == "get_access_info":
                    await self.get_access_info(client)
                elif url == "get_ip_info":
                    await self.get_ip_info(client)
                else:
                    self.handle_not_found(client, url)            # 404
            except Exception as e:
                logout("error:"+str(e),is_error = True)
                client.close()
            finally:
                client.close()

async def main_task():
    task = []
    AMS_WEB_MODEL = AMS_WEB()
    #AMS_WEB_MODEL.swcith_ap(1)      # 打开热点
    AMS_WEB_MODEL.auto_connection() # 自动连接wifi
    AMS_WEB_MODEL.auto_update_access(json_file)  # 历史通道
    AMS_WEB_MODEL.auto_conent_MQTT(json_file)
    task.append(asyncio.create_task(AMS_WEB_MODEL.status_lED()))
    task.append(asyncio.create_task(AMS_WEB_MODEL.run_web_loop()))
    task.append(asyncio.create_task(AMS_WEB_MODEL.run_ams_loop()))
    await asyncio.gather(*task)
    
if __name__ == "__main__":

    asyncio.run(main_task())       
    
 