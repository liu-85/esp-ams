# YAO_AMS(基于YBA-AMS改进版)



<img src="./assets/83bc5bb869cc607bf0961988dcada98.jpg" alt="83bc5bb869cc607bf0961988dcada98" style="zoom:10%;" />



### 简介

​	本项目是适用于拓竹打印机的ams自动换料系统，基于YBA-AMS的版本进行改进。

   			1. 使用esp32为主控。使用mircopython进行开发，且使用TCP - web进行ams与操作人可视化交互。mqtt 与 拓竹打印机连接。且使用接口进行获取数据，可以方便二次开发。
   			2. 目前已经测试4通道，主板提供扩展接口,最多支持8通道。
   			3. 在A1mini上进行测试。只能使用0.1.03.01.00以下的版本固件。
   			4. 每次换料时间为1分钟半左右（从停止打印到开始打印，包括冲刷时间）。
   			5. 改进缓冲区，让远程送料不易卡住

### 使用说明

#### 初次配置

   1. 先连接主板的热点进行初始配置。热点名称为 AMS_WIFI,密码为;123456.连接后先进行主板wifi连接，与打印机同一个网络下。

      连接成功，再进行mqtt连接，输入对应的信息进行连接。之后就是料盘配置，为每一料盘设置样式，方便区分。页面如下:

      ![image-20250113204216748](C:\Users\yao\AppData\Roaming\Typora\typora-user-images\image-20250113204216748.png)

2. 配置完成后，电脑可以断开热点连接，进行wifi连接，后续需要修改配置的可以，输入页面最上面的ip地址进行访问，修改配置等。

3. 修改下Bambu Studio的换料G-code.修改内容再g-code下![image-20250113210443144](./assets/image-20250113210443144.png)

#### 打印时候配置



1. 打印前需要确认mqtt是否连接。主板上蓝灯为长亮为mqtt已经连接，闪烁的话为wifi已经连接，一会长亮，一会闪烁则为可以进行多色打印。

2. 进行打印，配置打印颜色对应的通道id就完成了。配置文件是可持久化的，主板断开电源重新启动，数据还是原先配置的。打印通道对应示例如下：

![image-20250113205448590](C:\Users\yao\AppData\Roaming\Typora\typora-user-images\image-20250113205448590.png)

### 部分命令说明

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

### 部分G-code说明

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

#### 挤出机

```python
M109 S[nozzle_temperature_range_high]  ; 设置喷嘴温度
M82  ; 激活绝对挤出模式
M83  ; 激活相对挤出模式
G92 E0              ; 重置挤出量
```

#### 风扇

```python
M106 P1 S0;关闭风扇1（扇热风扇）
M106 P1 S255:风扇开到最大
```

#### 切料软件配置

```python
M73 P101 R[next_extruder]
```





   

