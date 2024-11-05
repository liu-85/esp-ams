# 笔记AMS

以下代码来自[ha-bambulab/custom_components/bambu_lab/pybambu/commands.py at main · greghesp/ha-bambulab (github.com)](https://github.com/greghesp/ha-bambulab/blob/main/custom_components/bambu_lab/pybambu/commands.py)

```python
"""MQTT Commands"""
# 开灯
CHAMBER_LIGHT_ON = {
    "system": {"sequence_id": "0", "command": "ledctrl", "led_node": "chamber_light", "led_mode": "on",
               "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}}
# 关灯

CHAMBER_LIGHT_OFF = {
    "system": {"sequence_id": "0", "command": "ledctrl", "led_node": "chamber_light", "led_mode": "off",
               "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}}
# 设置速度
SPEED_PROFILE_TEMPLATE = {"print": {"sequence_id": "0", "command": "print_speed", "param": ""}}
# 获取版本
GET_VERSION = {"info": {"sequence_id": "0", "command": "get_version"}}
# 恢复打印
PAUSE = {"print": {"sequence_id": "0", "command": "pause"}}
# 暂停打印
RESUME = {"print": {"sequence_id": "0", "command": "resume"}}
# 停止打印
STOP = {"print": {"sequence_id": "0", "command": "stop"}}
# 获取所有
PUSH_ALL = {"pushing": {"sequence_id": "0", "command": "pushall"}}
# 开始推送
START_PUSH = { "pushing": {"sequence_id": "0", "command": "start"}}
# 发送gcode
SEND_GCODE_TEMPLATE = {"print": {"sequence_id": "0", "command": "gcode_line", "param": ""}} # param = GCODE_EACH_LINE_SEPARATED_BY_\n

# X1 only currently
GET_ACCESSORIES = {"system": {"sequence_id": "0", "command": "get_accessories", "accessory_type": "none"}}
```

另外添加

```python
修改当前耗材K值
tray_id字段254表示外挂料盘
{
  "print": {
    "command": "extrusion_cali_set",
    "k_value": 0.02500000037252903,
    "n_coef": 1.399999976158142,
    "sequence_id": "20209",
    "tray_id": 254
  },
  "user_id": "1234567890"
}

退料
{
  "print": {
    "command": "ams_change_filament",
    "curr_temp": 230,
    "sequence_id": "20211",
    "tar_temp": 230,
    "target": 255
  },
  "user_id": "1234567890"
}

进料
{
  "print": {
    "command": "ams_change_filament",
    "curr_temp": 230,
    "sequence_id": "20211",
    "tar_temp": 230,
    "target": 254
  },
  "user_id": "1234567890"
}

打印控制 暂停继续停止
{
  "print": {
    "command": "resume",
    "sequence_id": "20211"
  },
  "user_id": "1234567890"
}

```


# G-code

#### 擦拭插头

```python
M400
M106 P1 S178; 开启风扇
M400 S3; 设置打印速度
; 移动打印头进行擦拭
G1 X-3.5 F18000
G1 X-13.5 F3000
G1 X-3.5 F18000
G1 X-13.5 F3000
G1 X-3.5 F18000
G1 X-13.5 F3000
M400  ; 等待所有动作完成
M106 P1 S0  ; 关闭风扇

###########
M400 S3;\nG1 X-3.5 F18000;\nG1 X-13.5 F3000;\nG1 X-3.5 F18000;\nG1 X-13.5 F3000;\nG1 X-3.5 F18000;\nG1 X-13.5 F3000; \nM400;
```

#### 切割材料

```python
G1 X180 F18000 # 跑到切割待机位
G1 X200 F300	# 进行切割
G1 X-13.5 F6000		# 跑回冲刷区
G1 E-5 F100	# 退出5mm的丝料
```

####  设置安全距离

```python
; 移动打印头到安全位置 
G91  ;相对定位 
G1 Z10 F600  ;将Z轴提升10mm，避免与打印物接触
G90  ; 绝对定位
```

### 挤出机

```python
M109 S[nozzle_temperature_range_high]  ; 设置喷嘴温度
M82  ; 激活绝对挤出模式
M83  ; 激活相对挤出模式
G92 E0              ; 重置挤出量
```

### 风扇

```python
M106 P1 S0;关闭风扇1（扇热风扇）
M106 P1 S255:风扇开到最大
```

### 切片软件配置

```python
M73 P101 R[next_extruder]
G91
G1 Z10 F10000 
G90
G1 X-13.5 F10000
M400 U1
```

