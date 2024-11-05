from device_processing import material
from bambu.bambu_mqtt import Bambu_mqtt_cliet
import time
from logout import logout
from bambu.bambu_commands import banbu_start,START_PUSH
import ujson
from bambu.bambu_commands import *
from info_load import read_json_file
import uasyncio as asyncio
class AMS(Bambu_mqtt_cliet):
    def __init__(self):
        super().__init__(mqtt_server="", DEVICE_SERIAL="", password="") #继承MQTT类
        self.meterial_list = [material(18,5),material(17,16),material(23,22),material(21,19)]
        self.access_list = [n+1 for n in range(len(self.meterial_list))]  # 颜色映射通道
        self.dianji_dict = {key:value for key,value in zip(self.access_list,self.meterial_list)} # 绑定关系
        self.filament_current = 0 #self.now_filament()  # 获取当前的料盘
        self.now_warring = ""
        
    def update_warring(self,text,is_error=False):
        if len(self.now_warring)>=30:
            indexs = self.now_warring.find("\n")
            self.now_warring = self.now_warring[indexs:]
        self.now_warring += (text+"\n")
        
    def auto_update_access(self,file_path):
        new_data = read_json_file(file_path).get("access",None)
        if new_data:
            self.access_list = new_data
            self.dianji_dict = {key:value for key,value in zip(self.access_list,self.meterial_list)}
            self.filament_current = self.now_filament()
            return True
        self.filament_current = self.now_filament()
        return False
        
    def now_filament(self):
        # 获取当前料盘
        times_ms = 2000
        step_time = 500
        for key in self.dianji_dict:
            logout("开始获取当前料盘id"+str(key))
            count = 0
            while count < times_ms/step_time:
                count+=1
                self.dianji_dict[key].dianji_roll(-1,step_time)
                if self.dianji_dict[key].dianji_top.value():
                    self.dianji_dict[key].dianji_roll(1,count*step_time)
                    return key
            self.dianji_dict[key].dianji_roll(1,times_ms)
        return 0
    
    
    def fileament_move(self,fileament_id,counts=10,orientation=1):
        error_count = 0
        for n in range(counts):
            self.dianji_dict[fileament_id].dianji_roll(orientation,500)  
            if (orientation ==1 and self.dianji_dict[fileament_id].dianji_bottom.value()) or \
               (orientation == -1 and self.dianji_dict[fileament_id].dianji_top.value()):
                error_count += 1
        if error_count > counts*0.3:
            return False
        return True 
        
    def exchange_fileament(self,new_filament_id):
        logout("当前料盘:"+str(self.filament_current)+"新的料盘:"+str(new_filament_id))
        if self.filament_current == new_filament_id:
            logout("无需换料")
            return True
        
        self.piblish_gcode("M83")
        self.client.wait_msg()
        time.sleep(0.1)
        # 退料
        if self.filament_current: # 有料
            logout("开始退料")
            self.piblish_gcode("M400;\n G1 E-60 F200;")
            self.client.wait_msg()
            logout(self.update_print_info())
            if not self.fileament_move(self.filament_current,counts=20,orientation=-1):
                logout("退料失败")
                return False
        # 进料
        for n in range(10): # 运行10次
            print("开始进料",n)
            if not self.fileament_move(new_filament_id,orientation=1):
                break
        self.piblish_gcode("M400;\n G1 E60 F200;")
        self.client.wait_msg()
        logout(self.update_print_info())
        self.dianji_dict[new_filament_id].dianji_roll(1,1000)
        self.filament_current = new_filament_id
        logout("换料成功")
        return True
            
    async def run_ams_loop(self):
        exchange_count = 0 # 重复换料次数
        # 主要运行线程
        # 接受消息获取运行信息
        while True:
            if not self.check_mqtt_connection():
                logout("mqtt未连接")
                await asyncio.sleep(10)
                continue
            try:
                await asyncio.sleep_ms(500)
                self.piblish(START_PUSH)
                self.piblish(banbu_start)
                self.client.wait_msg() # 检查是否有新的消息到达
                info = self.update_print_info()
                # 判断是否换料
                if info["change_info"]["code"] and exchange_count <3:
                    exchange_count += 1
                    if self.exchange_fileament(info["change_info"]["filament_next"]+1):
                        exchange_count = 0
                        self.piblish(bambu_resume) # 继续打印
                        for n in range(10):
                            await asyncio.sleep_ms(500)
                            self.client.wait_msg()
                            data = ujson.loads(self.new_message).get("print",{})
                            if data.get("command","") == "resume" and data.get("result","") == "success":
                                logout("继续打印")
                                break
                if exchange_count >3 :
                    logout("AMS异常，已经暂停")
            except Exception as e:
                logout("error:"+str(e))
                
                
                
                
                        
        


if __name__ == "__main__":
    AMS_MODEL = AMS()
    AMS_MODEL.auto_update_access("config.json")

    