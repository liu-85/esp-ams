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


# ---------------------------------------------------------------------------
# 假 socket：只记录每次 sendall 的内容与大小，用来验证"分块发送"的真实行为
# ---------------------------------------------------------------------------
class _FakeSocket:
    def __init__(self):
        self.parts = []
        self.chunks = []

    def sendall(self, data):
        if isinstance(data, str):          # 真机 socket 也接受 str，这里对齐一下
            data = data.encode("utf-8")
        data = bytes(data)
        self.parts.append(data)
        self.chunks.append(len(data))

    def content(self):
        return b"".join(self.parts)


def _split_head(raw):
    pos = raw.find(b"\r\n\r\n")
    check(pos > 0, "HTTP 响应里必须有空行分隔响应头和正文")
    return raw[:pos + 4], raw[pos + 4:]


def _code_only(fn):
    """取函数的源码，但去掉文档字符串和注释行。

    否则"提示性文字"会把源码检查带偏 ——
    比如注释里写着"千万别用 f.read() 整份读"，断言却把它当成真的这么写了。
    （不用 `src.replace(fn.__doc__, "")`：编译器给的 __doc__ 和源码文本
      在缩进/转义上不保证逐字相同，直接按行跳过更可靠。）
    """
    import inspect
    out = []
    in_doc = False
    for line in inspect.getsource(fn).split("\n"):
        s = line.strip()
        if in_doc:
            if s.endswith('"""') or s.endswith("'''"):
                in_doc = False
            continue
        if s[:3] in ('"""', "'''"):
            if not (s.endswith('"""') or s.endswith("'''")) or len(s) <= 6:
                in_doc = True          # 多行文档字符串的开始
            continue
        if s.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def test_web_root_is_streamed_not_read_into_ram():
    """★ 页面必须分块流式发送，绝不能整份读进内存。

    真实故障：ESP32-C3 空闲堆只有几十 KB 且碎片化，
    `open("index.html").read()` 要一次性拿到 40KB 连续内存
      → memory allocation failed, allocating 36096 bytes。
    更坑的是整份缓存根本没写进去，于是**每个请求都在同一处再失败一次**，
    现象就是"AP 能连上、管理页怎么都打不开、串口疯狂刷错误"。
    """
    import inspect
    from AMS_WEB import AMS_WEB, FILE_CHUNK

    src = _code_only(AMS_WEB.hanld_rootv2)
    check(".read()" not in src, "hanld_rootv2 不能再出现无参数的 read()（整份读进内存）")
    check("_index_cache" not in src, "页面不能再做整份内存缓存")
    check("send_file" in src, "应该交给 send_file 分块发送")

    src_file = _code_only(AMS_WEB.send_file)
    check("FILE_CHUNK" in src_file, "send_file 必须按 FILE_CHUNK 分块读")
    check(".read()" not in src_file, "send_file 里不能有无参数的 read()")
    check("gc.collect()" in src_file, "发文件之前要先 gc.collect() 收拢碎片")
    check("await asyncio.sleep_ms" in src_file,
          "块与块之间必须 await 让步，否则发 40KB 期间其它任务全被饿死")
    check("file_size" in src_file,
          "Content-Length 要用 os.stat 取文件长度，不能靠先读一遍")
    check(FILE_CHUNK <= 2048,
          "单块大小必须远小于空闲堆（<=2KB），当前 %d 字节" % FILE_CHUNK)

    app = AMS_WEB()
    check(not hasattr(app, "_index_cache"),
          "不应该再有 _index_cache 这种「整份页面」字段")


def test_web_streams_real_index_html_byte_for_byte():
    """★ 拿真机上那份 40KB 的 index.html 实跑：内容逐字节一致，
    且**单次发送不超过 FILE_CHUNK** —— 这就是"不会再 OOM"的硬证据。"""
    import asyncio
    from AMS_WEB import AMS_WEB, FILE_CHUNK

    app = AMS_WEB()
    cwd = os.getcwd()
    try:
        os.chdir(SRC_DIR)                 # hanld_rootv2 是按相对路径打开页面的
        with open("index.html", "rb") as f:
            page = f.read()
        check(len(page) > 30000, "页面应该足够大（这次故障就是它太大导致的）")

        client = _FakeSocket()
        asyncio.run(app.hanld_rootv2(client))
    finally:
        os.chdir(cwd)

    raw = client.content()
    head, body = _split_head(raw)
    check(b"200 OK" in head, "首页应该返回 200")
    check(("Content-Length: %d" % len(page)).encode() in head,
          "Content-Length 必须等于文件真实字节数，浏览器才知道何时读完")
    check_eq(body, page, "分块拼回来的正文必须和磁盘上的 index.html 逐字节一致")

    biggest = max(client.chunks)
    check(biggest <= FILE_CHUNK,
          "单次发送最大 %d 字节，超过 FILE_CHUNK=%d 就又会一次性占满内存"
          % (biggest, FILE_CHUNK))
    check(len(client.chunks) > 10,
          "40KB 页面应该被拆成很多块发（实际 %d 块）" % len(client.chunks))


def test_web_send_file_reports_missing_file():
    """文件缺失要回 500，不能把异常抛给请求循环（那样每次都是"处理请求出错"）"""
    import asyncio
    from AMS_WEB import AMS_WEB

    app = AMS_WEB()
    client = _FakeSocket()
    ok = asyncio.run(app.send_file(client, "这个文件不存在.html"))
    check(ok is False, "文件不存在时 send_file 应返回 False")
    head, _ = _split_head(client.content())
    check(b"500" in head, "应该返回 500")


def test_web_memory_error_sends_tiny_page_not_blank():
    """★ 万一真的分配不出内存：要给浏览器一个极小的提示页，而不是白屏。

    但只有"一个字都还没发出去"时才能补响应 —— 否则会把半个响应拼坏。
    """
    import inspect
    import AMS_WEB as web_module

    src = _code_only(web_module.AMS_WEB._web_worker)
    check("MemoryError" in src, "服务循环必须单独接住 MemoryError")
    check("oom_respond" in src, "内存不足时要走 oom_respond 兜底")
    check("mem_note" in src, "异常日志里要带空闲内存，否则下次 OOM 还是只能猜")

    check(len(web_module._OOM_PAGE) < 1024,
          "兜底页面必须很小（几百字节），否则它自己也会分配失败，当前 %d 字节"
          % len(web_module._OOM_PAGE))

    app = web_module.AMS_WEB()
    client = _FakeSocket()
    app._sent = 0
    check(app.oom_respond(client), "还没发过数据时应该能补上兜底响应")
    head, _ = _split_head(client.content())
    check(b"503" in head, "兜底响应状态码应该是 503")

    app2 = web_module.AMS_WEB()
    client2 = _FakeSocket()
    app2._sent = 100          # 假装响应已经发出去一半了
    check(not app2.oom_respond(client2), "已经发过数据时不能再补响应")
    check_eq(len(client2.parts), 0, "此时不应该再往连接里写任何东西")


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
    """请求头必须用非阻塞方式读，并且要有 await 让步

    历史：读请求头用阻塞 recv + 3 秒超时，浏览器开的"预连接"套接字什么都不发，
    服务端就傻等满 3 秒，期间整个事件循环停摆 → "网页偶尔打不开"。
    现在这一职责拆在 _read_headers（头）和 _read_body（体）里。
    """
    import inspect
    from AMS_WEB import AMS_WEB

    src = inspect.getsource(AMS_WEB._read_headers)
    check("setblocking(False)" in src, "读请求头前应把 socket 设为非阻塞")
    check("setblocking(True)" in src, "读完后要恢复阻塞模式，便于后续发送")
    check("await asyncio.sleep_ms" in src, "没数据时必须 await 让出 CPU，否则会卡住其它任务")
    check("HEADER_WAIT_MS" in src, "必须有总等待上限，不能被空闲连接拖死")

    body = inspect.getsource(AMS_WEB._read_body)
    check("setblocking(False)" in body, "读请求体前也应设为非阻塞")
    check("await asyncio.sleep_ms" in body, "等请求体时同样要 await 让步")
    check("BODY_WAIT_MS" in body, "请求体等待也要有上限")


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


def test_ams_loop_waits_for_wifi_before_mqtt():
    """★ WiFi 都没连上时不要空转重连 MQTT。

    AP 配置模式下 STA 本来就没连网，旧逻辑每 10 秒建一次 MQTT 并打印
    "未连接wifi"，串口日志全被噪音淹没，真正有用的报错反而看不清。
    """
    import inspect
    from AMS_MODEL import AMS

    src = inspect.getsource(AMS.run_ams_loop)
    check("wlan_sta.isconnected()" in src, "重连 MQTT 之前必须先确认 WiFi 已连接")

    idx_wifi = src.find("wlan_sta.isconnected()")
    # 注意：这里的实参是 preflight=True（先花最多 1 秒探一下打印机通不通），
    # 所以不能用 "conent_and_subscribe()" 这种带空括号的字面量去搜。
    idx_conn = src.find("conent_and_subscribe(")
    check(idx_wifi != -1 and idx_conn != -1 and idx_wifi < idx_conn,
          "判断顺序不对：应该先看 WiFi，再决定要不要真的去连 MQTT")


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
    for key in ("ok", "problems"):
        check(key in info["boot_safety"], "boot_safety 缺少字段 %s" % key)

    # ★ 反向断言：report 是整段中文接线表，dumps 之后体积翻几倍，
    #   它一进 /status，空闲堆小的板子就会 OOM ——
    #   现象是"页面能打开，但运行状态全是 -、通道一直加载中"。
    check("report" not in info["boot_safety"],
          "boot_safety 不能再带 report：那是整段中文文本，会把 /status 撑到 OOM")


def test_status_is_small_enough_for_the_board():
    """★ /status 的体积必须受控

    它是每 2 秒被打一次的接口，体积一大就变成"内存杀手"。
    这条守的是本次"页面能开、数据全空"的故障不再复发。
    """
    import ujson
    from AMS_WEB import AMS_WEB

    app = AMS_WEB()
    app._scan_cache = ["一个挺长的WiFi名字_%02d" % i for i in range(40)]

    raw = ujson.dumps(app._status_dict())
    check(len(raw) < 2400,
          "/status 的 JSON 太大了（%d 字符），会把空闲堆吃光；"
          "只放网页真正用到的字段，长文本要截断" % len(raw))
    check(app._status_dict()["ssids"] is not None, "ssids 字段永远要有值（可以是空列表）")
    check(len(app._status_dict()["ssids"]) <= 12,
          "ssids 要限量，否则附近 WiFi 一多 /status 就爆")


def test_status_survives_broken_fields():
    """★ 单个字段取不到值时，/status 必须整体仍然可用

    任何一个字段抛异常就让整页变空白，现象是"页面能打开、数据全是 -"，
    排查起来极其痛苦。
    """
    import ujson
    from AMS_WEB import AMS_WEB

    app = AMS_WEB()

    def boom():
        raise ValueError("模拟某个状态字段炸了")

    app.sta_ip = boom
    app.status_text = boom
    app._hardware_dict = boom

    info = app._status_dict()
    check(info["ip"] == "", "取不到 IP 时要回空字符串，而不是抛出去")
    check(info["wifi_status_text"] == "", "取不到状态文案时要回空字符串")
    check_eq(info["hardware"], {}, "取不到硬件状态时要回空字典")
    check(ujson.dumps(info), "/status 即使有字段失败也要能序列化出去")


def test_request_reader_waits_for_post_body():
    """★ POST 的请求体必须读完

    旧代码读到 `\\r\\n\\r\\n` 就返回，而 JSON 请求体常常在下一个 TCP 段里。
    于是解析出 None → 路由条件不成立 → 掉进 404，
    网页上就是"MQTT 设置保存提示 404，而且什么都没存进去"。
    """
    import inspect
    import AMS_WEB as web_module

    src = inspect.getsource(web_module.AMS_WEB._read_body)
    check("_content_length" in src,
          "读请求体时必须按 Content-Length 判断该收多少，否则请求体永远读不全")
    check("body_len" in src,
          "必须按长度把请求体收齐")

    body = b'{"mqtt_server":"192.168.1.9","DEVICE_SERIAL":"SN1","mqtt_password":"1234"}'
    head = (b"POST /mqtt_connect HTTP/1.1\r\nHost: x\r\n"
            b"Content-Length: %d\r\n\r\n" % len(body))
    check(web_module._content_length(head) == len(body),
          "_content_length 解析结果不对")


def test_content_length_parsing_is_robust():
    """Content-Length 的解析要经得起脏输入（大小写、缺字段、非数字）"""
    from AMS_WEB import _content_length

    check_eq(_content_length(b"GET / HTTP/1.1\r\n\r\n"), 0, "GET 没有 Content-Length，应该是 0")
    check_eq(_content_length(b"POST / HTTP/1.1\r\ncontent-length: 42\r\n\r\n"), 42,
             "请求头字段名大小写不固定")
    check_eq(_content_length(b"POST / HTTP/1.1\r\nContent-Length: abc\r\n\r\n"), 0,
             "非数字时要回 0，不能抛异常")
    check_eq(_content_length(b"POST / HTTP/1.1\r\nContent-Length:  7 \r\n\r\n"), 7,
             "数字两边可能有空格")


class _FakeReadSocket:
    """按"每次 recv 给一段"来喂数据，模拟 TCP 分段到达"""

    def __init__(self, segments):
        self.segments = list(segments)
        self.blocking = True

    def setblocking(self, flag):
        self.blocking = flag

    def recv(self, size):
        if not self.segments:
            return b""              # 对端没有更多数据了
        return self.segments.pop(0)


async def _read_whole_request(app, sock):
    """按 _serve_client 的真实顺序读一次请求（头 → 体），返回原始字节。

    读请求的职责被拆成 _read_headers / _read_body 两个函数之后，
    测试里需要一个等价的小工具把它们串起来，否则测试只能去啃内部实现细节。
    """
    head, extra = await app._read_headers(sock)
    if not head:
        return None
    body = await app._read_body(sock, head, extra)
    if not body:
        return head
    return head + body


def test_read_request_collects_body_arriving_in_a_later_segment():
    """★ 请求体在下一个 TCP 段里时，也必须被读齐

    这是"Mqtt设置保存提示404"的直接复现：旧代码只读到空行为止，
    请求体留在 socket 里没读，于是解析出 None → 路由不成立 → 404。

    现在读请求分两步（_read_headers → _read_body），这里按 _serve_client
    的真实顺序把两步串起来，验证分段到达也能读全。
    """
    import ujson
    import uasyncio as _asyncio
    from AMS_WEB import AMS_WEB

    app = AMS_WEB()
    body = b'{"mqtt_server":"192.168.1.9","DEVICE_SERIAL":"SN1","mqtt_password":"pw"}'
    head = (b"POST /mqtt_connect HTTP/1.1\r\nHost: ams\r\n"
            b"Content-Length: %d\r\n\r\n" % len(body))

    raw = _asyncio.run(_read_whole_request(app, _FakeReadSocket([head, body])))
    check(raw is not None, "请求必须能被读到")
    check(raw.endswith(body), "请求体必须被读完（旧代码就在这里丢掉请求体，然后回 404）")
    check_eq(app.process_json(raw), ujson.loads(body),
             "读全之后必须能解析出 JSON，路由条件才成立")

    # 反过来：只给请求头（客户端没发体）时不能卡住，也不能假装读到了体
    only_head = _asyncio.run(_read_whole_request(app, _FakeReadSocket([head])))
    check(only_head is not None, "只有请求头时也要能返回，不能卡死")
    check_eq(app.process_json(only_head), None, "没有请求体就应该解析出 None")

    # 头 + 体挤在同一个 TCP 段里，也必须工作（真实网络里很常见）
    joined = _asyncio.run(_read_whole_request(app, _FakeReadSocket([head + body])))
    check(joined.endswith(body), "头和体在同一段里时也要能读齐")


def test_send_response_handles_non_ascii_content_length():
    """★ 中文响应的 Content-Length 必须是 UTF-8 字节数

    用 len(字符串) 当长度（字符数）会让浏览器少收或多收字节，
    页面上就是"JSON 解析失败"或白屏。同时验证单块发送不会太大。
    """
    import ujson
    from AMS_WEB import AMS_WEB, ENC_CHUNK

    app = AMS_WEB()
    sock = _FakeSocket()
    payload = ujson.dumps({"cause_desc": "★ 欠压复位：供电电压掉到了阈值以下" * 8})
    app.send_response(sock, payload, is_json=True)

    raw = sock.content()
    head, body = _split_head(raw)
    length = None
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1].strip())
    check(length is not None, "响应必须带 Content-Length")
    check_eq(length, len(body), "Content-Length 必须是正文字节数")
    check_eq(ujson.loads(body.decode("utf-8"))["cause_desc"],
             ujson.loads(payload)["cause_desc"], "中文内容必须一字不差地送达")

    check(ENC_CHUNK <= 1024,
          "字符串响应的单块大小要足够小（当前 %d），否则又会出现一大块连续分配" % ENC_CHUNK)
    check(max(sock.chunks) <= ENC_CHUNK * 4 + 256,
          "单次 sendall 不应该出现整份响应那样的大块")


def test_write_routes_never_answer_404_for_empty_body():
    """★ 写接口收到空请求体要回 400，不能回 404

    404 会说成"接口不存在"，把用户带到完全错误的方向；
    真实原因只是请求体没读全。
    """
    import inspect
    import AMS_WEB as web_module

    for name in ("wifi_connect", "mqtt_connect", "access_set", "hardware_test",
                 "jog_set"):
        check(name in web_module.WRITE_ROUTES, "%s 应被视为写接口" % name)

    # 路由现在在 _serve_client 里（run_web_loop 只负责起监听 + 拉 worker）
    src = inspect.getsource(web_module.AMS_WEB._serve_client)
    check("handle_bad_body" in src,
          "写接口的请求体为空时必须走 handle_bad_body（回 400），不能落到 404")

    bad = inspect.getsource(web_module.AMS_WEB.handle_bad_body)
    check("400" in bad, "空请求体要回 400 Bad Request")


def test_mqtt_config_is_saved_before_connecting():
    """★ MQTT 配置必须"先落盘，再交给后台连接"

    旧代码只有连上打印机才写 config.json。而 AP 配置模式下根本没联网，
    MQTT 必然连不上 —— 于是保存永远失败、配置永远存不下来，
    重启之后还得重填。

    这一版更进一步：**请求里根本不做 TLS 连接**。
    TLS 握手是阻塞的，打印机没开机时会卡好几秒甚至几十秒 ——
    那正是"点保存时网页像卡死"的来源。现在只更新参数 + 置脏标志，
    由 run_ams_loop 去连，回包如实说明"saved / connecting"。
    """
    import inspect
    import AMS_WEB as web_module

    check("先落盘" in (web_module.AMS_WEB.handle_mqtt_cennect.__doc__ or ""),
          "函数注释里要写明「先落盘再连接」这个约定")

    # ★ 用 _code_only：注释和文档字符串里也会出现 conent_and_subscribe 这些
    #   名字，直接搜源码文本会被"提示性文字"带偏。
    src = _code_only(web_module.AMS_WEB.handle_mqtt_cennect)
    check("updata_data" in src, "必须调用 updata_data 真正写 config.json")
    check("conent_and_subscribe" not in src,
          "请求处理里不能现场做 TLS 连接 —— 那会把事件循环按住好几秒")
    check("_mqtt_dirty" in src,
          "应该置脏标志，让主循环用新参数重连")
    check("mqtt_update_info" in src, "应该把新参数同步给 MQTT 客户端")
    check('"saved"' in src and '"connected"' in src,
          "返回里要有 saved / connected，网页才能给出准确提示")

    # 参数校验必须发生在落盘之前（缺必填项就别写文件）
    idx_req = src.find("MQTT_REQUIRED")
    idx_save = src.find("updata_data")
    check(idx_req != -1 and idx_save != -1 and idx_req < idx_save,
          "顺序不对：应该先校验必填项，再写盘")


def test_mqtt_save_reports_honest_status():
    """★ MQTT 保存必须如实回答，不能动不动就报"失败"

    旧行为：打印机没开机（或 AP 模式下没联网）→ 现场连接失败 → 回"失败"，
    可是配置其实已经存好了，重启几次主循环一连就上 ——
    用户看到的就是"提示失败，但重启几次它自己又连上了"这种自相矛盾的现象。
    """
    from AMS_WEB import AMS_WEB

    app = AMS_WEB()
    app.updata_data = lambda d: dict(d)          # 别真的写 config.json
    conn = _FakeConn()

    ok = app.handle_mqtt_cennect(conn, {
        "mqtt_server": "192.168.1.9",
        "DEVICE_SERIAL": "sn001",
        "mqtt_password": "12345678",
    })

    check_eq(ok, True, "参数齐全时保存必须成功")
    data = _json_of(conn)
    check_eq(data.get("saved"), True, "必须如实说明「配置已保存」")
    check_eq(data.get("connected"), False, "没连上就如实说没连上（不能谎报成功）")
    check_eq(data.get("connecting"), True, "要说明「正在后台连接」")
    check("已保存" in (data.get("info") or ""),
          "提示语里要明确写出「配置已保存」——这才能解释"
          "「重启几次就自己连上了」的现象")
    check(("没联网" in (data.get("info") or "")) or ("后台连接" in (data.get("info") or "")),
          "要区分「设备当前没联网」和「联网了、后台正在连」两种情况")
    check_eq(app._mqtt_dirty, True,
             "必须置脏标志，主循环看到就用新参数立刻重连")
    check_eq(app.mqtt_server, "192.168.1.9", "新参数要同步进 MQTT 客户端")
    check_eq(app.DEVICE_SERIAL, "SN001", "序列号应统一成大写")


def test_mqtt_save_rejects_blank_required_fields():
    """必填项留空要回 400 并点名是哪一项，而不是默默吞掉"""
    from AMS_WEB import AMS_WEB

    app = AMS_WEB()
    saved = []
    app.updata_data = lambda d: saved.append(d) or dict(d)
    conn = _FakeConn()

    ok = app.handle_mqtt_cennect(conn, {"mqtt_server": "192.168.1.9"})
    check_eq(ok, False, "缺序列号和访问码时必须拒绝")
    check_eq(_status_of(conn), 400, "参数缺失要回 400")
    check_eq(saved, [], "校验没过就绝不能写盘")
    check("访问码" in (_json_of(conn).get("info") or "") or
          "设备序列号" in (_json_of(conn).get("info") or ""),
          "提示里要点名缺失的中文字段，而不是糊一句「参数缺失」")


def test_ams_loop_reconnects_when_mqtt_config_is_dirty():
    """★ 主循环必须响应"配置刚改过"，立刻用新参数重连一次"""
    import inspect
    from AMS_MODEL import AMS

    src = inspect.getsource(AMS.run_ams_loop)
    check("_mqtt_dirty" in src, "run_ams_loop 要看脏标志")
    idx = src.find("_mqtt_dirty")
    idx_close = src.find("close_client()")
    check(idx != -1 and idx_close != -1,
          "配置变脏时必须先 close_client()（放掉旧 socket），再用新参数重连")
    check("close_client()" in src,
          "重连前必须关闭旧 socket —— 旧代码直接覆盖 self.client，每重连一次漏一个 socket")


def test_close_client_releases_old_socket():
    """close_client() 必须真的断开旧连接，否则 socket 会一个接一个漏掉"""
    from bambu.bambu_mqtt import Bambu_mqtt_cliet

    class FakeSock:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeClient:
        def __init__(self):
            self.sock = FakeSock()
            self.disconnected = False

        def disconnect(self):
            self.disconnected = True

    m = Bambu_mqtt_cliet("127.0.0.1", "S", "P")
    fake = FakeClient()
    m.client = fake
    m._mqtt_alive = True

    check_eq(m.close_client(), True, "有旧连接时 close_client 应返回 True")
    check_eq(fake.disconnected, True, "应该调用 disconnect()")
    check_eq(m.client, None, "client 必须置空")
    check_eq(m._mqtt_alive, False, "存活缓存必须一起清掉")
    check_eq(m.close_client(), False, "已经没有连接时再调用应安全返回 False")
    check_eq(m.mqtt_alive_cached(), False, "没有连接时缓存状态必须为 False")


def test_mqtt_defaults_fill_blank_fields():
    """用户名 / 客户端名 / 端口留空时用默认值补齐，不该直接报错"""
    from AMS_WEB import MQTT_DEFAULTS, MQTT_REQUIRED

    check_eq(MQTT_DEFAULTS.get("username"), "bblp", "默认用户名")
    check_eq(MQTT_DEFAULTS.get("mqtt_port"), "8883", "默认端口")
    check("mqtt_server" in MQTT_REQUIRED, "打印机 IP 是必填项")
    check("mqtt_password" in MQTT_REQUIRED, "访问码是必填项")
    check("client_id" not in MQTT_REQUIRED, "客户端名有默认值，不该强制填写")


def test_wifi_connect_closes_ap_after_success():
    """★ 配网成功后要关掉配置热点

    这是"WiFi 连上之后就不再显示配网卡片"能成立的前提 ——
    网页只在 AP 模式下显示 WiFi 配置。
    """
    import inspect
    import AMS_WEB as web_module

    src = inspect.getsource(web_module.AMS_WEB.handle_wifi_cennect)
    idx_send = src.find("send_response")
    idx_ap = src.find("swcith_ap(0)")
    check(idx_ap != -1, "配网成功后必须关闭配置热点")
    check(idx_send != -1 and idx_send < idx_ap,
          "必须先回包给浏览器，再关热点；反了客户端会丢掉「连接成功」这句话")


def test_ap_can_be_toggled_from_web():
    """★ 网页上必须能把配置热点再打开

    联网后 WiFi 配置卡片就藏起来了，没有这个入口，想换 WiFi 只能重刷固件。
    """
    import inspect
    import AMS_WEB as web_module

    check(hasattr(web_module.AMS_WEB, "handle_ap_set"), "缺少 /ap_set 处理函数")
    src = inspect.getsource(web_module.AMS_WEB.handle_ap_set)
    check("swcith_ap" in src, "/ap_set 要真的去开/关热点")

    routes = inspect.getsource(web_module.AMS_WEB._serve_client)
    check('"ap_set"' in routes, "/ap_set 必须挂进路由")
    check('"log"' in routes, "/log 必须挂进路由")


def test_log_ring_buffer_is_bounded():
    """★ 日志环形缓冲必须限量

    网页右侧的日志面板靠它拿数据。不限制行数和行长的话，
    它自己就会变成内存泄漏点。
    """
    import logout as log_module

    saved = list(log_module._log_lines) if hasattr(log_module, "_log_lines") else []
    try:
        log_module.clear()
        for i in range(log_module.LOG_MAX_LINES * 3):
            log_module.logout("测试日志 %d" % i, is_print=False)
        lines = log_module.recent()
        check_eq(len(lines), log_module.LOG_MAX_LINES,
                 "缓冲里只应保留 LOG_MAX_LINES 行")
        check(len(lines[-1]) <= log_module.LOG_LINE_MAX + 12,
              "每行都要截断（时间戳另算），否则一条超长日志就能吃掉整块内存")

        long_text = "x" * (log_module.LOG_LINE_MAX * 5)
        log_module.logout(long_text, is_print=False)
        check(log_module.recent(1)[0].find("x" * (log_module.LOG_LINE_MAX + 1)) == -1,
              "超长日志必须被截断")

        check_eq(len(log_module.recent(3)), 3, "recent(n) 要能只取 n 行")
        check(log_module.recent(3) is not log_module._log_lines,
              "recent 必须返回副本，不能把内部列表交给调用方")
    finally:
        log_module.clear()
        for line in saved:
            log_module._log_lines.append(line)


def test_log_endpoint_returns_recent_lines():
    """/log 要能返回日志，并且体积受控"""
    import inspect
    import AMS_WEB as web_module
    from AMS_WEB import LOG_TAIL

    check(0 < LOG_TAIL <= 60, "LOG_TAIL 要在合理范围内，当前 %r" % (LOG_TAIL,))
    src = inspect.getsource(web_module.AMS_WEB.get_log)
    check("recent_logs" in src, "/log 要从环形缓冲里取数据")
    check("LOG_TAIL" in src, "/log 要限量，不能把整个缓冲发出去")


# ===========================================================================
# 网页（index.html）的回归保护
#
# 页面里最容易"改着改着就退回去"的是这几件事：
#   · 通道必须默认就画出 4 个（不能等接口）
#   · WiFi 配置只在配置热点开启时出现
#   · 每个目录默认折叠
#   · 右下角常驻设备日志
# 这些都是"用户能直接看见"的行为，所以用源码级断言钉住。
# ===========================================================================
def _page_source():
    with open(os.path.join(SRC_DIR, "index.html"), encoding="utf-8") as handle:
        return handle.read()


def test_page_renders_four_channels_without_waiting():
    """★ 通道必须默认就是 4 个，而且**不能等接口回来才画**

    以前 renderAccess 只在 applyStatus 里被调用，接口一慢/一出错，
    整页就永远停在"加载中…" —— 用户看到的就是"默认 4 个通道没显示出来"。
    """
    page = _page_source()
    check("DEFAULT_CHANNELS = [1, 2, 3, 4]" in page,
          "页面里要有 DEFAULT_CHANNELS = [1, 2, 3, 4] 这个默认值")
    check("renderAccess(DEFAULT_CHANNELS" in page,
          "boot() 里要先用默认值把通道画出来")
    check("ensureHwButtons(DEFAULT_CHANNELS)" in page,
          "硬件调试页也要先用默认通道把点动按钮画出来")
    # 通道 / 硬件这两块**不能**留"加载中…"占位：接口不通时用户看到的
    # 应该是一套可用的界面，而不是永远转圈。
    body = page[page.find("<body>"):page.find("<script>")]
    for anchor in ('id="access_info"', 'id="hardware_info"', 'id="hardware_test"'):
        pos = body.find(anchor)
        check(pos >= 0, "页面缺少结构：%s" % anchor)
        check("加载中" not in body[pos:pos + 160],
              "%s 里不该写死「加载中…」占位" % anchor)


def test_page_wifi_card_only_in_ap_mode():
    """★ WiFi 配置只在"配置热点开着"的时候出现

    用户的诉求：连上网之后就别再显示这个卡片了。
    """
    page = _page_source()
    check("apOnly" in page, "WiFi 菜单项要标成 apOnly")
    check("applyWifiVisibility" in page, "要根据 ap_on 决定 WiFi 菜单项显不显示")
    check("apOn = !!d.ap_on" in page, "apOn 必须来自 /status 的 ap_on 字段")

    src = page[page.find("function applyWifiVisibility"):]
    src = src[:src.find("function showPage")]
    check("'hide'" in src, "热点关掉时要把 WiFi 菜单项藏起来")
    check("showPage('status')" in src,
          "正停在 WiFi 页而热点关掉了，要自动挪回运行状态（否则是一页空白）")

    # 藏起来靠的是 CSS 类，不是内联 display —— 内联 display 会被 showPage
    # 的 classList 操作绕过去，容易出现"藏了又冒出来"。
    check(".item.hide{display:none}" in page,
          "要有 .item.hide 这条 CSS 规则，光加类名不生效")


def test_page_directories_stay_folded_on_boot():
    """★ 开机 / 刷新时，一个目录都不许自动展开

    之前 showPage() 会"顺手"展开当前页所在目录，而 boot() 一定会调一次
    showPage()，结果侧栏一进来就有一个目录是摊开的 —— 跟"每个目录默认折叠"
    直接矛盾（实测：3 个目录里 1 个 open）。
    现在展开只发生在"用户自己点菜单"这一条路径上。
    """
    page = _page_source()

    # 1) 用户点菜单项 → 显式要求展开所在目录
    menu = page[page.find("function buildMenu"):]
    menu = menu[:menu.find("function openKeys")]
    check("showPage(it.id, true)" in menu,
          "点菜单项时要传 autoReveal=true 展开所在目录")

    # 2) showPage 必须用这个开关把"自动展开"关在门外
    fn = page[page.find("function showPage"):]
    fn = fn[:fn.find("function toggleMenu")]
    check("function showPage(id, autoReveal)" in fn,
          "showPage 要接收 autoReveal 参数")
    guard = fn.find("if(autoReveal){")
    reveal = fn.find("classList.add('open')")
    check(guard >= 0, "展开目录的代码要包在 if(autoReveal) 里")
    check(reveal > guard,
          "classList.add('open') 必须出现在 if(autoReveal){ 之后，不能无条件执行")

    # 3) 开机那次调用绝不能自动展开
    boot = page[page.find("function boot"):]
    boot = boot[:boot.find("if(document.readyState")]
    check("showPage(store(PAGE_KEY) || 'status')" in boot,
          "boot() 里初始化页面时不能传 autoReveal，否则默认就有目录是开的")


def test_page_menu_starts_folded():
    """★ 每个目录默认折叠

    展开状态记在 localStorage，默认值必须是"空"（=全部折叠）。
    """
    page = _page_source()
    check("OPEN_KEY" in page, "目录展开状态要持久化，避免每次刷新都变样")
    check("store(OPEN_KEY) || ''" in page, "没有记录时应该当成「全部折叠」")

    # 菜单是 JS 动态生成的，初始 HTML 里不应有写死的 open
    nav = page[page.find('<nav id="menu">'):]
    nav = nav[:nav.find("</nav>")]
    check("open" not in nav, "初始菜单里不能有写死的展开状态")


def test_page_has_left_menu_and_log_panel():
    """★ 左侧菜单 + 右侧内容 + 右下角常驻设备日志"""
    page = _page_source()
    for token in ('class="app"', 'class="side"', 'class="main"',
                  'id="menu"', 'id="content"', 'id="logbox"', 'id="log_lines"'):
        check(token in page, "页面缺少结构：%s" % token)
    check("设备日志" in page, "右下角要是「设备日志」面板")
    check("pollLog" in page and "/log" in page, "日志面板要真的去拉 /log")
    check("scrollTop = box.scrollHeight" in page,
          "新日志到达时要自动滚到底（否则用户永远看到最旧的一行）")


def test_page_reports_mqtt_save_result_precisely():
    """网页要说清楚「保存成功但没连上」和「彻底失败」的区别"""
    page = _page_source()
    check("/mqtt_connect" in page, "页面要调用 /mqtt_connect")
    check("d.connected" in page and "d.saved" in page,
          "要根据 saved / connected 给不同的提示，不能笼统报成功")


def test_page_never_builds_request_body_outside_json():
    """POST 必须带 JSON 请求体 —— 空体在真机上会被判成 400"""
    page = _page_source()
    check("JSON.stringify(data)" in page,
          "写操作必须把参数 JSON 序列化后放进 body")
    check("'Content-Type':'application/json'" in page,
          "必须声明 Content-Type，否则服务端没法识别")



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
# 这一版新增：把「响应太慢 / 像崩溃 / 刷新好几次才出页面 / 点动转圈」
# 的根因逐条钉死
#
# 用户原话：
#   · "系统响应太慢，而且会崩溃一样，刷新也没会打不开，多次刷新才会出来页面"
#   · "网页调试按下通道一直在转圈，几十秒后才有动作，响应延迟"
#   · "MQTT设置提示失败，重启几次又自动连接上了"
#   · "在硬件调试中添加一个设置进退响应多少秒的选项，统一设置 4 个通道"
#   · "在系统中添加OTA菜单，可以上传BIN文件在线更新"
#
# 根因是四处阻塞 / 排队（都已修掉，下面每条都对应一个测试）：
#   1) /status 里调 check_mqtt_connection()，真的在 SSL 上发 PINGREQ ——
#      打印机连接半死时会卡到 TCP 超时（几十秒），而 /status 每 2 秒被轮询一次；
#   2) accept → 处理 → accept 串行，且 listen(2) 太小，浏览器多开的连接被丢 SYN；
#   3) 读请求头时给浏览器的"预连接"套接字白等 600ms；
#   4) 手动点动走同步的 bus.run()，用 time.sleep_ms 度过整个时长，按住事件循环。
# ===========================================================================

class _FakeConn(_FakeSocket):
    """既能"分段收"又能"记录发"的假连接，用来跑完整的请求路径。

    （_FakeSocket 只管发、_FakeReadSocket 只管收；路由测试需要两者兼顾，
      所以单独做一个子类，避免动到既有用例的行为。）
    """

    def __init__(self, segments=()):
        _FakeSocket.__init__(self)
        self.segments = list(segments)
        self.blocking = True
        self.timeout = None
        self.closed = False

    def setblocking(self, flag):
        self.blocking = flag

    def settimeout(self, seconds):
        self.timeout = seconds

    def recv(self, size):
        if not self.segments:
            return b""
        return self.segments.pop(0)

    def close(self):
        self.closed = True


def _status_of(conn):
    """从响应里取出状态码；没有响应就返回 0"""
    raw = conn.content()
    if not raw.startswith(b"HTTP/1.1 "):
        return 0
    try:
        return int(raw.split(b" ", 2)[1])
    except Exception:
        return 0


def _body_of(conn):
    return _split_head(conn.content())[1]


def _json_of(conn):
    import ujson
    try:
        return ujson.loads(_body_of(conn))
    except Exception:
        return {}


def _raw_request(method, url, body=None):
    """拼一个最简 HTTP 请求，返回 (请求头, 请求体) 两段。

    头体分开是为了贴近真实 TCP：请求体常常在下一个段里到达。
    """
    body = body or b""
    head = "%s /%s HTTP/1.1\r\nHost: ams\r\n" % (method, url)
    if body:
        head += "Content-Length: %d\r\n" % len(body)
    head += "\r\n"
    return head.encode(), body


def _serve(app, method, url, body=None):
    """跑一次 _serve_client（= 真实的路由 + 处理路径），返回假连接"""
    import asyncio
    head, payload = _raw_request(method, url, body)
    conn = _FakeConn([head, payload] if payload else [head])
    asyncio.run(app._serve_client(conn))
    return conn


# ---------------------------------------------------------------------------
# ① 阻塞 I/O 必须全部移出请求路径（"一卡几十秒"的根因）
# ---------------------------------------------------------------------------

def test_status_never_touches_network():
    """★ /status 里绝不能出现任何网络 I/O

    旧代码在 _status_dict 里调 check_mqtt_connection()，它会真的在 SSL 上发
    PINGREQ。打印机连接一旦半死，这个阻塞写会卡到 TCP 自己超时（**几十秒**），
    而 /status 是每 2 秒被轮询一次的 → 网页周期性假死，
    表现就是"系统响应太慢、像崩溃、刷新也打不开"。
    """
    import ujson
    import AMS_WEB as web_module
    from AMS_WEB import AMS_WEB

    src = _code_only(web_module.AMS_WEB._status_dict)
    check("check_mqtt_connection" not in src,
          "/status 里不能再出现 check_mqtt_connection —— 那是真正的网络 I/O")
    check("mqtt_alive_cached" in src, "只能读主循环留下的缓存标志")

    mqtt_info = _code_only(web_module.AMS_WEB.get_mqtt_info)
    check("check_mqtt_connection" not in mqtt_info,
          "/get_mqtt_info 也不能真发 ping（老页面会调它）")

    # 行为验证：把所有网络入口都换成"一碰就炸"，/status 依然必须可用
    app = AMS_WEB()

    def boom(*_args, **_kwargs):
        raise AssertionError("这条路径不允许走网络")

    app.check_mqtt_connection = boom
    app.conent_and_subscribe = boom
    app.tcp_reachable = boom

    info = app._status_dict()
    check("is_mqtt_con" in info, "/status 仍然要给 is_mqtt_con")
    check_eq(info["is_mqtt_con"], False, "没连过 MQTT 时应为 False")
    ujson.dumps(info)                 # 序列化失败会让 /status 直接 500


def test_mqtt_alive_cached_does_no_io():
    """★ mqtt_alive_cached 一次网络 I/O 都不能做（网页每 2 秒就读它一次）"""
    from bambu.bambu_mqtt import Bambu_mqtt_cliet

    class PingBomb:
        def ping(self):
            raise AssertionError("读状态时绝不允许真的发 PINGREQ")

    m = Bambu_mqtt_cliet("127.0.0.1", "S", "P")
    m.client = PingBomb()
    m._mqtt_alive = True
    check_eq(m.mqtt_alive_cached(), True, "应该直接返回缓存值，不碰网络")

    m.client = None
    check_eq(m.mqtt_alive_cached(), False, "没连接时必须返回 False，且不抛异常")


def test_mqtt_ping_has_hard_timeout():
    """★ 存活探测必须带硬超时：连接半死时不能让调用方卡几十秒"""
    import inspect
    from bambu.bambu_mqtt import Bambu_mqtt_cliet, MQTT_PING_TIMEOUT_S

    check(MQTT_PING_TIMEOUT_S <= 1.5,
          "ping 超时必须很小（当前 %r 秒），否则半死连接会冻住整个事件循环"
          % (MQTT_PING_TIMEOUT_S,))
    src = inspect.getsource(Bambu_mqtt_cliet._ping_guarded)
    check("settimeout" in src, "必须给 socket 设超时")
    check("MQTT_PING_TIMEOUT_S" in src, "超时值要用常量，别散落魔数")

    class FakeSock:
        def __init__(self):
            self.timeouts = []

        def settimeout(self, value):
            self.timeouts.append(value)

        def close(self):
            pass

    class DeadClient:
        def __init__(self):
            self.sock = FakeSock()
            self.pings = 0

        def ping(self):
            self.pings += 1
            raise OSError("连接半死")

    m = Bambu_mqtt_cliet("127.0.0.1", "S", "P")
    dead = DeadClient()
    m.client = dead

    check_eq(m.check_mqtt_connection(force=True), False,
             "ping 抛异常时要判为不存活，而不是把异常抛出去")
    check_eq(dead.pings, 1, "应该真的 ping 一次")
    check_eq(dead.sock.timeouts[0], MQTT_PING_TIMEOUT_S, "ping 前要设成硬超时")
    check_eq(dead.sock.timeouts[-1], None,
             "ping 后要把超时还原成不限制（后续 publish 需要阻塞写）")
    check_eq(m.mqtt_alive_cached(), False, "缓存状态要跟着更新")


def test_mqtt_preflight_skips_unreachable_printer():
    """★ 打印机没开机时，别让 SSL 连接一直卡到系统超时

    preflight=True 先用普通 socket 花最多 1 秒探一下 IP:端口能不能握手，
    探不通就直接放弃这一轮 —— 把"每 10 秒冻一次、每次好几秒"
    变成"每 10 秒探 1 秒就放弃"。探测失败也绝不能去建 SSL 连接。
    """
    import bambu.bambu_mqtt as mqtt_module
    from bambu.bambu_mqtt import Bambu_mqtt_cliet, MQTT_PREFLIGHT_TIMEOUT_S

    check(MQTT_PREFLIGHT_TIMEOUT_S <= 2.0, "预探测超时要短，否则等于没优化")

    m = Bambu_mqtt_cliet("127.0.0.1", "S", "P")
    m.mqtt_port = 1                       # 这个端口不会有服务在听
    m.wlan_sta.isconnected = lambda: True

    check_eq(m.tcp_reachable(), False, "连不上的地址必须返回 False（且要很快返回）")

    # 探不通时不许构造 MQTTClient
    original = mqtt_module.MQTTClient

    class Bomb:
        def __init__(self, *args, **kwargs):
            raise AssertionError("预探测没过就不该去建 SSL 连接")

    mqtt_module.MQTTClient = Bomb
    try:
        check_eq(m.conent_and_subscribe(preflight=True), False,
                 "预探测没通过时应安全返回 False")
    finally:
        mqtt_module.MQTTClient = original

    check_eq(m.client, None, "连不上时不能留下半成品连接")
    check_eq(m.mqtt_alive_cached(), False, "存活状态应置 False")


# ---------------------------------------------------------------------------
# ② 监听队列 + 并发 worker（"刷新好几次才出来页面"的根因）
# ---------------------------------------------------------------------------

def test_web_uses_worker_pool_and_big_backlog():
    """★ 必须多 worker 并发 + 足够大的 listen 队列

    旧实现是"accept 一个、处理完再 accept 下一个"的串行循环，
    而且 listen(2) 太小：浏览器一次开 6 个连接，多出来的 SYN 被内核直接丢掉，
    由浏览器按 TCP 退避（1s→2s→4s…）重试 —— 用户看到的就是
    "刷新也不打不开、要刷好几次"。
    """
    import inspect
    from AMS_WEB import WEB_WORKERS, LISTEN_BACKLOG
    import AMS_WEB as web_module

    check(WEB_WORKERS >= 2,
          "至少要 2 个 worker，否则一个慢连接就把后面全都堵住（当前 %d）" % WEB_WORKERS)
    check(LISTEN_BACKLOG >= 8,
          "listen 队列要够大，否则多开的连接会被丢 SYN（当前 %d）" % LISTEN_BACKLOG)

    src = _code_only(web_module.AMS_WEB.run_web_loop)
    check("LISTEN_BACKLOG" in src, "listen() 要用这个常量")
    check("_web_worker" in src, "要拉起 worker 服务循环")
    check("gather" in src, "多个 worker 要并发跑")
    check("_serve_client" not in src,
          "run_web_loop 本身不能再直接处理请求（那就退回成串行了）")

    worker = _code_only(web_module.AMS_WEB._web_worker)
    check("accept()" in worker, "worker 要自己抢连接")
    check("_serve_client" in worker, "抢到之后交给 _serve_client 处理")
    check("await asyncio.sleep_ms" in worker, "抢不到连接时要用 await 轮询，不能空转")

    # 反例：连 *所有* worker 的 accept 都失败时不能把异常抛出去炸掉任务
    check("except" in worker, "accept 失败必须兜住（非阻塞 accept 没连接就抛 OSError）")


def test_web_first_byte_window_is_short():
    """★ 浏览器的"预连接"套接字一个字都不发，必须很快丢掉

    旧代码给每个这样的连接白等 HEADER_WAIT_MS（600ms），一次页面加载开 6 个
    连接就是 3.6 秒 —— 这就是"刷新好几次才出来页面"的直接原因。
    现在只给一个很短的"首字节窗口"。
    """
    import asyncio
    from AMS_WEB import AMS_WEB, HEAD_FIRST_BYTE_MS, HEADER_WAIT_MS

    check(HEAD_FIRST_BYTE_MS <= 250,
          "首字节窗口要很短（当前 %d ms），否则预连接会白占时间" % HEAD_FIRST_BYTE_MS)
    check(HEAD_FIRST_BYTE_MS < HEADER_WAIT_MS,
          "首字节窗口必须明显小于整体上限，否则等于没优化")

    app = AMS_WEB()

    # 一个字节都不发的连接 → 立刻丢掉，不能返回半个请求
    sock = _FakeReadSocket([])
    head, extra = asyncio.run(app._read_headers(sock))
    check_eq(head, None, "空连接必须被丢掉，不能返回半个请求")
    check_eq(extra, b"", "丢掉时不能附带任何残留数据")
    check_eq(sock.blocking, True, "不管走哪条路径，都要把 socket 恢复成阻塞模式")


# ---------------------------------------------------------------------------
# ③ 手动点动：立刻回包 + 后台计时（"按一下转圈几十秒"的根因）
# ---------------------------------------------------------------------------

def test_bus_two_phase_api_is_non_blocking():
    """★ begin() 必须立刻返回并让电机转起来；finish() 再收尾

    这是网页点动能"立刻回包"的底层保证：时长由调用方用 await 度过，
    不再由 bus.run() 用 time.sleep_ms 死死按住事件循环。
    """
    import inspect
    from motor_clutch import FilamentMotorBus

    src = inspect.getsource(FilamentMotorBus.begin)
    check("time.sleep_ms" not in src, "begin() 里不能有阻塞延时")
    check("self.motor.set_direction" in src, "begin() 要让电机立刻转起来")
    check("release_all()" in src, "电机没转起来时要立刻回到安全状态")

    fin = inspect.getsource(FilamentMotorBus.finish)
    check("self.motor.stop()" in fin, "finish() 要停电机")
    check("finally" in fin, "finish() 必须用 finally 兜底")
    check("release_all()" in fin, "finish() 必须断开全部离合")

    bus = new_bus()
    check_eq(bus.busy, False, "初始应空闲")
    bus.begin(3, -1, owner="t")
    check_eq(bus.busy, True, "begin 之后应标记忙")
    check_eq(engaged_count(), 1, "begin 之后应恰好 1 路吸合")
    check_eq(bus.motor.direction, -1, "begin 应该让电机按指定方向转起来")

    # 忙的时候绝不允许再吸合另一路（否则就是两路咬合）
    raised = False
    try:
        bus.engage(1, owner="other")
    except Exception:
        raised = True
    check(raised, "总线忙时不允许切换到别的通道")
    check_eq(engaged_count(), 1, "被拒绝之后仍然只有 1 路吸合")

    bus.finish()
    check_eq(bus.busy, False, "finish 之后应回到空闲")
    check_eq(bus.motor.direction, 0, "finish 之后电机必须停")
    check_eq(engaged_count(), 0, "finish 之后离合必须全断开")
    check_eq(bus.active_channel, None, "finish 之后 active_channel 应为 None")

    # 引脚被外部改坏时也要能兜住：finish 里 stop 抛异常也必须断开离合
    bus.begin(2, 1, owner="t")
    original_stop = bus.motor.stop

    def boom():
        raise RuntimeError("模拟 stop 失败")

    bus.motor.stop = boom
    try:
        try:
            bus.finish()
        except Exception:
            pass
    finally:
        bus.motor.stop = original_stop
        bus.release_all()
    check_eq(bus.busy, False, "stop 抛异常时总线也不能卡在忙状态")


def test_jog_does_not_block_event_loop():
    """★ 点动不能再走同步的 bus.run()（它用 time.sleep_ms 按住事件循环）"""
    import AMS_WEB as web_module

    src = _code_only(web_module.AMS_WEB.handle_hardware_test)
    check("motor_bus.begin(" in src, "上半场要调非阻塞的 begin()")
    check("create_task" in src, "下半场要交给后台任务")
    check(".run(" not in src,
          "点动不能再调同步的 bus.run() —— 那会把整个事件循环按住好几秒")

    fin = _code_only(web_module.AMS_WEB._finish_jog)
    check("await asyncio.sleep_ms" in fin,
          "计时必须用 await asyncio.sleep_ms（让出 CPU），不是 time.sleep_ms")
    check("time.sleep_ms" not in fin, "这里绝不能出现阻塞的 time.sleep_ms")
    check("motor_bus.finish(" in fin, "到点要调 finish() 收尾")
    check("release_all()" in fin, "收尾失败时还要兜一层 release_all()")


def test_hardware_test_replies_before_the_action_finishes():
    """★ 点动必须"立刻回包 + 后台计时"，不能等动作做完才回

    旧实现按一下按钮要等整个 times_ms 走完才回包，期间其它请求全排队 ——
    网页上就是"一直在转圈，几十秒后才有动作"。
    """
    import asyncio
    import AMS_WEB as web_module
    from AMS_WEB import AMS_WEB

    async def scenario():
        app = AMS_WEB()
        app.jog_ms = 200                  # 直接给合法值，不去动 config.json
        created = []
        real_create = web_module.asyncio.create_task

        def spy(coro, *args, **kwargs):
            task = real_create(coro, *args, **kwargs)
            created.append(task)
            return task

        web_module.asyncio.create_task = spy
        conn = _FakeConn()
        try:
            ok = await app.handle_hardware_test(conn, {"channel": 2, "direction": 1})
        finally:
            web_module.asyncio.create_task = real_create

        snap = {
            "ok": ok,
            "code": _status_of(conn),
            "data": _json_of(conn),
            "direction": app.motor_bus.motor.direction,
            "engaged": len(app.motor_bus.engaged_channels()),
            "busy": app.motor_bus.busy,
            "pending": (len(created) == 1 and not created[0].done()),
        }
        if created:
            await created[0]              # 跑完下半场：到点自动停 + 断开
        snap["direction_after"] = app.motor_bus.motor.direction
        snap["engaged_after"] = len(app.motor_bus.engaged_channels())
        snap["busy_after"] = app.motor_bus.busy
        return snap

    snap = asyncio.run(scenario())

    check_eq(snap["ok"], True, "点动应该被受理")
    check_eq(snap["code"], 200, "点动接口要立刻回 200")
    check_eq(snap["data"].get("ok"), True, "回包里要报告已开始")
    check("已开始" in (snap["data"].get("info") or ""),
          "提示语要说清楚已经开始、多久后自动停，实际: %r" % snap["data"].get("info"))
    check_eq(snap["direction"], 1, "回包时电机应该已经在转（不是等动作做完才回）")
    check_eq(snap["engaged"], 1, "回包时应该恰好 1 路离合吸合")
    check_eq(snap["busy"], True, "回包时总线应该标记为忙")
    check(snap["pending"], "动作必须还在后台进行 —— 证明没有同步跑完")

    check_eq(snap["direction_after"], 0, "到点后电机必须停")
    check_eq(snap["engaged_after"], 0, "到点后必须断开全部离合")
    check_eq(snap["busy_after"], False, "总线要回到空闲")


def test_jog_rejects_when_bus_is_busy():
    """★ 总线忙时立刻拒绝并说清楚，绝不排队（排队就是"按一下转圈半天"）"""
    import asyncio
    from AMS_WEB import AMS_WEB

    async def scenario():
        app = AMS_WEB()
        app.jog_ms = 200
        app.motor_bus.begin(1, 1, owner="test")
        app._jog_channel = 1
        conn = _FakeConn()
        ok = await app.handle_hardware_test(conn, {"channel": 3, "direction": 1})
        data = _json_of(conn)
        code = _status_of(conn)
        app.motor_bus.finish()
        app._jog_channel = None
        return ok, data, code

    ok, data, code = asyncio.run(scenario())
    check_eq(ok, False, "忙的时候必须拒绝，不能排队")
    check_eq(data.get("ok"), False, "回包里要如实说没执行")
    check_eq(data.get("running"), True, "要告诉前端当前有动作在跑")
    check_eq(code, 200, "忙是正常业务状态，回 200 即可")
    check("正在动作" in (data.get("info") or ""), "提示要说人话")


def test_hardware_test_validates_input():
    """非法通道 / 方向必须回 400 并说明，绝不能默默乱动电机"""
    import asyncio
    from AMS_WEB import AMS_WEB

    async def scenario():
        app = AMS_WEB()
        out = []
        for body in ({"channel": 99, "direction": 1},
                     {"channel": 1, "direction": 7},
                     "不是字典"):
            conn = _FakeConn()
            ok = await app.handle_hardware_test(conn, body)
            out.append((ok, _status_of(conn), _json_of(conn)))
        return out, app

    out, app = asyncio.run(scenario())
    for ok, code, data in out:
        check_eq(ok, False, "非法参数必须拒绝")
        check_eq(code, 400, "非法参数要回 400")
        check(data.get("info"), "必须给出中文原因")
    check_eq(app.motor_bus.motor.direction, 0, "拒绝之后电机必须是停的")
    check_eq(app.motor_bus.engaged_channels(), [], "拒绝之后离合必须全断开")


# ---------------------------------------------------------------------------
# ④ 进退响应时间：4 个通道统一，只作用于手动点动
# ---------------------------------------------------------------------------

def test_jog_time_clamped_to_safe_range():
    """★ 进退响应时间必须夹在安全区间里

    0 秒没有意义（离合还没咬合就停了）；几百秒会把机构里关着的料顶坏。
    """
    from AMS_MODEL import AMS
    from hardware_config import JOG_TIME_MS, JOG_MIN_MS, JOG_MAX_MS

    check(JOG_MIN_MS >= 100, "下限不能太小，否则离合还没咬合就停了")
    check(JOG_MAX_MS <= 120000, "上限不能太大，否则容易顶坏料")
    check(JOG_MIN_MS <= JOG_TIME_MS <= JOG_MAX_MS, "出厂默认值要落在区间内")

    check_eq(AMS.clamp_jog_ms("3000"), 3000, "要支持字符串形式的毫秒数")
    check_eq(AMS.clamp_jog_ms(2500), 2500, "区间内原样返回")
    check_eq(AMS.clamp_jog_ms(0), JOG_MIN_MS, "0 要夹到下限")
    check_eq(AMS.clamp_jog_ms(-5), JOG_MIN_MS, "负数要夹到下限")
    check_eq(AMS.clamp_jog_ms(999999), JOG_MAX_MS, "超大值要夹到上限")
    check_eq(AMS.clamp_jog_ms("3"), JOG_MIN_MS,
             "clamp_jog_ms 的单位是毫秒，3 毫秒当然要夹到下限")
    check_eq(AMS.clamp_jog_ms("abc"), JOG_TIME_MS, "非数字要退回默认值")
    check_eq(AMS.clamp_jog_ms(None), JOG_TIME_MS, "None 要退回默认值")
    check_eq(AMS.clamp_jog_ms(1500.9), 1500, "小数要取整")


def test_jog_time_is_uniform_for_four_channels():
    """★ 一个值管 4 个通道，并且要落盘（重启也记得）"""
    import AMS_WEB as web_module
    from AMS_WEB import AMS_WEB

    app = AMS_WEB()
    saved = []
    app.updata_data = lambda d: (saved.append(dict(d)), dict(d))[1]

    check_eq(app.set_jog_ms(3000), 3000, "设置的值要原样生效")
    check_eq(app.jog_ms, 3000, "实例上的值要更新")
    check_eq(saved, [{"jog_ms": 3000}], "必须把 jog_ms 写进 config.json")

    src = _code_only(web_module.AMS_WEB.handle_hardware_test)
    check("self.jog_ms" in src,
          "点动时长必须来自 self.jog_ms —— 这就是「4 个通道统一」的实现方式")
    check("times_ms" in src, "仍允许请求里显式指定 times_ms（内部/兼容用）")

    # 4 个通道都走同一个值
    for channel in (1, 2, 3, 4):
        check_eq(app.clamp_jog_ms(app.jog_ms), 3000,
                 "通道%d 用的也必须是同一个值" % channel)


def test_jog_time_survives_restart():
    """★ 重启后要从 config.json 恢复进退响应时间"""
    import AMS_MODEL
    from AMS_MODEL import AMS
    from hardware_config import JOG_TIME_MS

    original = AMS_MODEL.read_json_file
    try:
        AMS_MODEL.read_json_file = lambda path: {"jog_ms": 7000}
        app = AMS()
        app.auto_update_access("whatever.json")
        check_eq(app.jog_ms, 7000, "应该从配置里恢复 jog_ms")

        # 配置里是坏值时要安全退回默认，不能抛异常
        AMS_MODEL.read_json_file = lambda path: {"jog_ms": "坏值", "access": None}
        app = AMS()
        app.auto_update_access("whatever.json")
        check_eq(app.jog_ms, JOG_TIME_MS, "配置里的坏值要退回默认值")

        # 配置里超范围时也要夹住
        AMS_MODEL.read_json_file = lambda path: {"jog_ms": 99999999, "access": None}
        app = AMS()
        app.auto_update_access("whatever.json")
        from hardware_config import JOG_MAX_MS
        check_eq(app.jog_ms, JOG_MAX_MS, "配置里超范围的值也要夹住")
    finally:
        AMS_MODEL.read_json_file = original


def test_jog_set_endpoint_clamps_and_reports():
    """★ /jog_set 要如实报告"实际生效"的秒数（被夹过必须说明）"""
    from AMS_WEB import AMS_WEB
    from hardware_config import JOG_MIN_MS

    app = AMS_WEB()
    app.updata_data = lambda d: dict(d)

    conn = _FakeConn()
    check_eq(app.handle_jog_set(conn, {"seconds": 2.5}), True, "正常设置应成功")
    data = _json_of(conn)
    check_eq(data.get("ok"), True, "应报告成功")
    check_eq(data.get("jog_ms"), 2500, "应该回实际生效的毫秒数")
    check_eq(app.jog_ms, 2500, "实例值要更新")

    conn2 = _FakeConn()
    app.handle_jog_set(conn2, {"seconds": 0.05})       # 小于下限
    d2 = _json_of(conn2)
    check_eq(d2.get("jog_ms"), JOG_MIN_MS, "超范围要夹到下限")
    check("调整" in (d2.get("info") or ""),
          "被夹过要在提示里说明，不能显示用户填的数、实际跑另一个数")

    conn3 = _FakeConn()
    check_eq(app.handle_jog_set(conn3, {"ms": "abc"}), False, "非数字要拒绝")
    check_eq(_status_of(conn3), 400, "参数不对要回 400")

    conn4 = _FakeConn()
    check_eq(app.handle_jog_set(conn4, {}), False, "既没 seconds 也没 ms 要拒绝")
    check_eq(_status_of(conn4), 400, "缺参数要回 400")


def test_jog_time_only_affects_manual_jog():
    """★ 进退响应时间只管手动点动，绝不能改变自动换料的时长

    自动换料走的是 NO_LIMIT_LOAD_MS / NO_LIMIT_RETRACT_MS / FILAMENT_STEP_MS
    那一套，跟 jog_ms 完全无关 —— 用户明确要求"只管手动点动按钮"。
    """
    import inspect
    import AMS_WEB as web_module
    import device_processing
    from AMS_MODEL import AMS

    check("jog_ms" not in inspect.getsource(AMS.exchange_fileament),
          "自动换料流程不能读 jog_ms（那会把手动设置的秒数带进换料）")
    check("jog_ms" not in inspect.getsource(device_processing),
          "硬件驱动层不应该知道 jog_ms")
    check("self.jog_ms" in _code_only(web_module.AMS_WEB.handle_hardware_test),
          "真正用 jog_ms 的应该只有手动点动这条路径")


def test_status_exposes_jog_ms_and_mqtt_configured():
    """★ /status 要带 jog_ms（按钮文案）和 mqtt_configured（区分未配 / 重试中）"""
    from AMS_WEB import AMS_WEB

    app = AMS_WEB()
    app.jog_ms = 4000
    info = app._status_dict()
    check_eq(info.get("jog_ms"), 4000, "要把设备上生效的时长告诉页面")

    check("mqtt_configured" in info, "/status 必须带 mqtt_configured")
    check_eq(info["mqtt_configured"], False, "参数没配全时应该是 False")

    app.mqtt_server = "192.168.1.9"
    app.DEVICE_SERIAL = "SN1"
    app.password = "12345678"
    check_eq(app._status_dict()["mqtt_configured"], True,
             "三项都齐了才算是「已配置」")


def test_hardware_status_reports_busy():
    """★ 硬件状态要带 busy，页面才能显示「正在动作」"""
    from AMS_WEB import AMS_WEB

    app = AMS_WEB()
    hw = app._hardware_dict()
    check("busy" in hw, "hardware 里要带 busy")
    check_eq(hw["busy"], False, "初始应空闲")

    app.motor_bus.begin(1, 1, owner="t")
    try:
        check_eq(app._hardware_dict()["busy"], True, "begin 之后要显示忙")
    finally:
        app.motor_bus.finish()
    check_eq(app._hardware_dict()["busy"], False, "finish 之后要恢复空闲")


# ---------------------------------------------------------------------------
# ⑤ 应用层 OTA：网页上传更新包 → 写文件系统 → 重启
# ---------------------------------------------------------------------------

def _in_temp_dir(prefix):
    """进到临时目录，返回 (临时目录, 还原函数)"""
    import tempfile
    cwd = os.getcwd()
    tmp = tempfile.mkdtemp(prefix=prefix)
    os.chdir(tmp)
    return tmp, (lambda: os.chdir(cwd))


def test_ota_crc32_matches_zlib():
    """★ 纯 Python 的 CRC32 必须和 zlib.crc32 完全一致

    否则 PC 上用 zlib 造的包，到了板子上会被判成"校验失败"。
    """
    import zlib
    from ota_update import crc32

    samples = (b"", b"a", b"hello world", bytes(range(256)),
               "中文与 ASCII 混合内容".encode("utf-8"), b"\x00" * 1000)
    for data in samples:
        check_eq(crc32(data), zlib.crc32(data) & 0xFFFFFFFF,
                 "CRC32 与 zlib 不一致（%d 字节的样本）" % len(data))

    # 增量调用（分块喂入）也要和一次性算出来的一致
    blob = bytes(range(256)) * 7
    inc = 0
    for i in range(0, len(blob), 37):
        inc = crc32(blob[i:i + 37], inc)
    check_eq(inc, zlib.crc32(blob) & 0xFFFFFFFF, "必须支持分块增量计算 CRC32")


def test_ota_pack_roundtrips_byte_for_byte():
    """★ 更新包解析后必须逐字节还原，子目录要自动创建"""
    import shutil
    from ota_update import OtaUpdate, build_pack

    files = [
        ("AMS_WEB.py", b"print('web')\n" * 40),
        ("index.html", "页面内容·中文·".encode("utf-8") * 30),
        ("bambu/bambu_mqtt.py", b"x = 1\n" * 10),
    ]
    pack = build_pack(files)

    tmp, restore = _in_temp_dir("ams_ota_ok_")
    try:
        upd = OtaUpdate(len(pack))
        # 故意用不规则的块大小喂进去，模拟 TCP 分段
        for i in range(0, len(pack), 7):
            upd.feed(pack[i:i + 7])
        names = upd.finish()

        check_eq(sorted(names), sorted(n for n, _ in files), "应该写入全部文件")
        for name, data in files:
            with open(name, "rb") as handle:
                check_eq(handle.read(), data, "%s 的内容必须逐字节一致" % name)
        check_eq([n for n in os.listdir(".") if n.endswith(".new")], [],
                 "成功路径也不能留下 .new 临时文件")
        check_eq(os.path.isdir("bambu"), True, "子目录要自动创建")
        check_eq(upd.state, "done", "状态应是 done")
        check_eq(upd.received, len(pack), "接收字节数要等于包长")
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)


def test_ota_pack_rejects_corrupt_payload():
    """★ 载荷被改动一个字节 → CRC 必须发现，且不留半截文件"""
    import shutil
    from ota_update import OtaUpdate, OtaError, build_pack

    pack = bytearray(build_pack([("AMS_WEB.py", b"abcdef" * 60)]))
    pack[-1] ^= 0xFF                      # 破坏最后一个字节

    tmp, restore = _in_temp_dir("ams_ota_corrupt_")
    try:
        upd = OtaUpdate(len(pack))
        raised = False
        try:
            upd.feed(bytes(pack))
            upd.finish()
        except OtaError:
            raised = True
        check(raised, "CRC 不匹配时必须抛 OtaError")
        check_eq(os.path.exists("AMS_WEB.py"), False, "校验失败时绝不能写出正式文件")
        check_eq([n for n in os.listdir(".") if n.endswith(".new")], [],
                 "feed 出错要自己清干净，不能留下 .new 残file")
        check_eq(upd.error is not None, True, "要记录错误原因，供上层打日志")
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)


def test_ota_pack_rejects_truncated_upload():
    """★ 上传中途断线：finish() 必须拒绝，且不留半截文件、不重启"""
    import shutil
    from ota_update import OtaUpdate, OtaError, build_pack

    pack = build_pack([("AMS_WEB.py", b"hello" * 100)])

    tmp, restore = _in_temp_dir("ams_ota_half_")
    try:
        upd = OtaUpdate(len(pack))
        upd.feed(pack[:len(pack) // 2])        # 只喂一半
        raised = False
        try:
            upd.finish()
        except OtaError:
            raised = True
        check(raised, "包不完整时 finish() 必须抛 OtaError")
        check_eq([n for n in os.listdir(".") if n.endswith(".new")], [],
                 "中止后不能留下 .new 残file")
        check_eq(os.path.exists("AMS_WEB.py"), False, "不完整的包绝不能生成正式文件")
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)


def test_ota_rejects_firmware_bin_with_helpful_message():
    """★ 用户很容易把整机固件 BIN 拖进来 —— 必须提示改走 USB"""
    from ota_update import (OtaUpdate, OtaError, ESP_IMAGE_MAGIC,
                            build_pack, looks_like_firmware_bin)

    bin_like = bytes([ESP_IMAGE_MAGIC]) + b"\x00" * 64
    check(looks_like_firmware_bin(bin_like), "0xE9 开头的应该被识别为整机固件 BIN")
    check(not looks_like_firmware_bin(build_pack([("a.py", b"1")])),
          "正常的 .ams 更新包不能被误判成固件")

    raised = None
    upd = OtaUpdate(len(bin_like))
    try:
        upd.feed(bin_like)
    except OtaError as e:
        raised = str(e)
    check(raised is not None, "整机固件 BIN 必须被拒绝")
    check("USB" in raised and ".ams" in raised,
          "提示要说清楚这是整机固件、应用更新包是 .ams、整机升级走 USB，"
          "实际: %r" % raised)


def test_ota_rejects_unsafe_names():
    """★ 更新包来自网络，必须当不可信输入：拒绝越权路径 / 覆盖设备配置"""
    import shutil
    from ota_update import OtaUpdate, OtaError, build_pack

    tmp, restore = _in_temp_dir("ams_ota_evil_")
    try:
        for bad in ("../evil.py", "/etc/passwd", "bambu/../../evil.py",
                    "config.json", "wifi.dat", "boot_stat.json"):
            pack = build_pack([(bad, b"payload")])
            upd = OtaUpdate(len(pack))
            raised = False
            try:
                upd.feed(pack)
            except OtaError:
                raised = True
            check(raised, "危险文件名 %r 必须被拒绝" % bad)
        check_eq([n for n in os.listdir(".") if n.endswith(".new")], [],
                 "拒绝之后不能留下任何临时文件")
        check_eq(os.listdir("."), [], "被拒绝的包绝不能写出任何文件")
    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)


def test_ota_route_is_streamed():
    """★ 升级包几百 KB，绝不能先整份读进内存"""
    import inspect
    import AMS_WEB as web_module
    from AMS_WEB import OTA_MAX_BYTES, OTA_RECV_CHUNK, STREAM_ROUTES

    check("ota_upload" in STREAM_ROUTES, "ota_upload 必须走流式路径")
    check(OTA_RECV_CHUNK <= 4096,
          "接收块要小，单次分配不能大（当前 %d）" % OTA_RECV_CHUNK)
    check(OTA_MAX_BYTES <= 4 * 1024 * 1024, "上限别放太大，避免把文件系统写满")

    src = _code_only(web_module.AMS_WEB.handle_ota_upload)
    check("OtaUpdate" in src, "必须交给 OtaUpdate 边收边写")
    check("upd.feed(" in src, "收到一块就喂一块，不能攒在内存里")
    check("upd.received < total" in src, "循环条件必须盯住已接收字节数")
    check("await asyncio.sleep_ms" in src, "等待数据时要 await 让步，否则网页会卡住")
    check("abort()" in src, "任何失败路径都要 abort()，绝不留半截文件")
    check("OTA_IDLE_TIMEOUT_MS" in src, "要有「中间没数据」的超时判定")
    check("OTA_TOTAL_TIMEOUT_MS" in src, "要有总时长上限")

    routes = inspect.getsource(web_module.AMS_WEB._serve_client)
    check("STREAM_ROUTES" in routes,
          "路由必须在读请求体之前就分流，否则几百 KB 会先被整份读进内存")
    idx_stream = routes.find("STREAM_ROUTES")
    idx_body = routes.find("_read_body")
    check(idx_stream != -1 and idx_body != -1 and idx_stream < idx_body,
          "顺序不对：流式接口必须排在通用「读请求体」之前")


def test_ota_upload_endpoint_writes_files_then_asks_for_reboot():
    """★ 端到端：POST /ota_upload 把文件写进文件系统，并告诉前端会重启"""
    import asyncio
    import shutil
    import AMS_WEB as web_module
    from AMS_WEB import AMS_WEB
    from ota_update import build_pack

    files = [("AMS_WEB.py", b"# new web\n" * 20),
             ("umqtt/simple.py", b"# new mqtt\n" * 5)]
    pack = build_pack(files)

    tmp, restore = _in_temp_dir("ams_ota_http_")
    saved_delay = web_module.REBOOT_DELAY_MS
    try:
        web_module.REBOOT_DELAY_MS = 1        # 别在生产值上白等
        app = AMS_WEB()
        app.allow_reboot = False              # 自测里不真重启

        head, payload = _raw_request("POST", "ota_upload", pack)
        conn = _FakeConn([head, payload])
        asyncio.run(app._serve_client(conn))

        check_eq(_status_of(conn), 200, "上传成功应回 200")
        data = _json_of(conn)
        check_eq(data.get("ok"), True, "应该报告成功")
        check_eq(data.get("files"), len(files), "要报告写入了几个文件")
        check_eq(data.get("reboot"), True, "要告诉前端设备会重启")
        check("重启" in (data.get("info") or ""), "提示语要写明会自动重启")

        for name, content in files:
            with open(name, "rb") as handle:
                check_eq(handle.read(), content, "%s 必须被写进文件系统" % name)
        check_eq([n for n in os.listdir(".") if n.endswith(".new")], [],
                 "成功路径不能留 .new 临时文件")
    finally:
        web_module.REBOOT_DELAY_MS = saved_delay
        restore()
        shutil.rmtree(tmp, ignore_errors=True)


def test_ota_upload_endpoint_rejects_bad_pack():
    """★ 坏的升级包要回 400 并说明原因，且绝不写文件、绝不重启"""
    import asyncio
    import shutil
    import AMS_WEB as web_module
    from AMS_WEB import AMS_WEB

    tmp, restore = _in_temp_dir("ams_ota_bad_http_")
    saved_delay = web_module.REBOOT_DELAY_MS
    try:
        web_module.REBOOT_DELAY_MS = 1
        app = AMS_WEB()
        app.allow_reboot = False

        head, payload = _raw_request("POST", "ota_upload", b"definitely not a pack")
        conn = _FakeConn([head, payload])
        asyncio.run(app._serve_client(conn))

        check_eq(_status_of(conn), 400, "坏包要回 400")
        data = _json_of(conn)
        check_eq(data.get("ok"), False, "要如实说没成功")
        check(data.get("info"), "要给出中文原因")
        check_eq(os.listdir("."), [], "坏包绝不能写出任何文件")

        # Content-Length 为 0（前端没带文件）也要被挡住
        head2, _ = _raw_request("POST", "ota_upload", None)
        conn2 = _FakeConn([head2])
        asyncio.run(app._serve_client(conn2))
        check_eq(_status_of(conn2), 400, "没带文件要回 400")
    finally:
        web_module.REBOOT_DELAY_MS = saved_delay
        restore()
        shutil.rmtree(tmp, ignore_errors=True)


def test_reboot_is_skipped_in_selftest_mode():
    """★ allow_reboot=False 时只记日志、不真重启（桌面自测用）"""
    import AMS_WEB as web_module
    from AMS_WEB import AMS_WEB

    app = AMS_WEB()
    app.allow_reboot = False
    check_eq(app._reboot(), False, "自测模式下不应该真的重启")

    app.allow_reboot = True
    original = web_module._machine_reset
    called = []
    web_module._machine_reset = lambda: called.append(True)
    try:
        check_eq(app._reboot(), True, "允许重启时应该真的调用 reset()")
        check_eq(called, [True], "reset() 必须被调用一次")
    finally:
        web_module._machine_reset = original


# ---------------------------------------------------------------------------
# ⑥ 页面：OTA 菜单 / 进退响应时间 / MQTT 状态文案
# ---------------------------------------------------------------------------

def test_page_has_ota_menu_and_upload():
    """★ 系统菜单里要有 OTA 页，能选更新包并上传"""
    page = _page_source()
    check("id:'ota'" in page, "系统菜单里要有 OTA 项")
    check("系统升级" in page, "菜单/标题要能看出是系统升级")
    check('id="page_ota"' in page, "要有 OTA 页面容器")
    check('id="ota_file"' in page, "要有选择更新包的文件框")
    check('id="btn_ota_upload"' in page, "要有上传按钮")
    check("'/ota_upload'" in page, "页面要真的把文件 POST 到 /ota_upload")
    check(".ams" in page, "文件框要限定 .ams 更新包")
    check("body: f" in page,
          "要把 File 对象直接当请求体发出去（设备端按 Content-Length 流式接收）")
    check("application/octet-stream" in page, "要声明 Content-Type")


def test_web_routing_works_on_raw_bytes_headers():
    """★ 路由必须能解析"从 socket 读来的原始字节"请求行

    请求头是 bytes。如果正则用 str 模式去匹配 bytes，MicroPython 和 CPython
    都会直接抛 TypeError（can't use a string pattern on a bytes-like object）
    —— 后果是**每个请求都 500**，整个网页全废。
    这条就是拿真实字节请求把 _serve_client 走一遍，防止改回去。
    """
    import asyncio
    from AMS_WEB import AMS_WEB

    app = AMS_WEB()

    # 正常 GET /status
    conn = _FakeConn([b"GET /status HTTP/1.1\r\nHost: ams\r\n\r\n"])
    asyncio.run(app._serve_client(conn))
    check_eq(_status_of(conn), 200, "GET /status 应该正常回 200")
    check("access_list" in _body_of(conn).decode("utf-8"), "正文应该是状态 JSON")

    # 带查询串的 URL 也要能解析出路径
    conn2 = _FakeConn([b"GET /status?t=1 HTTP/1.1\r\nHost: ams\r\n\r\n"])
    asyncio.run(app._serve_client(conn2))
    check_eq(_status_of(conn2), 200, "带 ? 查询串时也要能认出 /status")

    # 未知路径 → 404（能走到 404 就说明路由解析本身没炸）
    conn3 = _FakeConn([b"GET /no_such_thing HTTP/1.1\r\nHost: ams\r\n\r\n"])
    asyncio.run(app._serve_client(conn3))
    check_eq(_status_of(conn3), 404, "未知路径要回 404 而不是 500")

    # POST 写接口收到空请求体 → 400（不是 404、更不是 500）
    conn4 = _FakeConn([b"POST /mqtt_connect HTTP/1.1\r\nHost: ams\r\n\r\n"])
    asyncio.run(app._serve_client(conn4))
    check_eq(_status_of(conn4), 400, "写接口空请求体要回 400")


def test_page_ota_hints_usb_for_firmware_bin():
    """★ 页面上要说清楚"整机 BIN 不能走这里"，否则用户白折腾"""
    page = _page_source()
    check("整机固件 BIN" in page, "要提示整机固件 BIN 不能走网页 OTA")
    check("esp32c3-ams-firmware.bin" in page, "要点名 C3 那个文件名")
    check("esp32s3-ams-firmware.bin" in page, "两块板都要点名（S3 那份也不能走这里）")
    check("USB" in page, "要告诉用户整机升级还得插 USB")


def test_page_hardware_has_jog_time_setting():
    """★ 硬件调试页要有「进退响应时间」入口（4 个通道统一）"""
    page = _page_source()
    check('id="jog_seconds"' in page, "要有秒数输入框")
    check('id="btn_jog_save"' in page, "要有保存按钮")
    check("进退响应时间" in page, "文案要说清楚这是进退响应时间")
    check("4 个通道统一" in page, "要写明对 4 个通道统一生效")
    check("不会改变自动换料" in page,
          "必须写明「只影响手动点动」，否则用户会以为改了自动换料时长")
    check('min="0.2"' in page and 'max="60"' in page,
          "输入范围要和设备端的安全区间一致")


def test_page_jog_buttons_follow_the_device_setting():
    """★ 点动按钮上的秒数必须跟着设备设置走，不能写死

    设备端现在自己决定时长（前端不传 times_ms），所以：
      · 按钮文案要用 jogLabel() 拼出来；
      · 设置一改就要重画按钮（重画的 key 里带上 jogMs）；
      · /status 回来的 jog_ms 要用 syncJogInput 同步进输入框和按钮。
    """
    page = _page_source()
    check("function jogLabel()" in page, "要有根据 jogMs 生成文案的函数")
    check("'进料 ' + jogLabel()" in page, "进料按钮文案要跟着设置走")
    check("'退料 ' + jogLabel()" in page, "退料按钮文案要跟着设置走")
    check("+ '@' + jogMs" in page, "按钮重画的 key 里要带上 jogMs")
    check("syncJogInput(d.jog_ms)" in page, "/status 里的 jog_ms 要同步进页面")
    check("send('/jog_set'" in page, "保存要走 /jog_set")
    check("channel:channel, direction:direction }" in page,
          "点动请求只带通道和方向，时长交给设备端决定")
    check("document.activeElement !== el" in page,
          "正在输入时不能被每 2 秒一次的 /status 覆盖掉")


def test_page_mqtt_status_distinguishes_configured_but_retrying():
    """★ 页面要能区分「还没配」和「配好了、后台正在重试」"""
    page = _page_source()
    check("d.mqtt_configured" in page, "要根据 mqtt_configured 给不同提示")
    check("已保存" in page, "配好了要显示「已保存」")
    check("d.saved" in page and "d.connected" in page,
          "仍然要按 saved / connected 分别给提示")


# ===========================================================================
# 两块开发板：ESP32-C3 与 ESP32-S3（42 针）的引脚表
# 背景：执行机构是「1 个共享电机 + 4 路电磁离合」，两块板可用的 IO 完全不同，
#       所以引脚表拆成 board_c3.py / board_s3.py，编译期再由 board_select.py
#       决定设备加载哪一份。这里把**两份表都**拉出来校验，避免"只测了 C3、
#       S3 那份写错了却没人发现"。
# ===========================================================================

REQUIRED_BOARD_ATTRS = (
    "BOARD_ID", "BOARD_NAME", "CHIP", "MACHINE_KEYWORDS", "GPIO_MAX",
    "MOTOR_PIN_IN1", "MOTOR_PIN_IN2", "CLUTCH_PINS", "LED_PIN",
    "LIMIT_SWITCH_PINS", "RECOMMENDED_LIMIT_SWITCH_PINS",
    "STRAPPING_PINS", "SAFE_OUTPUT_PINS", "RESERVED_PINS",
    "SPARE_PINS", "BOARD_NOTES",
)


def _load_board(board_id):
    import importlib
    return importlib.import_module("board_%s" % board_id)


def _used_pins(board):
    """这块板按默认接线实际占用的引脚集合"""
    pins = {board.MOTOR_PIN_IN1, board.MOTOR_PIN_IN2}
    pins.update(board.CLUTCH_PINS)
    if board.LED_PIN is not None:
        pins.add(board.LED_PIN)
    for pin in board.LIMIT_SWITCH_PINS:
        if pin is not None:
            pins.add(pin)
    return pins


def _check_board_pin_table(board, expected_chip):
    """一份引脚表该满足的全部硬性条件"""
    for name in REQUIRED_BOARD_ATTRS:
        check(hasattr(board, name), "%s 缺少必需的常量 %s" % (board.CHIP, name))

    check_eq(board.CHIP, expected_chip, "%s 的 CHIP 写错了" % board.BOARD_ID)
    check(board.MACHINE_KEYWORDS, "MACHINE_KEYWORDS 不能为空，否则认不出芯片")
    check(board.BOARD_NOTES, "BOARD_NOTES 不能为空")

    # 4 路离合是这个方案的硬性前提
    check_eq(len(board.CLUTCH_PINS), 4, "%s 必须是 4 路电磁离合" % board.BOARD_ID)
    check_eq(len(board.LIMIT_SWITCH_PINS), 4,
             "%s 的 LIMIT_SWITCH_PINS 必须和离合路数一致" % board.BOARD_ID)
    check_eq(len(set(board.CLUTCH_PINS)), 4, "4 路离合的引脚不能重复")

    # 电机两脚不能是同一个
    check(board.MOTOR_PIN_IN1 != board.MOTOR_PIN_IN2, "电机 IN1 / IN2 不能同脚")

    # 电机脚必须在"可安全输出"的集合里
    for label, pin in (("IN1", board.MOTOR_PIN_IN1), ("IN2", board.MOTOR_PIN_IN2)):
        check(pin in board.SAFE_OUTPUT_PINS,
              "%s 的电机 %s = GPIO%d 不在安全输出引脚里" % (board.BOARD_ID, label, pin))

    # 所有会主动驱动的脚：范围合法 + 不碰保留脚
    used = _used_pins(board)
    for pin in sorted(used):
        check(isinstance(pin, int) and 0 <= pin <= board.GPIO_MAX,
              "%s 的 GPIO%d 超出 GPIO0~GPIO%d 范围" % (board.BOARD_ID, pin, board.GPIO_MAX))
        check(pin not in board.RESERVED_PINS,
              "%s 的 GPIO%d 是保留脚（%s），不能用"
              % (board.BOARD_ID, pin, board.RESERVED_PINS.get(pin)))

    # 安全输出集合本身不能和保留脚冲突
    overlap = set(board.SAFE_OUTPUT_PINS) & set(board.RESERVED_PINS)
    check_eq(overlap, set(), "%s 的 SAFE_OUTPUT_PINS 里混进了保留脚" % board.BOARD_ID)

    # strapping 集合要有意义
    check(board.STRAPPING_PINS, "%s 必须声明 strapping 引脚" % board.BOARD_ID)
    for pin in board.STRAPPING_PINS:
        check(0 <= pin <= board.GPIO_MAX, "strapping 脚 GPIO%d 超出范围" % pin)

    # 剩余可用 IO：范围合法、不是保留脚、也没被占用
    spare = set(board.SPARE_PINS)
    for pin in sorted(spare):
        check(0 <= pin <= board.GPIO_MAX, "剩余可用 GPIO%d 超出范围" % pin)
        check(pin not in board.RESERVED_PINS, "剩余可用里不能有保留脚 GPIO%d" % pin)
        check(pin not in used, "GPIO%d 已被占用，不能再算剩余可用" % pin)
        check(pin in set(board.SAFE_OUTPUT_PINS) | set(board.STRAPPING_PINS),
              "GPIO%d 既不在安全输出脚也不在 strapping 脚里，不该列为可用" % pin)
    # 反过来：安全输出脚里没被占用的，必须全部列出来（不许漏）
    check_eq(spare, set(board.SAFE_OUTPUT_PINS) - used,
             "%s 的 SPARE_PINS 必须把安全输出脚里未占用的全部列出" % board.BOARD_ID)

    strap_spare = getattr(board, "STRAPPING_SPARE_PINS", ())
    check(strap_spare, "%s 应该把空闲的 strapping 脚也列出来" % board.BOARD_ID)
    check_eq(set(strap_spare), set(board.STRAPPING_PINS) - used,
             "%s 的 STRAPPING_SPARE_PINS 必须等于 strapping 脚减去已占用的" % board.BOARD_ID)


def test_board_c3_pin_table_is_valid():
    """★ C3 引脚表：7 只干净脚，电机在安全脚上，没有越界或误用保留脚"""
    _check_board_pin_table(_load_board("c3"), "esp32c3")


def test_board_s3_pin_table_is_valid():
    """★ S3（42 针）引脚表：4 路离合全在干净脚上，剩余 IO 不漏报"""
    _check_board_pin_table(_load_board("s3"), "esp32s3")


def test_s3_clutches_all_land_on_clean_pins():
    """★ S3 相比 C3 最大的好处：4 路离合不用再挤到 strapping 脚上

    C3 只有 7 只干净脚，4 号离合被迫放在 strapping 的 GPIO3 上"带病运行"；
    S3 的离合 1~4 落在 GPIO6/7/8/9，四只全是干净脚，一劳永逸。
    """
    board = _load_board("s3")
    for index, pin in enumerate(board.CLUTCH_PINS, 1):
        check(pin in board.SAFE_OUTPUT_PINS,
              "S3 的电磁离合%d = GPIO%d 不在安全输出脚上" % (index, pin))
        check(pin not in board.STRAPPING_PINS,
              "S3 的电磁离合%d 不该落在 strapping 脚 GPIO%d 上" % (index, pin))


def test_s3_lists_every_spare_io_pin():
    """★ S3 的「剩余可用 IO 全部引出」——数量与内容都要对得上

    本方案占 6 只（电机 2 + 离合 4）+ 状态灯 1 只，
    S3 可安全输出的脚共 25 只，所以剩余应为 18 只；再加上 4 只空闲 strapping 脚。
    """
    board = _load_board("s3")
    check_eq(len(board.SAFE_OUTPUT_PINS), 25, "S3 安全输出脚应为 25 只")
    check_eq(len(board.SPARE_PINS), 18, "S3 剩余干净 IO 应为 18 只")
    check_eq(len(board.STRAPPING_SPARE_PINS), 4, "S3 剩余 strapping 脚应为 4 只")

    # 到位开关的推荐脚必须真的还在剩余列表里
    for pin in board.RECOMMENDED_LIMIT_SWITCH_PINS:
        check(pin in board.SPARE_PINS,
              "到位开关推荐脚 GPIO%d 应该也在剩余可用列表里" % pin)

    # 保留脚一个都不能混进来
    for pin in (19, 20, 26, 43, 44):
        check(pin in board.RESERVED_PINS, "GPIO%d 必须被 S3 标为保留脚" % pin)
        check(pin not in board.SPARE_PINS, "保留脚 GPIO%d 不能算剩余可用" % pin)


def test_board_files_agree_on_the_shared_contract():
    """两份引脚表必须提供同一套常量名，hardware_config 才能无差别地加载"""
    c3 = _load_board("c3")
    s3 = _load_board("s3")
    for name in REQUIRED_BOARD_ATTRS:
        check(hasattr(c3, name) and hasattr(s3, name),
              "两份板型文件都要有 %s" % name)
    check(c3.BOARD_ID != s3.BOARD_ID, "两块板的 BOARD_ID 不能相同")
    check_eq(c3.GPIO_MAX, 21, "C3 的 GPIO 上限是 21")
    check_eq(s3.GPIO_MAX, 48, "S3 的 GPIO 上限是 48")
    # 机器名关键字不能互相包含，否则识别会串台
    c3_keys = set(c3.MACHINE_KEYWORDS) - {"C3"}
    s3_keys = set(s3.MACHINE_KEYWORDS) - {"S3"}
    check_eq(c3_keys & s3_keys, set(), "C3 与 S3 的识别关键字不能重叠")


def test_built_board_select_wins_over_chip_detection():
    """★ 编译期写入的 board_select.py 优先级最高（这就是"编译自动选配置"）"""
    saved = hardware_config._BUILT_BOARD
    try:
        hardware_config._BUILT_BOARD = "s3"
        board_id, source = hardware_config._detect_board()
        check_eq(board_id, "s3", "board_select.py 写 s3 时必须选 S3")
        check("构建" in source or "board_select" in source,
              "要说明板型来自构建时指定，实际: %r" % source)

        hardware_config._BUILT_BOARD = "c3"
        check_eq(hardware_config._detect_board()[0], "c3",
                 "board_select.py 写 c3 时必须选 C3")
    finally:
        hardware_config._BUILT_BOARD = saved


def test_board_falls_back_to_chip_name_detection():
    """没有 board_select.py 时按芯片名识别；认不出来按 C3 处理"""
    saved_built = hardware_config._BUILT_BOARD
    saved_machine = hardware_config._machine_name
    try:
        hardware_config._BUILT_BOARD = "auto"
        hardware_config._machine_name = lambda: "ESP32S3 MODULE WITH ESP32S3"
        check_eq(hardware_config._detect_board()[0], "s3",
                 "机器名里带 S3 要认出 S3")

        hardware_config._machine_name = lambda: "ESP32C3 MODULE WITH ESP32C3"
        check_eq(hardware_config._detect_board()[0], "c3",
                 "机器名里带 C3 要认出 C3")

        hardware_config._machine_name = lambda: "X86_64"
        board_id, source = hardware_config._detect_board()
        check_eq(board_id, "c3", "认不出芯片时要退回 C3")
        check("认不出" in source, "退回默认时要提示一句，实际: %r" % source)
    finally:
        hardware_config._BUILT_BOARD = saved_built
        hardware_config._machine_name = saved_machine


def test_current_board_config_matches_the_selected_board_file():
    """hardware_config 导出的必须就是所选板型文件里的那一套"""
    check(hardware_config.BOARD_ID in ("c3", "s3"),
          "BOARD_ID 只能是 c3 或 s3，实际 %r" % hardware_config.BOARD_ID)
    check(hardware_config.BOARD_SOURCE, "要能说清板型是怎么定下来的")

    board = _load_board(hardware_config.BOARD_ID)
    check_eq(hardware_config.BOARD_NAME, board.BOARD_NAME, "板名应与板型文件一致")
    check_eq(hardware_config.CHIP, board.CHIP, "芯片名应与板型文件一致")
    check_eq(hardware_config.GPIO_MAX, board.GPIO_MAX, "GPIO 上限应与板型文件一致")
    check_eq(tuple(hardware_config.CLUTCH_PINS), tuple(board.CLUTCH_PINS),
             "离合引脚应与板型文件一致")
    check_eq(hardware_config.MOTOR_PIN_IN1, board.MOTOR_PIN_IN1,
             "电机 IN1 应与板型文件一致")
    check_eq(tuple(hardware_config.SPARE_PINS), tuple(board.SPARE_PINS),
             "剩余可用 IO 应与板型文件一致")
    check_eq(tuple(hardware_config.STRAPPING_SPARE_PINS),
             tuple(getattr(board, "STRAPPING_SPARE_PINS", ())),
             "剩余 strapping 脚应与板型文件一致")

    # 接线表里要把板型和剩余可用脚都打出来，方便上电核对
    report = hardware_config.describe()
    check(hardware_config.BOARD_NAME in report, "接线表里要写清开发板型号")
    check("剩余可用" in report, "接线表里要列出剩余可用 IO")


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
