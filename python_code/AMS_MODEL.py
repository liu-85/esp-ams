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
        self.color_list = ["red" for n in range(len(self.meterial_list))]
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
        file_data = read_json_file(file_path)
        new_data = file_data.get("access",None)
        color_list = file_data.get("color_list",None)
        if color_list:
            self.color_list = color_list
        if new_data:
            self.access_list = new_data
            self.dianji_dict = {key:value for key,value in zip(self.access_list,self.meterial_list)}
            self.filament_current = self.now_filament()
            return True
        self.filament_current = self.now_filament()
        return False
        
    def now_filament(self,start_filamet=1):
        # 获取当前料盘
        times_ms = 4000
        step_time = 500
        if start_filamet<=4 and start_filamet>0:
            # 第一个测测试
            logout("开始获取当前料盘id"+str(start_filamet))
            count = 0
            while count < times_ms/step_time:
                count+=1
                self.dianji_dict[start_filamet].dianji_roll(-1,step_time)
                if self.dianji_dict[start_filamet].dianji_top.value():
                    self.dianji_dict[start_filamet].dianji_roll(1,count*step_time)
                    return start_filamet
        for key in self.dianji_dict:
            if key != start_filamet:
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
        if error_count > counts*0.2:
            return False
        return True 
        
    def exchange_fileament(self,new_filament_id,count=0):
        if count == 0:
            self.filament_current = self.now_filament(self.filament_current)
        
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
            self.piblish_gcode("M400;\n G1 E-50 F200;")
            self.client.wait_msg()
            logout(self.update_print_info())
            self.dianji_dict[self.filament_current].dianji_roll(-1,1000)
            if not self.fileament_move(self.filament_current,counts=15,orientation=-1):
                logout("退料失败")
                return False
        # 进料
        for n in range(10): # 运行10次
            print("开始进料",n)
            if not self.fileament_move(new_filament_id,orientation=1):
                break
        self.piblish_gcode("M400;\n G1 E50 F200;")
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
        try:
            count = 0;
            con_num = 0
            while True:
                if not self.check_mqtt_connection():
                    logout("mqtt未连接")
                    # 尝试连接
                    if con_num<5:
                        self.conent_and_subscribe()
                        con_num+=1
                    await asyncio.sleep(10)
                    continue
                try:
                    con_num = 0
                    count+=1
                    if count%20==0:
                        count =0
                        self.client.disconnect()
                        await asyncio.sleep(3)
                        self.conent_and_subscribe()          
                    await asyncio.sleep_ms(200)
                    self.piblish(START_PUSH)
                    #self.piblish(banbu_start)
                    self.client.wait_msg() # 检查是否有新的消息到达
                    info = self.update_print_info()
                    # 判断是否换料
                    if info["change_info"]["code"] and exchange_count <=5:
                        exchange_count += 1
                        if self.exchange_fileament(info["change_info"]["filament_next"]+1,exchange_count):
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
        except Exception as e:
            logout("error:"+str(e),is_error = True)
            
                
if __name__ == "__main__":

    AMS_MODEL = AMS()
    print(AMS_MODEL.now_filament())
    #AMS_MODEL.auto_update_access("config.json")
    #print(AMS_MODEL.do_connect("Mr","15816728266"))
    #AMS_MODEL.conent_and_subscribe()
    #AMS_MODEL.piblish(START_PUSH)
    #AMS_MODEL.main_loop()
    