"""
main.py —— 上电自动启动入口
============================

MicroPython 上电时会依次执行根目录下的 boot.py 和 main.py，
所以把启动逻辑放在这里，插上电就能跑，不需要手动敲命令。

启动流程：
    0. ★ 先趁堆最空把 WiFi 驱动初始化出来（下面会解释为什么必须抢在最前面）
    1. 加载 config.json（如果有）
    2. 用里面存的 WiFi 密码自动连接（network_model.auto_connection）
    3. 连不上就打开自己的热点 AMS_WIFI，让用户做初次配置
    4. 恢复通道映射和上次记录的当前料盘
    5. 自动连接打印机 MQTT
    6. 并发跑三个任务：状态灯 / Web 配置页面 / 换料主循环

★★★ 为什么第 0 步必须排在 `from AMS_WEB import main_task` 之前 ★★★

    现象：固件烧好后主板卡在 REPL，手机搜不到 AMS_WIFI，串口只有一行
              RuntimeError: Wifi Unknown Error 0x0101

    根因：这个 0x0101 是 ESP-IDF 的 ESP_ERR_NO_MEM（MicroPython 的错误码
          对照表只认识 0x3001~0x300E 那几个 WiFi 专用码，所以只能报"未知错误"）。

          esp_wifi_init() 要向堆里申请一块约 24KB 的**连续**内存。而紧接着的
          `from AMS_WEB import main_task` 会把整套应用模块加载进堆
          （.mpy 字节码也要在内存里展开成代码对象，实测吃掉约 107 KB），
          顺便把剩下的堆切得很碎。

    实测（ESP32-C3，本机串口量的）：
          刚进 main.py，堆空了          → 200,816 字节空闲 → WLAN(AP_IF) 成功
          先 import AMS_WEB 再建 WLAN   →  92,672 字节空闲 → 仍然失败
          （名义上还剩 92KB，但已经拼不出连续的一块 24KB）

    所以不是"内存不够"，是"内存太碎"。把顺序倒过来就好了：
    此刻堆几乎是空的，一次成功；之后 network_model 里再调 network.WLAN()，
    MicroPython 只是把同一个缓存对象还回来 —— 实测只花 16 字节。

    ⚠️ 以后往这个文件前面加 import 时，务必保持第 0 步在最上面。

注意：这个文件必须是 .py 放在文件系统上，**不能编译成 .mpy**
      （MicroPython 是直接 exec 文件内容来执行 main.py 的）。
"""

# ---------------------------------------------------------------------------
# 第 0 步：趁堆最空，先把 WiFi 驱动初始化出来（顺序不能挪到下面 import 之后）
# ---------------------------------------------------------------------------
import gc
import network

gc.collect()
try:
    network.WLAN(network.AP_IF)      # 配置热点用
    network.WLAN(network.STA_IF)     # 连路由器用
except Exception as _wifi_error:     # noqa: BLE001 - 这里失败也不要挡住后面
    print("WiFi 预初始化失败（启动流程里还会再试一次）: %r" % (_wifi_error,))

# ---------------------------------------------------------------------------
# 再加载应用（这一步会把源码现场编译，吃掉一百多 KB 堆）
# ---------------------------------------------------------------------------
import uasyncio as asyncio
from AMS_WEB import main_task

if __name__ == "__main__":
    asyncio.run(main_task())
