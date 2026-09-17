"""
boot_resources.py —— 启动阶段"趁堆还干净"预先申请好的资源
=========================================================

--------------------------------------------------------------------------
为什么要这个文件
--------------------------------------------------------------------------
ESP32-C3 只有约 191 KB 的堆，而加载完一整套应用（AMS_WEB → AMS_MODEL →
device_processing / bambu / motor_clutch …）要吃掉 100 KB 以上，加载完
名义上还剩 64 KB，但**实测最大连续块只剩 3,584 字节**（gc.collect() 也
只能多合出 2 KB —— MicroPython 的 GC 不做压缩，碎片合并救不回来）。

这时候再想"新申请一大块东西"，失败的方式非常难查：
    · 开 WiFi 热点   → 直接硬复位，串口上没有 traceback、没有 panic
    · 建第一个 socket → OSError: -203（getaddrinfo）/ OSError: 105（socket()）

实测（同一块板子、同一套固件）：
    加载完应用再建 socket   → free 54 KB → OSError 105，Web 服务起不来
    先把监听 socket 建好，再加载应用 → free 50 KB → socket()/connect()/
                              accept() 全部正常

结论：**凡是"要占一大块、且只在启动时做一次"的资源，都要抢在应用加载
之前申请。** WiFi 那一半在 main.py 的第 0 / 0.5 步，Web 监听 socket
这一半在这里。

--------------------------------------------------------------------------
用法
--------------------------------------------------------------------------
    main.py（堆还干净时）:
        import boot_resources
        boot_resources.prepare_web_server()

    AMS_WEB.run_web_loop():
        sock = boot_resources.take_web_server()   # 拿不到就自己现场建
"""

# Web 服务的端口和 listen 队列长度放在这里，是因为 main.py 要在加载应用
# 之前就照着它们把监听 socket 建好 —— 所以它俩必须由一个"很轻、不依赖
# AMS_WEB"的模块提供。AMS_WEB 直接 import 回去用，避免两处写死。
WEB_PORT = 80
# 队列长度不能小：浏览器一次开 6 个连接，旧值 2 会把多出来的 SYN 直接丢掉，
# 由浏览器按 TCP 退避重试（1s→2s→4s…）——那正是"刷新好几次才出来页面"。
LISTEN_BACKLOG = 8

_server_socket = None


def prepare_web_server():
    """在堆还干净的时候把 Web 监听 socket 建好并绑上端口。

    重复调用只会建一次；失败时返回 None，交给 AMS_WEB 现场兜底。
    """
    global _server_socket
    if _server_socket is not None:
        return _server_socket

    import socket

    try:
        srv = socket.socket()
        try:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        srv.bind(("0.0.0.0", WEB_PORT))
        srv.listen(LISTEN_BACKLOG)
        srv.setblocking(False)
    except Exception:
        return None

    _server_socket = srv
    return srv


def take_web_server():
    """把预建好的监听 socket 取走（只取一次，取走后本模块不再持有）。

    取不到（比如从 REPL 直接跑 main_task，没走 main.py 的预建步骤）
    就返回 None，调用方自己现场建一个。
    """
    global _server_socket
    srv = _server_socket
    _server_socket = None
    return srv
