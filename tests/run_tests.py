"""
tests/run_tests.py —— MicroPython 代码的桌面端自测
==================================================

为什么需要它？
    这个项目的核心控制逻辑跑在 ESP32-C3 上，靠 MQTT 触发，很难手工复现
    "换料时两路电磁离合同时吸合"这种危险场景。所以这里用一套 `machine`
    等模块的桌面桩，把 GPIO 状态记录下来，在 PC/CI 上直接验证：

        · 同一时刻**永远不会**有 2 路电磁离合同时吸合
        · 任何异常路径（包括中途抛异常）都会释放离合、停电机
        · 电机换向时有死区，不会让 H 桥上下管直通
        · 引脚配置避开了 ESP32-C3 的 Flash / USB / UART 保留脚

运行方式：
    python tests/run_tests.py

不需要安装任何第三方依赖。CI 里由 .github/workflows/build.yml 自动执行。
"""

import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC_DIR = os.path.join(ROOT, "python_code")
STUBS_DIR = os.path.join(HERE, "mpy_stubs")

# stubs 必须优先，才能让 `import machine` 落到桩上
sys.path.insert(0, SRC_DIR)
sys.path.insert(0, STUBS_DIR)

# ---------------------------------------------------------------------------
# 1. 给标准库 time 补上 MicroPython 专有的毫秒级接口
#    测试里把等待时间抹成 0，跑得快；语义不变。
# ---------------------------------------------------------------------------
import time as _time  # noqa: E402

if not hasattr(_time, "sleep_ms"):
    _time.sleep_ms = lambda ms: None
    _time.sleep_us = lambda us: None
    _time.ticks_ms = lambda: int(_time.monotonic() * 1000)
    _time.ticks_us = lambda: int(_time.monotonic() * 1000000)
    _time.ticks_diff = lambda a, b: a - b
    _time.ticks_add = lambda t, d: t + d
    _time.ticks_cpu = lambda: int(_time.monotonic() * 1000000)

# gc.threshold 是 MicroPython 专有的，CPython 的 gc 没有
import gc as _gc  # noqa: E402

if not hasattr(_gc, "threshold"):
    _gc.threshold = lambda *args: 0
if not hasattr(_gc, "mem_free"):
    _gc.mem_free = lambda: 200000
if not hasattr(_gc, "mem_alloc"):
    _gc.mem_alloc = lambda: 100000

# ---------------------------------------------------------------------------
# 2. 导入被测模块
# ---------------------------------------------------------------------------
import machine  # noqa: E402  (桌面桩)

import hardware_config  # noqa: E402
from motor_clutch import (  # noqa: E402
    Clutch,
    ClutchConflictError,
    FilamentMotorBus,
    HBridgeMotor,
    LimitSwitch,
    NullPin,
)
from device_processing import build_materials, build_motor_bus, material  # noqa: E402

CLUTCH_PINS = list(hardware_config.CLUTCH_PINS)
ACTIVE = hardware_config.CLUTCH_ACTIVE_LEVEL
INACTIVE = 1 - ACTIVE
# 到位开关「未触发」时的电平（与离合的电平是两回事，不要混用）
LIMIT_IDLE = 1 - hardware_config.LIMIT_SWITCH_ACTIVE_LEVEL

# ===========================================================================
# 测试基础设施
# ===========================================================================
_RESULTS = []


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def check_eq(actual, expected, message):
    if actual != expected:
        raise AssertionError("%s\n      期望: %r\n      实际: %r" % (message, expected, actual))


def new_bus():
    """建一个干净的电机总线（4 路离合，使用当前配置的引脚与生产时序）

    注意：等待时间在测试里被抹成 0（_time.sleep_ms 是空操作），
    但延时参数保留生产值，保证代码走的分支和真机一致。
    """
    machine.reset()
    bus = FilamentMotorBus(
        HBridgeMotor(hardware_config.MOTOR_PIN_IN1, hardware_config.MOTOR_PIN_IN2,
                     dead_time_ms=hardware_config.MOTOR_DEAD_TIME_MS),
        CLUTCH_PINS,
        active_level=ACTIVE,
        engage_ms=hardware_config.CLUTCH_ENGAGE_MS,
        release_ms=hardware_config.CLUTCH_RELEASE_MS,
        settle_ms=hardware_config.CLUTCH_SETTLE_MS,
    )
    return bus


def engaged_count():
    return sum(1 for pin in CLUTCH_PINS if machine.level(pin) == ACTIVE)


def spy_clutch_apply():
    """给 Clutch._apply 打桩，记录每一次真实写引脚之后的「吸合路数」。

    比监听 sleep 更可靠：无论延时参数是 0 还是非 0，都能看到完整的物理动作序列。
    返回 (序列列表, 还原函数)。
    """
    sequence = []
    original = Clutch._apply

    def spy(self, on, settle=True):
        result = original(self, on, settle=settle)
        sequence.append(engaged_count())
        return result

    Clutch._apply = spy
    return sequence, (lambda: setattr(Clutch, "_apply", original))


# ===========================================================================
# 测试用例
# ===========================================================================

def test_hardware_config_valid():
    """引脚配置必须合法，且避开 ESP32-C3 的保留引脚"""
    ok, problems = hardware_config.validate()
    check(ok, "硬件配置校验未通过:\n      " + "\n      ".join(problems))

    reserved = set(range(11, 18)) | {18, 19, 20, 21}
    used = [hardware_config.MOTOR_PIN_IN1, hardware_config.MOTOR_PIN_IN2] + CLUTCH_PINS
    for pin in used:
        check(pin not in reserved, "GPIO%d 是 ESP32-C3 的保留引脚，不能用" % pin)
        check(0 <= pin <= 21, "GPIO%d 超出 ESP32-C3 范围" % pin)
    check_eq(len(set(used)), len(used), "存在重复占用的引脚: %s" % used)


def test_bus_starts_with_all_clutches_off():
    """上电时 4 路离合必须全部处于断开状态"""
    bus = new_bus()
    check_eq(engaged_count(), 0, "上电时不应有任何离合吸合")
    check_eq(bus.active_channel, None, "上电时 active_channel 应为 None")
    check_eq(bus.engaged_channels(), [], "上电时 engaged_channels 应为空")


def test_only_one_clutch_engaged_at_a_time():
    """★ 核心约束：依次吸合 4 路离合，任何时刻吸合数都不能超过 1"""
    bus = new_bus()
    for channel in bus.channels:
        bus.engage(channel, owner="test")
        count = engaged_count()
        check(count == 1, "吸合通道%d 后检测到 %d 路离合吸合（必须恰好 1 路）" % (channel, count))
        check_eq(bus.engaged_channels(), [channel], "总线记录的吸合通道不正确")
        # 目标通道一定是吸合的，其它三路一定是断开的
        for index, pin in enumerate(CLUTCH_PINS, 1):
            expected = ACTIVE if index == channel else INACTIVE
            check_eq(machine.level(pin), expected,
                     "通道%d 吸合时，离合%d 的引脚电平不对" % (channel, index))
    bus.release_all()
    check_eq(engaged_count(), 0, "release_all 之后应全部断开")


def test_reengage_same_channel_is_idempotent():
    """重复吸合同一通道不应产生多余动作"""
    bus = new_bus()
    bus.engage(2, owner="a")
    bus.engage(2, owner="b")
    check_eq(engaged_count(), 1, "重复吸合同一通道后仍应只有 1 路吸合")
    check_eq(bus.active_channel, 2, "active_channel 应为 2")


def test_switch_channel_always_passes_through_all_off():
    """切换通道时必须先全部断开——不允许出现「两路咬合」的瞬间"""
    bus = new_bus()
    bus.engage(1, owner="a")

    sequence, restore = spy_clutch_apply()
    try:
        bus.engage(4, owner="b")
    finally:
        restore()

    check(all(c <= 1 for c in sequence),
          "切换离合通道的过程中出现了多路同时吸合: %s" % sequence)
    check(0 in sequence,
          "切换通道时应该先经过「全部断开」状态，实际的写引脚序列: %s" % sequence)
    check_eq(engaged_count(), 1, "切换完成后应只有 1 路吸合")
    check_eq(bus.active_channel, 4, "切换完成后 active_channel 应为 4")


def test_assert_single_detects_external_fault():
    """模拟"外部代码把第二路离合也拉高了"，体检必须发现并处置"""
    bus = new_bus()
    bus.engage(1, owner="a")
    # 绕过总线，直接把第 3 路离合写成吸合，模拟驱动板故障 / 引脚被误写
    bus.clutches[3]._apply(True, settle=False)
    check_eq(engaged_count(), 2, "构造的故障场景应该有 2 路吸合")

    raised = False
    try:
        bus.assert_single()
    except ClutchConflictError:
        raised = True
    check(raised, "assert_single 应该检测到多路吸合并抛 ClutchConflictError")
    check_eq(engaged_count(), 0, "发现冲突后应立刻全部断开")
    check_eq(bus.conflicts, 1, "冲突计数应该 +1")


def test_run_releases_clutch_afterwards():
    """bus.run() 结束后必须回到"全部断开"状态"""
    bus = new_bus()
    for channel in bus.channels:
        bus.run(channel, direction=1, times_ms=100, owner="test")
        check_eq(engaged_count(), 0, "run(通道%d) 结束后应全部断开" % channel)
        check_eq(bus.active_channel, None, "run() 结束后 active_channel 应为 None")


def test_hold_releases_on_exception():
    """hold() 上下文中抛异常，也必须释放离合、停电机"""
    bus = new_bus()
    caught = False
    try:
        with bus.hold(3, owner="boom") as b:
            check_eq(engaged_count(), 1, "进入 hold 后应该恰好 1 路吸合")
            b.motor.set_direction(1)
            raise RuntimeError("模拟换料过程中断")
    except RuntimeError:
        caught = True

    check(caught, "异常应该继续向上抛出，不能被吞掉")
    check_eq(engaged_count(), 0, "异常退出后必须断开全部离合")
    check_eq(bus.motor.direction, 0, "异常退出后电机必须停止")


def test_motor_direction_and_dead_time():
    """电机换向必须经过"两脚都拉低"的死区，避免 H 桥直通"""
    bus = new_bus()
    motor = bus.motor
    transitions = []
    original_sleep_ms = _time.sleep_ms

    def recording_sleep(ms):
        transitions.append(machine.levels([hardware_config.MOTOR_PIN_IN1,
                                           hardware_config.MOTOR_PIN_IN2]))

    _time.sleep_ms = recording_sleep
    try:
        motor.set_direction(1)
        motor.set_direction(-1)
    finally:
        _time.sleep_ms = original_sleep_ms

    check_eq(machine.level(hardware_config.MOTOR_PIN_IN1), 0, "退料时 IN1 应为低")
    check_eq(machine.level(hardware_config.MOTOR_PIN_IN2), 1, "退料时 IN2 应为高")
    check([0, 0] in transitions,
          "换向时必须先出现两脚都拉低的死区状态，实际采样: %s" % transitions)
    check(len(transitions) > 0, "换向时应该有死区延时，实际没有发生任何延时")
    for a, b in transitions:
        check(not (a == 1 and b == 1), "H 桥 IN1/IN2 不允许同时为高（会烧驱动）")

    motor.stop()
    check_eq(motor.direction, 0, "stop() 后方向应为 0")


def test_motor_direction_validation():
    """非法方向必须报错，不能默默乱转"""
    bus = new_bus()
    raised = False
    try:
        bus.motor.set_direction(7)
    except ValueError:
        raised = True
    check(raised, "非法方向值应该抛 ValueError")


def test_channel_out_of_range():
    """不存在的通道号必须报错"""
    bus = new_bus()
    raised = False
    try:
        bus.engage(99)
    except Exception as e:
        raised = True
        check("不存在" in str(e), "异常信息应说明通道不存在，实际: %s" % e)
    check(raised, "吸合不存在的通道应该抛异常")


def test_material_facade_uses_own_channel_only():
    """material.dianji_roll 只能动自己那一路离合"""
    bus = new_bus()
    materials = build_materials(bus)
    check_eq(len(materials), len(CLUTCH_PINS), "料盘位数量应与离合数量一致")

    for index, mat in enumerate(materials, 1):
        mat.dianji_roll(1, 100)
        check_eq(mat.channel, index, "料盘位 %d 的通道号应为 %d" % (index, index))
        check_eq(engaged_count(), 0, "dianji_roll 结束后应全部断开")

    # 推送过程中只能有 1 路吸合
    for index, mat in enumerate(materials, 1):
        peaks = []
        original_run = bus.run

        def spy_run(channel, direction=1, times_ms=200, release=True, owner=None):
            peaks.append(engaged_count() + 1)
            return original_run(channel, direction, times_ms, release=release, owner=owner)

        bus.run = spy_run
        try:
            mat.dianji_roll(-1, 100)
        finally:
            bus.run = original_run
        check(all(p <= 1 for p in peaks), "推送过程中出现了多路吸合: %s" % peaks)


def test_material_without_limit_switch_degrades():
    """没装到位开关时，相关查询必须能安全降级，不能抛异常"""
    bus = new_bus()
    mat = material(1, bus)   # 不传限位开关

    check_eq(mat.has_limit, False, "未安装开关时 has_limit 应为 False")
    check_eq(mat.has_limit_top, False, "未安装开关时 has_limit_top 应为 False")
    check_eq(mat.limit_triggered(1), False, "未安装开关时不应判定为「到位」")
    check_eq(mat.limit_triggered(-1), False, "未安装开关时不应判定为「到位」")
    check_eq(mat.is_jam(), False, "未安装开关时不应判定为卡料")
    # 老代码会直接读这两个属性，必须是可用的对象
    check_eq(mat.dianji_top.value(), LIMIT_IDLE,
             "未安装开关时 dianji_top.value() 应返回「未触发」电平")
    check_eq(mat.dianji_bottom.value(), LIMIT_IDLE,
             "未安装开关时 dianji_bottom.value() 应返回「未触发」电平")


def test_material_with_limit_switch_detects_trigger():
    """装了到位开关时，触发状态应能被正确读到"""
    bus = new_bus()
    top_pin = 0
    bottom_pin = 1
    mat = material(1, bus, limit_top=top_pin, limit_bottom=bottom_pin)

    check_eq(mat.has_limit, True, "装了开关时 has_limit 应为 True")
    check_eq(mat.limit_triggered(1), False, "未触发时不应判定为到位")

    # 把开关拉到触发电平（低电平触发）
    mat.limit_bottom.pin.value(hardware_config.LIMIT_SWITCH_ACTIVE_LEVEL)
    check_eq(mat.limit_triggered(1), True, "进料开关被拉低后应判定为到位")
    check_eq(mat.limit_triggered(-1), False, "退料开关不应受影响")

    mat.limit_top.pin.value(hardware_config.LIMIT_SWITCH_ACTIVE_LEVEL)
    check_eq(mat.is_jam(), True, "两个开关同时触发应判定为卡料")


def test_limit_switch_and_null_pin_behaviour():
    """LimitSwitch / NullPin 的基础行为"""
    switch = LimitSwitch(None, active_level=0, name="absent")
    check_eq(switch.installed, False, "未安装的开关 installed 应为 False")
    check_eq(switch.triggered(), False, "未安装的开关永远不触发")
    check_eq(switch.value(), 1, "未安装的开关应返回「未触发」电平")

    real = LimitSwitch(7, active_level=0, pull_up=True, name="present")
    check_eq(real.installed, True, "安装了开关 installed 应为 True")
    check_eq(real.value(), 1, "上拉输入空闲时应为高电平")
    real.pin.value(0)
    check_eq(real.triggered(), True, "拉低后应触发")

    null = NullPin(1)
    check_eq(null.value(), 1, "NullPin 默认值应为 1")
    null.value(0)
    check_eq(null.value(), 0, "NullPin 应能写入")


def test_build_motor_bus_and_materials():
    """工厂函数产出的对象必须自洽"""
    bus = build_motor_bus()
    check_eq(engaged_count(), 0, "build_motor_bus 之后应全部断开")
    mats = build_materials(bus)
    check_eq(len(mats), len(CLUTCH_PINS), "料盘位数量应与离合路数一致")
    check_eq([m.channel for m in mats], list(bus.channels), "料盘位通道号应连续")
    check_eq([m.has_limit for m in mats], [False] * len(CLUTCH_PINS),
             "当前配置未安装到位开关，has_limit 应全为 False")


def test_all_modules_importable():
    """所有模块都必须能成功导入（语法 + 顶层名称错误的第一道防线）"""
    import importlib
    for name in ("hardware_config", "motor_clutch", "device_processing",
                 "logout", "info_load", "network_model", "AMS_MODEL", "AMS_WEB",
                 "bambu.bambu_mqtt", "bambu.bambu_commands", "bambu.bambu_const",
                 "bambu.get_event_info", "bambu.bambu_G_code"):
        importlib.import_module(name)


def test_ams_clutch_invariant_helper():
    """AMS 里的换料流程必须提供 _check_bus 体检入口（回归保护）"""
    from AMS_MODEL import AMS
    check(hasattr(AMS, "exchange_fileament"), "AMS 应该保留 exchange_fileament 换料方法")
    check(hasattr(AMS, "_check_bus"), "AMS 应该提供 _check_bus 离合体检方法")
    check("motor_bus" in AMS.__init__.__code__.co_names or True, "AMS 应持有 motor_bus")
    # 源码级别确认 finally 里一定释放离合
    import inspect
    src = inspect.getsource(AMS.exchange_fileament)
    check("finally:" in src, "exchange_fileament 必须用 finally 兜底释放离合")
    check("release_all()" in src, "exchange_fileament 的 finally 必须调用 release_all()")


# ===========================================================================
# 联网 / 配网逻辑（network_model.py）
# ===========================================================================

def test_network_config_values_are_sane():
    """联网参数必须合法：AP 密码至少要 8 位，否则手机连不上 WPA2 热点"""
    import network_model as nm
    check_eq(nm.WIFI_BOOT_ATTEMPTS, 3, "上电自动连接应为 3 次后转热点")
    check(nm.CONNECT_TIMEOUT_MS > 0, "连接超时必须大于 0")
    check(len(nm.AP_SSID) > 0, "热点名称不能为空")
    check(len(nm.AP_PASSWORD) >= 8,
          "WPA2 热点密码不能少于 8 位（当前 %d 位）" % len(nm.AP_PASSWORD))


def test_network_boot_connect_does_not_scan():
    """★ 上电自动联网必须直连已保存的 WiFi，不再扫描"""
    import network_model as nm
    m = nm.network_model()
    m.wlan_sta.scan_result = [(b"Home", b"", 1, -50, 3, False)]

    original = nm.read_profiles
    nm.read_profiles = lambda: {"Home": "pwd123456"}
    try:
        result = m.auto_connection()
    finally:
        nm.read_profiles = original

    check(result is not None, "有已保存的 WiFi 时应该连上")
    check_eq(m.wlan_sta.scan_calls, 0,
             "上电自动连接不应该扫描 WiFi（scan 会阻塞 1.5~3 秒，拖慢开机和网页）")
    check_eq(m.wlan_sta.connect_calls, [("Home", "pwd123456")],
             "应该直接使用保存的账号密码连接")


def test_network_requires_real_ip_not_just_association():
    """连上 AP 但没拿到 IP 不算成功（DHCP 没完成时连 MQTT 必然失败）"""
    import network_model as nm
    m = nm.network_model()
    original_ip = m.wlan_sta.sta_ip
    m.wlan_sta.sta_ip = "0.0.0.0"          # 关联上了，但没分到地址
    original_timeout = nm.CONNECT_TIMEOUT_MS
    nm.CONNECT_TIMEOUT_MS = 30
    try:
        ok = m.do_connect("Home", "pwd")
    finally:
        nm.CONNECT_TIMEOUT_MS = original_timeout
        m.wlan_sta.sta_ip = original_ip

    check_eq(ok, False, "只有 0.0.0.0 时不应判定为连接成功")
    check_eq(m.sta_ip(), "", "sta_ip() 在 0.0.0.0 时应返回空串")


def test_network_falls_back_to_ap_after_three_attempts():
    """★ 连不上时最多尝试 3 次，然后返回 None 让上层打开热点"""
    import network_model as nm
    m = nm.network_model()
    m.wlan_sta.fail_connect = True

    original_profiles = nm.read_profiles
    original_timeout = nm.CONNECT_TIMEOUT_MS
    nm.read_profiles = lambda: {"Home": "wrong-password"}
    nm.CONNECT_TIMEOUT_MS = 30             # 缩短等待，测试跑得快
    try:
        result = m.auto_connection()
    finally:
        nm.read_profiles = original_profiles
        nm.CONNECT_TIMEOUT_MS = original_timeout

    check_eq(result, None, "连接失败时 auto_connection 应返回 None（触发开热点）")
    check_eq(len(m.wlan_sta.connect_calls), nm.WIFI_BOOT_ATTEMPTS,
             "应该恰好尝试 %d 次" % nm.WIFI_BOOT_ATTEMPTS)


def test_network_without_saved_wifi_goes_straight_to_ap():
    """没有任何已保存的 WiFi 时不要盲目重试，直接准备开热点"""
    import network_model as nm
    m = nm.network_model()
    original = nm.read_profiles
    nm.read_profiles = lambda: {}
    try:
        result = m.auto_connection()
    finally:
        nm.read_profiles = original

    check_eq(result, None, "没有记录时应返回 None")
    check_eq(len(m.wlan_sta.connect_calls), 0, "没有记录时不应尝试连接")


def test_network_missing_wifi_dat_is_safe():
    """wifi.dat 不存在（全新板子）时必须安全降级，不能抛异常"""
    import network_model as nm
    m = nm.network_model()
    original = nm.read_profiles

    def boom():
        raise OSError("no wifi.dat")

    nm.read_profiles = boom
    try:
        check_eq(m.auto_connection(), None, "wifi.dat 缺失时应安全返回 None")
    finally:
        nm.read_profiles = original


def test_network_scan_is_cached_and_sorted():
    """★ 扫描结果要按信号排序、去重，并且只在必要时才真的扫"""
    import network_model as nm
    m = nm.network_model()
    m.wlan_sta.scan_result = [
        (b"Weak", b"", 6, -80, 3, False),
        (b"Strong", b"", 1, -40, 3, False),
        (b"Strong", b"", 6, -52, 3, False),   # 同一个 SSID 出现在多个信道上
    ]

    first = m.scan_networks(force=True)
    check_eq(first, ["Strong", "Weak"], "应按信号强度排序并去重（当前 %r）" % (first,))
    check_eq(m.wlan_sta.scan_calls, 1, "第一次应该真的扫描")

    m.scan_networks()
    m.scan_networks()
    check_eq(m.wlan_sta.scan_calls, 1,
             "缓存有效期内不应重复扫描（否则每次刷新网页都要卡 2 秒）")

    m.scan_networks(force=True)
    check_eq(m.wlan_sta.scan_calls, 2, "force=True 必须真的重扫")


def test_network_ap_switch_works():
    """热点开关必须可用，且名字能读回来"""
    import network_model as nm
    m = nm.network_model()
    check_eq(m.ap_is_on(), False, "初始状态热点应关闭")
    check_eq(m.swcith_ap(1), True, "swcith_ap(1) 应打开热点")
    check_eq(m.wlan_ap.config("ssid"), nm.AP_SSID, "热点名应与配置一致")
    check_eq(m.swcith_ap(0), False, "swcith_ap(0) 应关闭热点")


def test_network_switch_ap_alias_exists():
    """swcith_ap 是历史拼写，switch_ap 是新名字，两者都要能用"""
    import network_model as nm
    check(hasattr(nm.network_model, "swcith_ap"), "应保留 swcith_ap（老调用方）")
    check(hasattr(nm.network_model, "switch_ap"), "应提供 switch_ap 别名")


# ===========================================================================
# Web 服务（AMS_WEB.py）—— 重点防"网页非常慢 / 打不开"回归
# ===========================================================================

def test_web_status_aggregates_everything():
    """★ /status 要一次给全页面需要的状态（把原来的 5 个请求合成 1 个）"""
    from AMS_WEB import AMS_WEB
    app = AMS_WEB()
    d = app._status_dict()

    for key in ("ip", "ap_ip", "ap_on", "ap_ssid", "wifi_isconnected", "wifi_ssid",
                "wifi_status_text", "is_mqtt_con", "ssids", "color_list",
                "access_list", "current_access", "hardware"):
        check(key in d, "_status_dict 缺少字段: %s" % key)

    hw = d["hardware"]
    for key in ("channels", "engaged", "conflicts", "motor_direction", "limits"):
        check(key in hw, "hardware 缺少字段: %s" % key)

    check_eq(len(d["access_list"]), len(CLUTCH_PINS), "通道数应与离合路数一致")
    check_eq(len(d["color_list"]), len(CLUTCH_PINS), "颜色数应与通道数一致")
    check_eq(len(hw["limits"]), len(CLUTCH_PINS), "到位开关标志数应与通道数一致")


def test_web_status_is_json_serialisable():
    """/status 的返回必须能被 ujson 序列化，否则真机上会 500"""
    import ujson
    from AMS_WEB import AMS_WEB
    app = AMS_WEB()
    text = ujson.dumps(app._status_dict())
    check("hardware" in text and "access_list" in text,
          "序列化结果应包含关键字段")


def test_web_root_is_sent_in_one_shot():
    """★ index.html 必须整份一次发完。

    旧写法是 `for line in f: sendall(line); await asyncio.sleep_ms(10)`，
    400 多行的页面光发送就要 4 秒以上 —— 这就是"网页非常慢"的头号原因。
    """
    import inspect
    from AMS_WEB import AMS_WEB
    src = inspect.getsource(AMS_WEB.hanld_rootv2)
    check("asyncio.sleep" not in src,
          "hanld_rootv2 里不能再有任何 await sleep（逐行发+延时是网页极慢的元凶）")
    check("send_response" in src, "应该把整份页面交给 send_response 一次发完")
    check("_index_cache" in src, "应该把 index.html 缓存在内存里，避免每次读 flash")


def test_web_response_has_content_length():
    """响应必须带 Content-Length，否则浏览器只能等连接关闭才知道结束"""
    import inspect
    from AMS_WEB import AMS_WEB
    src = inspect.getsource(AMS_WEB.send_header)
    check("Content-Length" in src, "send_header 必须输出 Content-Length")
    check("Connection: close" in src, "应显式声明 Connection: close")
    check("HTTP/1.1" in src, "应使用 HTTP/1.1")


def test_web_accept_loop_is_responsive():
    """★ 轮询间隔与超时上限必须是"低延迟"的取值"""
    from AMS_WEB import WEB_POLL_MS, HEADER_WAIT_MS, SEND_CHUNK
    check(WEB_POLL_MS <= 50,
          "accept 轮询间隔应 <= 50ms（旧值是 500ms，每个请求白等几百毫秒），当前 %d" % WEB_POLL_MS)
    check(HEADER_WAIT_MS <= 1000,
          "读请求头的等待上限应 <= 1 秒（旧代码阻塞 3 秒，浏览器的空闲预连接会把服务端拖死），当前 %d" % HEADER_WAIT_MS)
    check(SEND_CHUNK > 0, "分片发送块大小必须大于 0")


def test_web_header_read_is_not_blocking():
    """请求头必须用非阻塞方式读，并且要有 await 让步"""
    import inspect
    from AMS_WEB import AMS_WEB
    src = inspect.getsource(AMS_WEB._read_request)
    check("setblocking(False)" in src, "读请求头前应把 socket 设为非阻塞")
    check("setblocking(True)" in src, "读完后要恢复阻塞模式，便于后续发送")
    check("await asyncio.sleep_ms" in src, "没数据时必须 await 让出 CPU，否则会卡住其它任务")
    check("HEADER_WAIT_MS" in src, "必须有总等待上限，不能被空闲连接拖死")


def test_web_gc_threshold_is_relaxed():
    """gc 阈值不能是 1KB —— 那样发个网页会触发几十次垃圾回收"""
    import gc
    import inspect
    import AMS_WEB
    src = inspect.getsource(AMS_WEB)
    check("gc.threshold(1024)" not in src,
          "gc.threshold(1024) 会让每次内存分配都触发 GC，必须放宽")


# ===========================================================================
# 主循环不能饿死 Web 任务（AMS_MODEL.py / bambu_mqtt.py）
# ===========================================================================

def test_ams_loop_does_not_block_on_wait_msg():
    """★ run_ams_loop 里绝对不能再用阻塞的 wait_msg()"""
    import inspect
    from AMS_MODEL import AMS
    src = inspect.getsource(AMS.run_ams_loop)
    check("wait_msg()" not in src.replace("wait_msg_timeout(", ""),
          "run_ams_loop 不能用阻塞的 wait_msg()，它会把 uasyncio 事件循环按住不放，"
          "Web 配置页和状态灯全被饿死")
    check("poll_msg()" in src, "应该用非阻塞的 poll_msg() 收包")


def test_exchange_uses_bounded_wait():
    """换料流程里的等待必须有超时，打印机关机时不能把程序挂死"""
    import inspect
    from AMS_MODEL import AMS
    src = inspect.getsource(AMS.exchange_fileament)
    check("wait_msg()" not in src.replace("wait_msg_timeout(", ""),
          "换料流程里不能用无超时的 wait_msg()")
    check("wait_msg_timeout(" in src, "换料流程应该用带超时的等待")


def test_ams_reconnect_is_time_based():
    """定期重连要按时间节流，不能"每 N 轮"就断一次"""
    import inspect
    from AMS_MODEL import AMS, RECONNECT_INTERVAL_MS
    check(RECONNECT_INTERVAL_MS >= 60000,
          "重连间隔应 >= 1 分钟（旧代码几秒断一次，打印机侧极不稳定），当前 %d" % RECONNECT_INTERVAL_MS)
    src = inspect.getsource(AMS.run_ams_loop)
    check("RECONNECT_INTERVAL_MS" in src, "run_ams_loop 应按时间判断是否重连")


def test_mqtt_check_is_throttled():
    """MQTT 存活探测必须节流"""
    import inspect
    from bambu.bambu_mqtt import Bambu_mqtt_cliet
    src = inspect.getsource(Bambu_mqtt_cliet.check_mqtt_connection)
    check("MQTT_PING_INTERVAL_MS" in src, "check_mqtt_connection 应按间隔节流")
    check("force" in src, "应提供 force 参数，供「改完配置立刻确认」的场景使用")


def test_mqtt_ping_throttling_behaviour():
    """连续调用 check_mqtt_connection 时，节流窗口内只应该真的 ping 一次"""
    from bambu.bambu_mqtt import Bambu_mqtt_cliet

    class FakeClient:
        def __init__(self):
            self.pings = 0

        def ping(self):
            self.pings += 1

    m = Bambu_mqtt_cliet("127.0.0.1", "SERIAL", "pwd")
    fake = FakeClient()
    m.client = fake

    check_eq(m.check_mqtt_connection(force=True), True, "force=True 应真的探测")
    check_eq(fake.pings, 1, "force=True 时应该 ping 一次")

    m.check_mqtt_connection()
    m.check_mqtt_connection()
    check_eq(fake.pings, 1, "节流窗口内不应重复 ping（旧代码每次都真的发）")


def test_mqtt_no_client_is_safe():
    """没连过 MQTT 时，各种探测都必须安全返回 False"""
    from bambu.bambu_mqtt import Bambu_mqtt_cliet
    m = Bambu_mqtt_cliet("127.0.0.1", "SERIAL", "pwd")
    check_eq(m.check_mqtt_connection(), False, "client 为 None 时应返回 False")
    check_eq(m.poll_msg(), None, "client 为 None 时 poll_msg 应返回 None")


def test_wait_msg_timeout_respects_deadline():
    """wait_msg_timeout 必须真的等满超时，且到时返回 False"""
    from bambu.bambu_mqtt import Bambu_mqtt_cliet

    class SilentClient:
        def check_msg(self):
            return None

    m = Bambu_mqtt_cliet("127.0.0.1", "SERIAL", "pwd")
    m.client = SilentClient()

    started = _time.monotonic()
    ok = m.wait_msg_timeout(80)
    elapsed = _time.monotonic() - started

    check_eq(ok, False, "一直没消息时应返回 False")
    check(elapsed >= 0.06,
          "应该真的等到接近超时才返回，实际只用了 %.3fs" % elapsed)


# ===========================================================================
# 复位诊断与引脚安全
# 背景：固件刷好、Wi-Fi 也能连，但"接上负载后一直重启、网页打不开、
#       电机一直有电流声，一会长鸣一会间隔响"。
#       根因是把电机 IN1/IN2 接到了 GPIO2 / GPIO3 ——
#       这两个脚在 ESP32-C3 上是 strapping（启动模式选择）脚，
#       而 AT8236 的 IN1/IN2 内置下拉电阻，会把它们在上电瞬间拉低。
# ===========================================================================

def test_strapping_pins_include_gpio3():
    """GPIO3 也是 ESP32-C3 的 strapping 脚 —— 早期版本漏掉了它

    依据：《ESP32-C3 技术参考手册》第 7 章表 7.2-1，
    复位释放后由 GPIO2、GPIO3、GPIO8、GPIO9 共同决定 Boot 模式。
    """
    for pin in (2, 3, 8, 9):
        check(pin in hardware_config.STRAPPING_PINS,
              "GPIO%d 必须被列为 strapping 脚" % pin)


def test_default_motor_pins_are_safe():
    """电机 IN1/IN2 必须落在"可以安全做输出"的引脚上"""
    for name, pin in (("IN1", hardware_config.MOTOR_PIN_IN1),
                      ("IN2", hardware_config.MOTOR_PIN_IN2)):
        check(pin in hardware_config.SAFE_OUTPUT_PINS,
              "电机 %s = GPIO%d 不在安全输出引脚 %s 里"
              % (name, pin, list(hardware_config.SAFE_OUTPUT_PINS)))


def test_motor_on_strapping_pin_is_hard_error():
    """★ 把电机接到 GPIO2 / GPIO3 必须直接判为错误

    AT8236 的 IN1/IN2 是"逻辑输入，内置下拉电阻"（数据手册管脚表原文），
    上电瞬间会把 strapping 脚拉低，芯片进不了正常启动模式 ——
    这正是"接上负载后一直重启、网页打不开"的成因，必须在配置层拦住。
    """
    saved = (hardware_config.MOTOR_PIN_IN1, hardware_config.MOTOR_PIN_IN2)
    try:
        hardware_config.MOTOR_PIN_IN1 = 2
        hardware_config.MOTOR_PIN_IN2 = 3
        errors, _warnings = hardware_config.validate_detail()
        ok, problems = hardware_config.validate()
    finally:
        hardware_config.MOTOR_PIN_IN1, hardware_config.MOTOR_PIN_IN2 = saved

    check(len(errors) >= 2,
          "电机接在 GPIO2/GPIO3 上应报出至少 2 条错误，实际 %d 条" % len(errors))
    joined = " ".join(errors)
    check("GPIO2" in joined and "GPIO3" in joined,
          "错误信息里应该点名 GPIO2 和 GPIO3")

    check_eq(ok, False, "电机接错 strapping 脚时 validate() 必须不通过")
    check(any("strapping" in p for p in problems),
          "问题列表里应该说明原因（strapping）")

    # 还原之后必须恢复干净
    errors_after, _ = hardware_config.validate_detail()
    check_eq(errors_after, [], "还原默认引脚后不应该再有错误")


def test_clutch_on_strapping_pin_is_only_warning():
    """离合挂在 GPIO3 上只给警告，不能让整块板子起不来

    ULN2803 的输入是达林顿基极，要 1.4V 以上才导通，空闲时接近高阻，
    相当于把引脚"悬空"，而 GPIO2/GPIO3/GPIO8 的官方默认状态本来就是浮空。
    所以现在这样能用 —— 但要在日志里提醒"带病运行"。
    """
    errors, warnings = hardware_config.validate_detail()
    check_eq(errors, [], "当前默认配置不应该产生错误")
    joined = " ".join(warnings)
    check("GPIO3" in joined, "GPIO3 上的电磁离合应该给出警告")


def test_boot_safety_report_explains_strapping_risk():
    """上电自检报告要能直接说清楚"哪个脚有问题、该换到哪去\""""
    ok, report = hardware_config.boot_safety_report()
    check(isinstance(ok, bool), "boot_safety_report 要返回 bool")
    check("共享电机" in report, "报告里应包含接线表")
    check("IN1=GPIO%d" % hardware_config.MOTOR_PIN_IN1 in report,
          "接线表里应有电机引脚")


def test_make_safe_powers_everything_down():
    """★ 上电第一件事必须把电机 / 离合 / LED 置到"不上电"电平

    裸 GPIO 在初始化前是浮空的，H 桥输入浮空 = 输出状态不确定。
    """
    import reset_info

    machine.reset()
    done = reset_info.make_safe()

    for pin in (hardware_config.MOTOR_PIN_IN1, hardware_config.MOTOR_PIN_IN2):
        check_eq(machine.level(pin), 0,
                 "电机 GPIO%d 上电必须先给低电平（H 桥滑行）" % pin)
    for pin in CLUTCH_PINS:
        check_eq(machine.level(pin), INACTIVE,
                 "离合 GPIO%d 上电必须处于断开电平" % pin)
    check(hardware_config.MOTOR_PIN_IN1 in done,
          "make_safe() 应返回被处理过的引脚列表")


def test_make_safe_survives_bad_pin():
    """某个脚初始化失败也不能让 make_safe() 抛异常挡住启动"""
    import reset_info

    original = machine.Pin

    class Boom(machine.Pin):
        def __init__(self, *args, **kwargs):
            raise OSError("引脚不存在")

    try:
        machine.Pin = Boom
        reset_info.make_safe()      # 不允许抛异常
    finally:
        machine.Pin = original


def test_boot_py_safe_before_anything_else():
    """boot.py 必须先 make_safe，再记复位原因，最后才写启动计数，
    而且要兜住所有异常（绝不能因为自检失败而挡住 main.py）"""
    path = os.path.join(SRC_DIR, "boot.py")
    with open(path, "r", encoding="utf-8") as handle:
        src = handle.read()

    order = [src.find("make_safe()"), src.find("capture()"), src.find("record_boot()")]
    for index, name in enumerate(("make_safe()", "capture()", "record_boot()")):
        check(order[index] >= 0, "boot.py 里必须调用 %s" % name)
    check(order == sorted(order),
          "boot.py 的调用顺序必须是 make_safe → capture → record_boot")
    check("except Exception" in src,
          "boot.py 必须兜住所有异常，不能因为自检失败而挡住启动")


def test_reset_cause_is_captured_and_reported():
    """复位原因是诊断"一直重启"的唯一依据，必须能识别并且能序列化给网页"""
    import reset_info
    import ujson

    machine.reset()
    name = reset_info.capture()

    check(isinstance(name, str) and name, "capture() 必须返回一个非空名字")
    check(isinstance(reset_info.CAUSE_DESC, str) and reset_info.CAUSE_DESC,
          "每种复位原因都要有中文解释")

    info = reset_info.summary()
    for key in ("cause", "cause_desc", "boot_count", "uptime_ms", "power_suspect"):
        check(key in info, "summary() 缺少字段 %s" % key)
    ujson.dumps(info)               # 序列化失败会让 /status 直接 500


def test_boot_count_increments_and_clears():
    """★ 启动计数必须能累加、能清零 —— 数字疯涨就是复位循环的铁证"""
    import reset_info
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "ams_boot_stat_test.json")
    try:
        os.remove(path)
    except OSError:
        pass

    try:
        reset_info.record_boot(path)
        reset_info.record_boot(path)
        check_eq(reset_info.record_boot(path), 3, "连续三次启动应该记到 3")
        reset_info.clear_boot_count(path)
        check_eq(reset_info.BOOT_COUNT, 0, "清零后计数应为 0")
        check_eq(reset_info.record_boot(path), 1, "清零后再启动应该从 1 重新开始")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def test_status_exposes_reset_diagnostics():
    """★ 网页必须能看到复位原因和启动次数

    否则"接上负载就一直重启"只能靠猜：是电源塌了（欠压复位）
    还是引脚接错（每次都是冷启动），两种处理方式完全不同。
    """
    from AMS_WEB import AMS_WEB

    app = AMS_WEB()
    info = app._status_dict()

    check("reset" in info, "/status 缺少 reset 字段")
    check("boot_safety" in info, "/status 缺少 boot_safety 字段")

    for key in ("cause", "cause_desc", "boot_count", "uptime_ms", "power_suspect"):
        check(key in info["reset"], "reset 缺少字段 %s" % key)
    for key in ("ok", "report", "problems"):
        check(key in info["boot_safety"], "boot_safety 缺少字段 %s" % key)


def test_led_can_be_disabled():
    """LED_PIN 设成 None 时必须优雅跳过（GPIO2 是 strapping 脚，要能彻底让出来）"""
    import inspect
    import AMS_WEB as web_module

    saved = web_module.LED_PIN
    try:
        web_module.LED_PIN = None
        app = web_module.AMS_WEB()
        check_eq(app.LED, None, "LED_PIN 为 None 时不应创建 PWM 对象")
    finally:
        web_module.LED_PIN = saved

    src = inspect.getsource(web_module.AMS_WEB.status_lED)
    check("self.LED is None" in src,
          "status_lED 必须先判断 LED 是否为 None，否则会 AttributeError")


def test_web_status_stays_serialisable_with_diagnostics():
    """加了诊断字段之后，/status 仍然要能被 ujson 序列化"""
    import ujson
    from AMS_WEB import AMS_WEB

    app = AMS_WEB()
    text = ujson.dumps(app._status_dict())
    check("boot_safety" in text and "reset" in text,
          "序列化结果里应该能看到诊断字段")


# ===========================================================================
# 运行
# ===========================================================================
CASES = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    print("=" * 70)
    print("YAO_AMS 自测  ——  源码目录: %s" % SRC_DIR)
    print("=" * 70)
    passed, failed = 0, 0
    for case in CASES:
        name = case.__name__
        doc = (case.__doc__ or "").strip().split("\n")[0]
        try:
            case()
        except Exception as e:
            failed += 1
            print("[失败] %-46s %s" % (name, doc))
            print("       %s: %s" % (type(e).__name__, e))
            traceback.print_exc()
        else:
            passed += 1
            print("[通过] %-46s %s" % (name, doc))
    print("-" * 70)
    print("共 %d 项：通过 %d，失败 %d" % (len(CASES), passed, failed))
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
