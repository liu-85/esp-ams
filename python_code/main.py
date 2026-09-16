"""
main.py —— 上电自动启动入口
============================

MicroPython 上电时会依次执行根目录下的 boot.py 和 main.py，
所以把启动逻辑放在这里，插上电就能跑，不需要手动敲命令。

启动流程：
    1. 加载 config.json（如果有）
    2. 用里面存的 WiFi 密码自动连接（network_model.auto_connection）
    3. 连不上就打开自己的热点 AMS_WIFI，让用户做初次配置
    4. 恢复通道映射和上次记录的当前料盘
    5. 自动连接打印机 MQTT
    6. 并发跑三个任务：状态灯 / Web 配置页面 / 换料主循环

注意：这个文件必须是 .py 放在文件系统上，**不能编译成 .mpy**
      （MicroPython 是直接 exec 文件内容来执行 main.py 的）。
"""

import uasyncio as asyncio
from AMS_WEB import main_task

if __name__ == "__main__":
    asyncio.run(main_task())
