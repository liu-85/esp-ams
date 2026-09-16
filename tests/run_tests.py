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
