"""
network_model.py —— WiFi 联网与配网
===================================

本文件只负责 WiFi，不管 MQTT（MQTT 在 bambu/bambu_mqtt.py 里）。

--------------------------------------------------------------------------
一、上电自动联网：**直连，不扫描**
--------------------------------------------------------------------------
   旧逻辑：每次开机先 scan() 一遍，从扫描结果里挑信号最强的、且在记录里的 AP 去连。
   问题：  scan() 是阻塞操作，ESP32-C3 上要 1.5~3 秒。开机慢、网页也跟着卡；
           而且扫描期间射频要切信道，会影响已经连上的连接。

   新逻辑：首次配网成功后 SSID/密码已经存在 wifi.dat 里，"直接连"又快又稳。
           auto_connection() 只做一件事：拿记录里的 SSID/密码挨个尝试连接，
           全程不扫描。

   想扫描只有一个入口：用户在网页上点「重新扫描 WiFi」→ 见 scan_networks(force=True)。

--------------------------------------------------------------------------
二、连不上就开热点（最多尝试 WIFI_BOOT_ATTEMPTS 次）
--------------------------------------------------------------------------
   auto_connection() 里总共最多尝试 WIFI_BOOT_ATTEMPTS 次（默认 3 次，
   有多个已保存 SSID 时轮流试）。全部失败返回 None，由 AMS_WEB.main_task
   打开配置热点 AMS_WIFI，让用户用手机连上来重新配网。

--------------------------------------------------------------------------
三、连接成功判定：必须拿到 IP
--------------------------------------------------------------------------
   `isconnected()` 只表示"和 AP 关联上了"，此时 DHCP 可能还没完成，
   ifconfig()[0] 还是 0.0.0.0。这时候去连 MQTT 必然失败。
   所以 do_connect() 会一直等到拿到非 0.0.0.0 的 IP 才算成功。

--------------------------------------------------------------------------
四、可调参数
--------------------------------------------------------------------------
   全部集中在下面这几个常量里，不用翻代码。
"""

import network
import time

from logout import logout
from info_load import read_profiles


# ==========================================================================
# 配置热点（AP）—— 自动联网失败时打开，用手机连它来配网
# ==========================================================================
AP_SSID = "AMS_WIFI"
AP_PASSWORD = "A12345678"   # 至少 8 位，否则某些手机不认；想空密码改成 "" 并把 AP_AUTHMODE 设 0
AP_AUTHMODE = 3             # 3 = WPA2-PSK

# ==========================================================================
# 联网策略
# ==========================================================================
WIFI_BOOT_ATTEMPTS = 3      # 上电自动连接的总尝试次数，用完就开热点
CONNECT_TIMEOUT_MS = 8000   # 单次连接最长等待时间（含 DHCP 拿 IP）
CONNECT_POLL_MS = 200       # 等待连接时的轮询间隔
RECONNECT_GAP_MS = 500      # 一轮试完还没成功，歇一下再试下一轮

# ==========================================================================
# 扫描缓存
# ==========================================================================
# scan() 会阻塞 1.5~3 秒，网页每次刷新都扫一遍会明显卡顿。
# 这里给扫描结果加个缓存，过期才会真的重扫；用户点「重新扫描」可强制刷新。
SCAN_CACHE_MS = 60000       # 60 秒


class network_model:
    # ★ 扫描缓存是**类属性**，全继承链共享一份。
    #
    # 为什么不能是实例属性：main.py 第 0.5 步会建一个 network_model() 实例
    # 做开机预热扫描（趁热点还没客户端、射频空闲），而真正服务网页的是
    # AMS_WEB() 实例（继承链 AMS_WEB → … → network_model）。如果缓存放在
    # 实例上，预热写进的是「废实例」的缓存，网页那个实例的缓存永远是空的
    # —— 配网页在「热点有客户端」时故意不做全信道扫描、只回缓存，列表就
    # 永远是空的，用户只能手填 SSID。扫描结果描述的是环境、不是某个实例，
    # 所以共享一份才符合语义。
    #
    # 注意：读取走 self._scan_cache（实例上没有就回落到类属性），
    #       写入必须显式写类属性 network_model._scan_cache，否则会在实例上
    #       造出一个遮蔽副本，又退回"各实例各一份"的老问题。
    _scan_cache = []
    _scan_ts = None

    def __init__(self):
        self.ap_ssid = AP_SSID
        self.ap_password = AP_PASSWORD
        self.ap_authmode = AP_AUTHMODE

        self.wlan_ap = network.WLAN(network.AP_IF)   # 热点模式
        self.wlan_sta = network.WLAN(network.STA_IF)  # 连接路由器的模式

        # ★ 关掉 WiFi 省电（modem sleep），幂等、代价极小。
        #   main.py 第 0 步已经在干净堆上设过一次（那才是权威位置，注释在那边），
        #   这里再兜一次是为了"从 REPL 直接跑 main_task()"这种情况 ——
        #   省电没关时传大文件会中途掉关联，表现就是网页永远发不完。
        #   esp_wifi_set_ps() 是整芯片生效的，所以两个口设一次都行。
        for _wlan in (self.wlan_sta, self.wlan_ap):
            try:
                _wlan.config(pm=0)       # 0 = WIFI_PS_NONE
            except Exception:
                pass

        # ★★ 这里**故意不**再把两个口 active(False) 关一遍！★★
        #
        # 以前这里是有的，看着像是"先归零再开始"，实际是个定时炸弹：
        # main.py 已经在堆还没碎的时候把 WiFi 拉起来了（STA 或热点），
        # 而 AMS_WEB → AMS → Bambu_mqtt_cliet → network_model 这条继承链
        # 会在 main_task 里再跑一次本 __init__，于是刚开好的热点/刚连上的
        # 路由器被当场关掉。
        #
        # 关掉之后谁再想开回来（main_task 的 swcith_ap(1)、网页上的"打开热点"
        # 按钮），就得调用 esp_wifi_start() 重新申请一大块**连续**内存 ——
        # 而那时堆已经被应用切碎了（实测空闲还有 64KB，最大连续块只剩 3,584
        # 字节），结果不是报异常，是**直接硬复位**：没有 traceback、没有
        # panic，串口上只看得到 HARD_RESET，板子每 3.3 秒重启一轮。
        #
        # 一句话：WiFi 的"起"归 main.py 管，这里只读取状态，不许关。
        # （关热点的正经入口仍然保留：swcith_ap(0)，配网成功后会用它。）

        # 扫描结果缓存：已提升为类属性（见类定义处的说明），这里**不要**
        # 再写 self._scan_cache = []，否则会在实例上造出遮蔽副本。

    # ======================================================================
    # 热点（AP）
    # ======================================================================
    def swcith_ap(self, status=1):
        """打开 / 关闭配置热点。status=1 打开，0 关闭。

        （方法名保留老拼写，避免调用方改动；新代码也可以用 switch_ap）
        """
        if status:
            if self.wlan_ap.active():
                # ★★ 已经开好了就千万别再 config() 一遍！★★
                #
                # 实测（板子串口，堆已被应用切碎、free=56872）：
                #   ap.active()                -> True（热点确实在跑）
                #   ap.ifconfig()              -> ('192.168.4.1', ...) 正常
                #   ap.active(True) 再来一次   -> OK（早就是 no-op，不重新申请内存）
                #   ap.config(essid=...)       -> RuntimeError 0x0101
                #   ap.config(password=...)    -> RuntimeError 0x0101
                #   ap.config(authmode=...)    -> RuntimeError 0x0101
                # 也就是说 esp_wifi_set_config() 在碎堆上**必定**失败（0x0101 =
                # ESP_ERR_NO_MEM），跟传哪个参数无关。而 main_task 里这一次调用
                # 只是把早已生效的 SSID/密码再下发一遍 —— 纯多余，却能把整个
                # 启动流程打断（异常从 main_task 冒泡出来，板子停在 REPL）。
                #
                # 所以：热点"起"这件事只做一次，谁先起算谁的，后来者只认状态。
                logout("配置热点已在运行: " + self.ap_ssid)
            else:
                self.wlan_ap.active(True)
                try:
                    self.wlan_ap.config(essid=self.ap_ssid,
                                        password=self.ap_password,
                                        authmode=self.ap_authmode)
                except Exception as _cfg_error:
                    # 真没开起来的时候才需要下发参数；万一这时内存不够，也别让
                    # 整条启动链断在这里，用驱动默认参数继续（热点照样能出来）。
                    logout("热点参数下发失败，用默认参数继续: %r" % (_cfg_error,))
                logout("配置热点已打开: " + self.ap_ssid + "  密码: " + self.ap_password)
                logout("手机连上热点后，浏览器打开 http://192.168.4.1 配网")
        else:
            self.wlan_ap.active(False)
            logout("配置热点已关闭")
        return self.wlan_ap.active()

    # 别名：语义更清楚的新名字
    switch_ap = swcith_ap

    def ap_is_on(self):
        return bool(self.wlan_ap.active())

    # ======================================================================
    # 状态查询（网页用）
    # ======================================================================
    def sta_ip(self):
        """STA 的 IP；没连上时返回空字符串"""
        try:
            if not self.wlan_sta.isconnected():
                return ""
            ip = self.wlan_sta.ifconfig()[0]
            return "" if ip == "0.0.0.0" else ip
        except Exception:
            return ""

    def ap_ip(self):
        try:
            if not self.wlan_ap.active():
                return ""
            return self.wlan_ap.ifconfig()[0]
        except Exception:
            return ""

    def current_ssid(self):
        """当前连着的 SSID；没连上返回空字符串"""
        try:
            if not self.wlan_sta.isconnected():
                return ""
            ssid = self.wlan_sta.config("ssid")
            if isinstance(ssid, bytes):
                ssid = ssid.decode("utf-8")
            return ssid or ""
        except Exception:
            return ""

    def status_text(self):
        """把 MicroPython 的 status() 码翻成人话，方便网页直接显示"""
        try:
            code = self.wlan_sta.status()
        except Exception:
            return "未知"
        # ★ 别直接写 network.STAT_CONNECT_FAIL 这类属性名：
        #   实测 v1.23.0 的 ESP32C3 构建里 **没有** STAT_CONNECT_FAIL，
        #   模块级字典求值时直接抛 AttributeError。而这个字典是在
        #   do_connect() 失败打日志那一行才被构造的 —— 于是真正的失败原因
        #   （密码错 / 找不到 AP / 超时）永远看不到，只看到一条莫名其妙的
        #   AttributeError。用 getattr 兜底，缺哪个就少哪条，不挡路。
        def _code(name):
            return getattr(network, name, None)

        table = {}
        for name, text in (("STAT_IDLE", "空闲"),
                           ("STAT_CONNECTING", "连接中"),
                           ("STAT_WRONG_PASSWORD", "密码错误"),
                           ("STAT_NO_AP_FOUND", "找不到该 WiFi"),
                           ("STAT_CONNECT_FAIL", "连接失败"),
                           ("STAT_GOT_IP", "已获取 IP"),
                           ("STAT_BEACON_TIMEOUT", "信号丢失（基站超时）"),
                           ("STAT_HANDSHAKE_TIMEOUT", "握手超时")):
            value = _code(name)
            if value is not None:
                table[value] = text
        return table.get(code, "状态码 %s" % code)

    # ======================================================================
    # 扫描（只在用户主动要求时才真的扫）
    # ======================================================================
    def scan_networks(self, force=False):
        """返回附近的 SSID 列表（去重、排序）。

        force=False 时优先用缓存，避免每次刷新网页都阻塞 2 秒。
        force=True  用于网页上的「重新扫描 WiFi」按钮。
        """
        now = time.ticks_ms()
        if (not force) and self._scan_ts is not None \
                and time.ticks_diff(now, self._scan_ts) < SCAN_CACHE_MS:
            return self._scan_cache

        try:
            self.wlan_sta.active(True)
            raw = self.wlan_sta.scan()
        except OSError as e:
            logout("扫描 WiFi 失败: " + str(e), is_error=True)
            return self._scan_cache

        # 扫描结果按信号强度排序，弱的在后；先出现的就是更强的那个
        found = []
        try:
            ordered = sorted(raw, key=lambda item: item[3], reverse=True)
        except Exception:
            ordered = raw

        for item in ordered:
            try:
                name = item[0].decode("utf-8")
            except Exception:
                name = ""
            if name and name not in found:
                found.append(name)

        # ★ 写类属性，别写实例属性（原因见类定义处的说明）
        network_model._scan_cache = found
        network_model._scan_ts = now
        logout("扫描到 %d 个 WiFi" % len(found))
        return found

    # ======================================================================
    # 上电自动联网
    # ======================================================================
    def auto_connection(self):
        """上电自动联网：**不扫描**，直接用 wifi.dat 里存的账号密码连。

        返回值：
            连上 → WLAN 对象（调用方据此判断"已联网"）
            连不上（没有记录 / 试满 WIFI_BOOT_ATTEMPTS 次都失败）→ None
                  → AMS_WEB.main_task 会打开配置热点
        """
        if self.wlan_sta.isconnected() and self.sta_ip():
            return self.wlan_sta

        try:
            profiles = read_profiles()
        except OSError:
            profiles = {}
        except Exception as e:
            logout("读取 wifi.dat 失败: " + str(e), is_error=True)
            profiles = {}

        if not profiles:
            logout("没有已保存的 WiFi 记录，直接进入配网模式")
            return None

        ssids = list(profiles.keys())
        logout("已保存 %d 个 WiFi: %s" % (len(ssids), ssids))

        self.wlan_sta.active(True)

        attempts = 0
        while attempts < WIFI_BOOT_ATTEMPTS:
            for ssid in ssids:
                if attempts >= WIFI_BOOT_ATTEMPTS:
                    break
                attempts += 1
                logout("自动连接 WiFi (%d/%d): %s"
                       % (attempts, WIFI_BOOT_ATTEMPTS, ssid))
                if self.do_connect(ssid, profiles[ssid]):
                    return self.wlan_sta
            if attempts < WIFI_BOOT_ATTEMPTS:
                time.sleep_ms(RECONNECT_GAP_MS)

        logout("自动连接 WiFi 失败 %d 次，转由上层打开配置热点" % attempts,
               is_error=True)
        return None

    # ======================================================================
    # 连接单个 WiFi
    # ======================================================================
    def do_connect(self, ssid, password, timeout_ms=None):
        """连接指定 WiFi，成功返回 True。

        成功 = 关联上 + **拿到非 0.0.0.0 的 IP**。
        密码错误、找不到 AP 这类硬失败会立刻返回，不会白等满超时。
        """
        timeout_ms = timeout_ms or CONNECT_TIMEOUT_MS

        self.wlan_sta.active(True)
        try:
            self.wlan_sta.disconnect()
        except Exception:
            pass
        time.sleep_ms(100)

        logout("正在连接 WiFi: %s ..." % ssid)
        try:
            self.wlan_sta.connect(ssid, password)
        except Exception as e:
            logout("调用 connect 失败: " + str(e), is_error=True)
            return False

        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            if self.wlan_sta.isconnected():
                ip = self.wlan_sta.ifconfig()[0]
                if ip and ip != "0.0.0.0":
                    logout("WiFi 连接成功: %s  IP=%s" % (ssid, ip))
                    return True
                # 关联上了但还没拿到 IP（DHCP 中），继续等

            # 硬失败就别等了，早点回去开热点
            try:
                code = self.wlan_sta.status()
            except Exception:
                code = None
            if code == network.STAT_WRONG_PASSWORD:
                logout("WiFi 密码错误: " + ssid, is_error=True)
                break
            if code == network.STAT_NO_AP_FOUND:
                logout("找不到 WiFi: " + ssid, is_error=True)
                break

            time.sleep_ms(CONNECT_POLL_MS)

        logout("WiFi 连接失败: %s（%s）" % (ssid, self.status_text()), is_error=True)
        try:
            self.wlan_sta.disconnect()
        except Exception:
            pass
        return False
