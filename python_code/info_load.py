import ujson
from logout import logout
NETWORK_PROFILES = 'wifi.dat'

def read_profiles():
    with open(NETWORK_PROFILES) as f:
        lines = f.readlines()
    profiles = {}
    for line in lines:
        ssid, password = line.strip("\n").split(";")
        profiles[ssid] = password
    return profiles


def write_profiles(profiles):
    lines = []
    for ssid, password in profiles.items():
        lines.append("%s;%s\n" % (ssid, password))
    with open(NETWORK_PROFILES, "w") as f:
        f.write(''.join(lines))
        


# 读取JSON文件
def read_json_file(file_path):
    try:
        with open(file_path, 'r') as file:
            data = ujson.load(file)
            return data
    except Exception as e:
        # ★ 只接 OSError 是不够的：config.json 被写坏（掉电写一半）时
        #   ujson.load 抛的是 ValueError，漏出去会让整条配置链路崩掉。
        logout("Error reading JSON file: " + str(e), is_error=True)
        return None
    
def write_json_file(file_path, data):
    try:
        with open(file_path, 'w') as file:
            ujson.dump(data, file)
            logout("JSON data written to file successfully")
    except OSError as e:
        # 原来这里写成了 logout("...", +str(e), is_error=True)：
        # 第二个位置参数是 is_print/is_save，异常信息根本打不出来，还会被当布尔用。
        logout("Error writing JSON file: " + str(e), is_error=True)

# 配置模板。
# ★ 键名必须和网页 POST 过来的 JSON、以及 auto_conent_MQTT() 读的键**完全一致**：
#   打印机的访问码叫 "mqtt_password"（不是 "password"），
#   客户端名 / 用户名 / 端口也都要有默认值，否则 auto_conent_MQTT 会 KeyError。
template_info = {
    "wifi_username" : "",
    "wifi_password" : "",
    #打印机信息
    "mqtt_server":"",
    "client_id":"mqttx_3c73cd31",
    "DEVICE_SERIAL":"",# 序列号
    "username":"bblp",
    "mqtt_password":"",
    "mqtt_port":"8883",
    "filament_current":0}




