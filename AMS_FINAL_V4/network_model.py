import network
from logout import logout
import time
from info_load import read_profiles

class network_model:
    def __init__(self):
        self.ap_ssid = "AMS_WIFI"       # wifi初始账户
        self.ap_password = "A12345678"  # wifi初始密码
        self.ap_authmode = 3  # WPA2
        self.wlan_ap = network.WLAN(network.AP_IF)  # 热点模式
        self.wlan_sta = network.WLAN(network.STA_IF) # 连接wifi模式
        self.wlan_ap.active(False)
        self.wlan_sta.active(False)
        
    def swcith_ap(self,status=1):
        #打开关闭热点,1是开，0是关
        if status:
            self.wlan_ap.active(True)
            self.wlan_ap.config(essid=self.ap_ssid, password=self.ap_password, authmode=self.ap_authmode)
            logout("当前热点已打开")
            logout('热点名称 ' + self.ap_ssid + ', 密码: ' + self.ap_password)
        else:
            self.wlan_ap.active(False)
            logout("当前热点已关闭")
        return self.wlan_ap.active()
        
    def auto_connection(self):
        # 从历史文件连接wifi
        connected = False
        try:
            if self.wlan_sta.isconnected():
                return self.wlan_sta
            # Read known network profiles from file
            profiles = read_profiles()
            # Search WiFis in range
            self.wlan_sta.active(True)
            networks = self.wlan_sta.scan()
            AUTHMODE = {0: "open", 1: "WEP", 2: "WPA-PSK", 3: "WPA2-PSK", 4: "WPA/WPA2-PSK"}
            for ssid, bssid, channel, rssi, authmode, hidden in sorted(networks, key=lambda x: x[3], reverse=True):
                ssid = ssid.decode('utf-8')
                encrypted = authmode > 0
                print("ssid: %s chan: %d rssi: %d authmode: %s" % (ssid, channel, rssi, AUTHMODE.get(authmode, '?')))
                if encrypted:
                    if ssid in profiles:
                        password = profiles[ssid]
                        connected = self.do_connect(ssid, password)
                    else:
                        print("skipping unknown encrypted network")
                else:  # open
                    connected = self.do_connect(ssid, None)
                if connected:
                    break
        except OSError as e:
            logout("exception"+str(e))
        return self.wlan_sta if connected else None
    
    def do_connect(self,ssid,password):
        # 连接wifi
        self.wlan_sta.active(True)
        self.wlan_sta.disconnect()
        logout('正在连接wifi %s...' % ssid)
        self.wlan_sta.connect(ssid, password)
        for retry in range(100):
            # 等待连接
            connected = self.wlan_sta.isconnected()
            if connected:
                break
            time.sleep(0.1)
            logout('.', end='')
        if connected:
            logout('\n连接成功. 网络信息：: '+self.wlan_sta.ifconfig()[0])
            
        else:
            logout('\n连接失败. 不能去连接: ' + ssid)
        return connected
        