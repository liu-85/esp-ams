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
    
    def updata_data(self,updata_data):
        # 更新json中的数据
        json_data=read_json_file(json_file)
        json_data.update(updata_data)
        write_json_file(json_file,json_data)
        return json_data   
    # 发送请求头数据    
    def send_header(self,client,status_code=200, content_length=None,is_json=False):
        client.sendall("HTTP/1.0 {} OK\r\n".format(status_code))
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
    # 首页处理
    async def hanld_root(self,client):
        logout("访问首页")
        self.wlan_sta.active(True)
        networks = self.wlan_sta.scan() 
        ssids = sorted(ssid.decode('utf-8') for ssid, *_ in networks if ssid)
        self.send_header(client)
        wifi_status_text = "未连接"
        wifi_status =""
        if self.wlan_sta.isconnected():
            wifi_status_text = "以连接"+self.wlan_sta.config('essid')+",局域网地址为:"+self.wlan_sta.ifconfig()[0]
            wifi_status = "status_true"
        await asyncio.sleep_ms(100)
        client.sendall("""\
<!DOCTYPE html>
<html lang="cn">
<head>
    <meta charset="UTF-8">
    <title>AMS_MQTT配置</title>
    <style>
        *{
         margin: 0;
            padding: 0;
        }
        .overlay {
          position: fixed;
          top: 0;
          left: 0;
          width: 100%;
          height: 100%;
          background-color: rgba(0, 0, 0, 0.5); /* 半透明灰色背景 */
          z-index: 9998; /* 设置一个较高的z-index值，比loader小1 */
        }""")
        await asyncio.sleep_ms(100)
        client.sendall("""\
        
        .loader {
          border: 16px solid #f3f3f3; /* Light grey */
          border-top: 16px solid #3498db; /* Blue */
          border-radius: 50%;
          width: 120px;
          height: 120px;
          animation: spin 2s linear infinite;
          position: fixed;
          top: 48%;
          left: 48%;
          transform: translate(-50%, -50%);
          z-index: 9999; /* 设置一个较高的z-index值 */
          text-align: center; /* 文字居中 */
        } 
        .loader-text {
          color: #fff; /* 白色文字 */
          font-size: 16px;
          margin-top: 60px; /* 调整文字位置 */
        }""")
        await asyncio.sleep_ms(100)
        client.sendall("""\
        @keyframes spin {
          0% { transform: rotate(0deg); }
          100% { transform: rotate(360deg); }
        }

        h1{
            text-align: center;
            margin: 10px 0;
        }
        ul{

            list-style: none;
        }
        button{
            background-color: #4CAF50; /* 绿色背景 */
            color: white; /* 白色文字 */
            padding: 5px 10px; /* 内边距 */
            text-align: center; /* 文字居中 */
            text-decoration: none; /* 取消下划线 */
            display: inline-block; /* 行内块元素 */
            font-size: 16px; /* 字体大小 */
            margin: 5px; /* 外边距 */
            cursor: pointer; /* 鼠标样式为手型 */
            border: none; /* 取消边框 */
            border-radius: 5px; /* 圆角 */
        }
        input{
            margin: 5px 0;
        }""")
        await asyncio.sleep_ms(100)
        client.sendall("""\

        .box{
            width: 50%;
            margin: 100px auto;
           text-align: center
        }
        .status{
            width: 20px;
            height: 20px;
            border-radius: 25px;
            display: inline-block;
            background-color: red;
        }
          .status_true{
             background-color: green;
        }
        #mqtt{
            display: none;
        }
        .warring{
            color:red;}

    </style>
</head>
<body>
<div class="overlay">
    <div class="loader">
        <div class="loader-text">请求中....</div>
    </div>
</div>
<div class="box">
    <div><p class="warring">"""+self.now_warring+"""
    <h1>WIFI配置</h1>
    <h3>
        当前wifi连接状态：<span class="status """+wifi_status+""" " id="wifi_status"></span><span id="wifi_statu_text">"""+wifi_status_text+"""</span>
    </h3>
    <ul>
        <li>请选择要连接WiFi名称</li>
            """)
        while len(ssids):
            ssid = ssids.pop(0)
            client.sendall("""\
                            <li><input type="radio" name="wifi_name" value="{0}">{0}</li>
            """.format(ssid))
        mqtt_info = read_json_file(json_file) # 读取数据
        mqtt_other = {"mqtt_status_text":"未连接","mqtt_status":""}

        if self.check_mqtt_connection():
            mqtt_other["mqtt_status_text"] = "以连接"
            mqtt_other["mqtt_status"]= "status_true"
        mqtt_info.update(mqtt_other)
        await asyncio.sleep_ms(100)
        client.sendall("""
                <li>密码<input type="password" name="wifi_password" ></li>
        <button class="myButton" onclick="connect_wifi()">确定连接wifi</button>
    </ul>
   <div id="mqtt">
        <h1>AMSmqtt配置</h1>
    <h3>当前mqtt连接状态：<span class="status {mqtt_status}" id="mqtt_status"></span><span id="mqtt_statu_text">{mqtt_status_text}</span></h3>
        打印机ip地址：<input name="mqtt_server" type="text" value="{mqtt_server}" ><br>
        打印机序列号：<input name="DEVICE_SERIAL" type="text" value="{DEVICE_SERIAL}"><br>
        打印机密码：<input name="mqtt_password" type="password" value="{mqtt_password}"><br>
        MQTT客户端名称：<input name="client_id" type="text" value="{client_id}"><br>
        MQTT服务端名称：<input name="username" type="text" value="{username}"><br>
        MQTT端口：<input name="mqtt_port" type="number" value="{mqtt_port}"><br>
        <button class="myButton" onclick="connect_mqtt()">确定修改</button>
        <p>打印机通道设置</p>
        """.format(**mqtt_info))
        await asyncio.sleep_ms(200)
        for n in range(len(self.access_list)):
            add_info = """<input type="button"  value="进料">"""
            if self.access_list == self.filament_current:
                add_info = """<input type="button"  value="退料">"""
            client.sendall("""\
                料盘{}: <input class="fileament_class" name="access{}" type="number" value="{}">{}<br>
                            """.format(n+1,n+1,self.access_list[n],add_info ))
        client.sendall("""\
        <button class="myButton" onclick="access_set()" >确定修改</button>
           </div>
        </div>
        """)
        await asyncio.sleep_ms(200)
        client.sendall("""\
        <script>
                // 获取加载动画元素和覆盖层元素
                var loader = document.querySelector('.loader');
                var overlay = document.querySelector('.overlay');
                loader.style.display = 'none';
                overlay.style.display = 'none';
                document.documentElement.style.overflow = 'auto'
                function loader_s(is_open=true){
                    if(is_open){
                        loader.style.display = 'block';
                        overlay.style.display = 'block';
                        document.documentElement.style.overflow = 'hidden';
                    }else{
                        loader.style.display = 'none';
                        overlay.style.display = 'none';
                        document.documentElement.style.overflow = 'auto';
                    }
                }""")
        await asyncio.sleep_ms(200)
        client.sendall("""\
                var wifi_status = document.getElementById('wifi_status');
                var mqtt_div = document.getElementById('mqtt');
                if(wifi_status.classList.contains('status_true')){
                    mqtt_div.style.display='block'
                }
                var observer = new MutationObserver(function(mutationsList) {
                    mutationsList.forEach(function(mutation) {
                        if (mutation.attributeName === 'class' && wifi_status.classList.contains('status_true')) {
                            mqtt_div.style.display='block'

                        }
                    });""")
        await asyncio.sleep_ms(200)
        client.sendall("""\
                });
                // 配置MutationObserver监视span元素的属性变化
                observer.observe(wifi_status, { attributes: true });

                function connect_wifi_event(data){
                    document.getElementById('wifi_status').classList.add("status_true")
                    document.getElementById('wifi_statu_text').innerText="已连接"
                    alert("wifi连接成功"+data["info"])
                }
                function connect_wifi(){
                    const jsonData = {
                        name: selectedRadioButton = document.querySelector('input[name="wifi_name"]:checked').value,
                        password: document.querySelector('input[name="wifi_password"]').value,
                    };""")
        await asyncio.sleep_ms(200)
        client.sendall("""\
                
                    send_data("/wifi_connect",jsonData,connect_wifi_event)
                }
                function connect_mqtt_event(data){
                    document.getElementById('mqtt_status').classList.add("status_true")
                    document.getElementById('mqtt_statu_text').innerText="已连接"+data["info"]
                    alert("mqtt连接成功:"+data["info"])
                }
                function connect_mqtt(){
                    const jsonData = {
                        mqtt_server: document.querySelector('input[name="mqtt_server"]').value,
                        DEVICE_SERIAL: document.querySelector('input[name="DEVICE_SERIAL"]').value,
                        mqtt_password: document.querySelector('input[name="mqtt_password"]').value,
                        client_id: document.querySelector('input[name="client_id"]').value,
                        username: document.querySelector('input[name="username"]').value,
                        mqtt_port: document.querySelector('input[name="mqtt_port"]').value,
                    };""")
        await asyncio.sleep_ms(200)
        client.sendall("""\
                    send_data("/mqtt_connect",jsonData,connect_mqtt_event)
                }
                function connect_access_set_event(data){
                    alert("通道设置成功:"+data["info"])
                }
                function access_set(){
                    const jsonData = {};
                    var filamentInputs = document.getElementsByClassName('fileament_class')
                    for (var i = 0; i < filamentInputs.length; i++) {
                                var name = filamentInputs[i].getAttribute('name');
                                var value = filamentInputs[i].value;
                                jsonData[name] = value;
                            }
                    console.log(jsonData)
                    send_data("/access_set",jsonData,connect_access_set_event)
                }""")
        await asyncio.sleep_ms(200)
        client.sendall("""\
                function send_data(url,data,event_fun){
                    loader_s()
                    const requestOptions = {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify(data)
                    };
                    fetch(url, requestOptions)
                        .then(response => {
                            if (!response.ok) {
                                return response.json().then(errorData => {

                                throw new Error(errorData.info);
                                                });""")
        await asyncio.sleep_ms(200)
        client.sendall("""\
                            }
                            return response.json();
                        })
                        .then(data => {
                            event_fun(data)
                            loader_s(false)
                        })
                        .catch(error => {
                            loader_s(false);
                            alert('出现问题'+ error)
                        });

                }

        </script>
        </body>
        </html>
        """)
        
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
            self.send_response(client,ujson.dumps(dict_info),is_json=True)
            try:
                profiles = read_profiles()
            except OSError:
                profiles = {}
            profiles[ssid] = password
            write_profiles(profiles)
            #await asyncio.sleep_ms(200)
            time.sleep(1)
            return True
        else:
            dict_info["info"] = "连接失败，请重新设置密码或稍后从事"
            self.send_response(client,ujson.dumps(dict_info),status_code=400,is_json=True)
            return False
    # 改变映射通道
    def handle_access_cenect(self,client,data):
        logout("改变映射通道")
        dict_info = {"info":None}
        print(data)
        new_data=[int(data["access"+str(n+1)]) for n in range(len(data)) ]
        
        if sum(set(new_data))!=sum(self.access_list) or len(set(new_data))!=len(new_data):
            dict_info["info"] = "请确定料盘编号"
            self.send_response(client,ujson.dumps(dict_info),status_code=400,is_json=True)
            return False
        for value in new_data:
            if value not in self.access_list:
                dict_info["info"] = "请确定料盘编号"
                self.send_response(client,ujson.dumps(dict_info),status_code=400,is_json=True)
                return False
            
        self.access_list = new_data
        self.updata_data({"access":new_data})
        self.dianji_dict = {key:value for key,value in zip(self.access_list,self.meterial_list)}
        dict_info["info"] = "切换通道成功"
        self.send_response(client,ujson.dumps(dict_info),is_json=True)
        self.filament_current = self.now_filament()
        
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
        
    async def run_web_loop(self,port=80):
        addr = socket.getaddrinfo('0.0.0.0', port)[0][-1] # 配置端口地址
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
                try:
                    url = ure.search("(?:GET|POST) /(.*?)(?:\\?.*?)? HTTP", request).group(1).decode("utf-8").rstrip("/")
                except Exception:
                    url = ure.search("(?:GET|POST) /(.*?)(?:\\?.*?)? HTTP", request).group(1).rstrip("/")
                logout("请求地址:{}".format(url))
                data = self.process_json(request)   # 处理请求数据
                if url == "": # 首页
                    await self.hanld_root(client)
                elif url == "wifi_connect" and data is not None: # 连接wifi
                    self.handle_wifi_cennect(client, data)
                elif url == "mqtt_connect" and data is not None:  # 连接MQTT
                    self.handle_mqtt_cennect(client, data)
                elif url == "access_set" and data is not None:    # 改变通道
                    self.handle_access_cenect(client, data)
                else:
                    self.handle_not_found(client, url)            # 404
            except Exception as e:
                logout("error:"+str(e),is_error = True)
                client.close()
            finally:
                print("关闭")
                client.close()

async def main_task():
    task = []
    AMS_WEB_MODEL = AMS_WEB() 
    AMS_WEB_MODEL.swcith_ap(1)      # 打开热点
    AMS_WEB_MODEL.auto_connection() # 自动连接wifi
    AMS_WEB_MODEL.auto_update_access(json_file)  # 历史通道
    AMS_WEB_MODEL.auto_conent_MQTT(json_file)
    #task.append(asyncio.create_task(AMS_WEB_MODEL.status_lED()))
    task.append(asyncio.create_task(AMS_WEB_MODEL.run_web_loop()))
    task.append(asyncio.create_task(AMS_WEB_MODEL.run_ams_loop()))
    await asyncio.gather(*task)
    
if __name__ == "__main__":

    asyncio.run(main_task())       
    
 