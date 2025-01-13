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
    except OSError as e:
        logout("Error reading JSON file:"+str(e),is_error=True)
        return None
    
def write_json_file(file_path, data):
    try:
        with open(file_path, 'w') as file:
            ujson.dump(data, file)
            logout("JSON data written to file successfully")
    except OSError as e:
        logout("Error writing JSON file:",+str(e),is_error=True)
        
template_info = {
    "wifi_username" : "",
    "wifi_password" : "",
    #打印机信息
    "mqtt_server":"",
    "client_id":"",
    "DEVICE_SERIAL":"",# 序列号
    "username":"bblp",
    "password":"",
    "mqtt_port":"",
    "filament_current":0}




