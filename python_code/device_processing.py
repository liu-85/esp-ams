from machine import Pin,PWM
import time
import uasyncio as asyncio

def huxideng():
    pin2 = PWM(Pin(2))
    pin2.freq(1000)
    while True:
        for n in range(1000):
            pin2.duty(n)
            time.sleep_ms(1)
        for n in range(1000,-1,-1):
            pin2.duty(n)
            time.sleep_ms(1)

def shiji(pin,jiaodu):    
    p2 = PWM(Pin(pin))
    p2.freq(50)
    f = int(((jiaodu+10)/90+0.5)*1023/20)
    p2.duty(f)

class material:
    def __init__(self,dianji_top,dianji_bottom):
        self.dianji_top = Pin(dianji_top, Pin.OUT)
        self.dianji_bottom = Pin(dianji_bottom, Pin.OUT)
        self.stop_roll()
    def dianji_roll(self,direction=1,times_ms=200):
        if direction == 1:
            self.dianji_top.value(1)
            self.dianji_bottom.value(0)
        elif direction ==-1:
            self.dianji_top.value(0)
            self.dianji_bottom.value(1)
        time.sleep_ms(times_ms)
        self.stop_roll()
        
    def is_jam(self):
        if self.dianji_top.value() and self.dianji_top.value():
            return True
        return False
        
    def is_one(self,duration = 10):
        start_time = time.time()
        while time.time()-start_time<duration:
            if not self.weidong_1.value():
                self.stop_roll()
                return True
            self.dianji_roll(-1)
        self.stop_roll()
        return False

    def stop_roll(self):
        self.dianji_top.value(0)
        self.dianji_bottom.value(0)
        
            
            

class stepping_motor_28BYJ48:
    def __init__(self,pin1,pin2,pin3,pin4,delay_time_ms=2):
        self.pin1 = Pin(pin1, Pin.OUT)
        self.pin2 = Pin(pin2, Pin.OUT)
        self.pin3 = Pin(pin3, Pin.OUT)
        self.pin4 = Pin(pin4, Pin.OUT)
        self.delay_time_ms = delay_time_ms
        self.init_value()
    def init_value(self):
        self.pin1.value(0)
        self.pin2.value(0)
        self.pin3.value(0)
        self.pin4.value(0)
    def roll(self,rotation_number,direction=1):
        for n in range(rotation_number*2050):
            value_list = [0,0,0,0]
            value_list[n%4]= 1
            if direction==1:
                self.pin1.value(value_list[0])
                self.pin2.value(value_list[1])
                self.pin3.value(value_list[2])
                self.pin4.value(value_list[3])
            elif direction== -1:
                self.pin4.value(value_list[0])
                self.pin3.value(value_list[1])
                self.pin2.value(value_list[2])
                self.pin1.value(value_list[3])
            time.sleep_ms(self.delay_time_ms)
        self.init_value()
if __name__ == "__main__":
    roll_dianji1 = material(5,18)
    roll_dianji1.dianji_roll(1,1000)
    roll_dianji2 = material(16,17)
    roll_dianji2.dianji_roll(1,1000)
    roll_dianji3 = material(22,23)
    roll_dianji3.dianji_roll(1,1000)
    roll_dianji4 = material(19,21)
    roll_dianji4.dianji_roll(1,1000)
    
    
    
    