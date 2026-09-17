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

★★★ 第二层坑：光建 WLAN 对象还不够，"开热点"也必须抢在前面 ★★★

    补上第 0 步之后 0x0101 消失了，但板子开始每 3.3 秒重启一轮：串口上反复
    刷 HARD_RESET，既没有 traceback 也没有 Guru Meditation，只能看到
    "自动联网失败，打开配置热点 AMS_WIFI"，而紧随其后的那句
    "配置热点已打开"永远打不出来。

    真正死的一句是 network_model.swcith_ap() 里的 wlan_ap.active(True)：
    esp_wifi_init() 只是把 WiFi 驱动挂上来，真正开射频的是 esp_wifi_start()，
    它同样要一大块**连续**内存。

    实测（同一块板子、同一套固件）：
          干净堆上开热点      → 空闲 166 KB → 成功，只花 4,592 字节
          加载完应用再开热点  → 空闲  64 KB → 直接硬复位
          （名义上 64 KB 是够的，问题是实测**最大连续块只剩 3,584 字节**，
            而 gc.collect() 只能多合出 2 KB —— MicroPython 的 GC 不做压缩，
            收集一下根本救不回来）

    所以 WiFi 的"起"必须在堆碎掉之前做完。第 0.5 步干的就是这件事：
    有保存的 WiFi 就连路由器，没有就在这儿把 AMS_WIFI 开起来。

    ⚠️ 推论：运行过程中不要反复开关热点。配网成功后 swcith_ap(0) 是安全的
      （关只是释放内存），但之后想在网页上再点"打开热点"，就得在碎片堆里
      重新申请连续内存，同样会复位。要换 WiFi 请重启设备走第 0.5 步。

★★★ 第三层坑：热点已经开着，也会被 config() 打死 ★★★

    把上面两层都补好之后，板子不再重启，却在 main_task 里抛
    RuntimeError: Wifi Unknown Error 0x0101，位置是 swcith_ap() 的
    wlan_ap.config(essid=..., password=..., authmode=...)。

    上机逐个参数试出来的结果（热点**确实**开着、ifconfig 也能读出
    192.168.4.1、free=56,872）：
          ap.active()               -> True
          ap.active(True) 再来一次  -> OK   （已经是 no-op，不重新申请内存）
          ap.ifconfig()             -> ('192.168.4.1', ...) 正常
          ap.config(essid=...)      -> RuntimeError 0x0101
          ap.config(password=...)   -> RuntimeError 0x0101
          ap.config(authmode=...)   -> RuntimeError 0x0101

    也就是：**碎堆上任何 esp_wifi_set_config(AP) 都必定失败**，跟传哪个参数
    无关（怀疑是 AP 改配置要顺带重启 AP，于是又去要连续内存）。而 main_task
    里那次调用只是把早已生效的 SSID/密码再下发一遍，纯多余却能把整个启动打断。

    修法：swcith_ap(1) 做成幂等 —— 热点已经 active 就直接返回，绝不再 config。

★★★ 第四层坑：STA 也要在干净堆上开，否则"扫描/连接 WiFi"会复位 ★★★

    scan_networks() / do_connect() 里都有 wlan_sta.active(True)。以前只建了
    STA 对象没开射频，用户一点"扫描 WiFi"就要在碎堆上 esp_wifi_start。

    实测：AP 已经起来之后再 sta.active(True) **只花 48 字节**（驱动共用的），
    所以第 0 步顺手把 STA 也开了几乎零成本；开完之后在同一块碎堆上
    sta.scan() 和 sta.connect() 都正常。

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
    _ap_if = network.WLAN(network.AP_IF)      # 配置热点用
    _sta_if = network.WLAN(network.STA_IF)    # 连路由器用
    # ★★ 两个口都在这里**真的开起来**，不能只建对象 ★★
    #
    # 只建对象的话，射频（esp_wifi_start）还留着没开，等到应用加载完
    # （网络扫描按钮、配网时连路由器）再调 active(True)，就得在碎堆上
    # 申请连续内存 —— 又是那个没有 traceback 的硬复位。
    #
    # 实测代价（板子串口，干净堆 196,992 起步）：
    #     建两个 WLAN 对象   172,352   （-24,640，主要是 AP 那一份）
    #     ap.active(True)   168,176   （-4,176）
    #     sta.active(True)  168,128   （**-48**，驱动已经起来了，几乎不要钱）
    #
    # 这里只预开 STA，AP 留给第 0.5 步按需开（连上路由器时不需要热点）。
    # 顺序无所谓 —— 此刻堆还是完整的（仍有 170KB 左右），先开谁都够。
    #
    # 开完之后实测（同一块碎堆，free=56,320）：
    #     sta.scan()                 -> OK
    #     sta.connect(ssid, pwd)     -> OK  ← 预启动之前这里是会炸的
    _sta_if.active(True)             # 连路由器 / 扫 WiFi 都靠它，先在这儿开好
    del _ap_if, _sta_if
except Exception as _wifi_error:     # noqa: BLE001 - 这里失败也不要挡住后面
    print("WiFi 驱动初始化失败: %r" % (_wifi_error,))

# ---------------------------------------------------------------------------
# 第 0.5 步：有保存的 WiFi 就连路由器，没有就把配置热点 AMS_WIFI 开起来
#
# ★★ 同样必须在 `from AMS_WEB import main_task` 之前，原因见文件头 ★★
#    STA 的射频第 0 步已经开好了，这一步只负责"连哪个"：
#    有保存的 WiFi 就连路由器，没有就把配置热点 AMS_WIFI 开起来
#    （AP 的 active(True) 也必须在应用加载前做，同样是 esp_wifi_start
#     要连续内存；此刻堆还有 170KB 左右，一次就开得起来）。
#
#    network_model 很轻（只依赖 network / time / logout / info_load），
#    先把它引进来不会把堆吃出缺口。后面应用再 import 它时只是拿缓存。
# ---------------------------------------------------------------------------
try:
    import network_model

    _wifi = network_model.network_model()
    if _wifi.auto_connection():
        print("已联网，IP = %s" % _wifi.sta_ip())
    else:
        _wifi.swcith_ap(1)           # 开配置热点：手机连 AMS_WIFI 后访问 192.168.4.1
except Exception as _bringup_error:  # noqa: BLE001 - 失败也不要挡住后面
    print("启动阶段联网失败（应用启动后还会再试一次）: %r" % (_bringup_error,))

# ---------------------------------------------------------------------------
# 第 0.6 步：同样趁堆干净，把 Web 服务的监听 socket 先建好
#
# ★★ 原因和上面完全一样，详见 boot_resources.py ★★
#    实测：加载完应用再 socket.getaddrinfo() → OSError(-203)，
#          socket.socket() → OSError(105)，Web 服务根本起不来；
#          而在加载应用之前先建好监听 socket，之后碎片堆里再
#          socket() / connect() / accept() 全都正常（实测 free 只剩 50KB
#          时依然能连上自己、accept 成功）。
#    换句话说：不是"堆不够"，是"第一个 socket 必须在干净堆上开"。
# ---------------------------------------------------------------------------
try:
    import boot_resources

    if boot_resources.prepare_web_server() is None:
        print("预建 Web 监听 socket 失败（应用里会现场再建一次）")
except Exception as _sock_error:     # noqa: BLE001
    print("预建 Web 监听 socket 出错（应用里会现场再建一次）: %r" % (_sock_error,))

# ---------------------------------------------------------------------------
# 再加载应用（这一步会把整套模块加载进堆，实测吃掉约 102 KB）
# ---------------------------------------------------------------------------
import uasyncio as asyncio
from AMS_WEB import main_task

if __name__ == "__main__":
    asyncio.run(main_task())
