"""
device_processing.py —— 外设驱动与料盘位对象
=============================================

本文件包含两块内容：

  1. 通用外设小工具：呼吸灯(huxideng)、舵机(shiji)、28BYJ48 步进电机
     —— 保留原样，供扩展接口时使用。

  2. material —— **单个料盘位（通道）的门面对象**。

--------------------------------------------------------------------------
关于 material 的改造说明（重要）
--------------------------------------------------------------------------
老版本：每个料盘位各配 1 个直流电机，用 2 个 GPIO 组成 H 桥直驱。
        4 个料盘位就要 8 个 GPIO，而且用到 GPIO22/23 —— ESP32-C3 上根本没有这两个脚。

新版本：整机只有 **1 个共享直流电机 + 4 路电磁离合**。
        电机占 2 个 GPIO（H 桥 IN1/IN2），每路离合占 1 个 GPIO，合计 6 个 GPIO。
        要哪个料盘位送料，就吸合那一路的电磁离合，让它的送料轮咬上电机主轴。

因此 material 不再自己持有电机，而是变成"通道代理"：
        material.dianji_roll(方向, 时长)
            ↓ 内部展开为
        FilamentMotorBus.run(本通道, 方向, 时长)
            ↓ 即
        断开其它全部离合 → 吸合本通道离合 → 电机转 → 停 → 断开全部离合

为了不破坏 AMS_MODEL.py 里的换料逻辑，**旧接口名全部保留**：
        dianji_roll() / stop_roll() / dianji_top / dianji_bottom

其中 dianji_top / dianji_bottom 在老代码里是电机方向引脚，但 AMS_MODEL
一直把它们当**到位开关**在读（now_filament / fileament_move 里都是
`if dianji_top.value():`），所以这里直接改成到位开关对象，语义反而变正确了。
没有装开关时它们是 NullPin，.value() 恒返回"未触发"电平，不会报错。
"""

from machine import Pin, PWM
import time
import uasyncio as asyncio

from logout import logout
from hardware_config import (
    LIMIT_SWITCH_PINS,
    LIMIT_SWITCH_ACTIVE_LEVEL,
    LIMIT_SWITCH_PULL_UP,
    FILAMENT_STEP_MS,
    MOTOR_PIN_IN1,
    MOTOR_PIN_IN2,
    MOTOR_DEAD_TIME_MS,
    MOTOR_BOOT_SETTLE_MS,
    CLUTCH_PINS,
    CLUTCH_ACTIVE_LEVEL,
    CLUTCH_ENGAGE_MS,
    CLUTCH_RELEASE_MS,
    CLUTCH_SETTLE_MS,
    boot_safety_report,
)
from motor_clutch import (
    HBridgeMotor,
    FilamentMotorBus,
    LimitSwitch,
    NullPin,
    MotorBusError,
    ClutchConflictError,
    MotorBusyError,
    ChannelOutOfRange,
)


# ==========================================================================
# 一、通用外设小工具（保留原样）
# ==========================================================================


def huxideng():
    """GPIO2 呼吸灯效果（阻塞式，仅用于调试）"""
    pin2 = PWM(Pin(2))
    pin2.freq(1000)
    while True:
        for n in range(1000):
            pin2.duty(n)
            time.sleep_ms(1)
        for n in range(1000, -1, -1):
            pin2.duty(n)
            time.sleep_ms(1)


def shiji(pin, jiaodu):
    """舵机角度控制，jiaodu 为 -90 ~ 90"""
    p2 = PWM(Pin(pin))
    p2.freq(50)
    f = int(((jiaodu + 10) / 90 + 0.5) * 1023 / 20)
    p2.duty(f)


class stepping_motor_28BYJ48:
    """28BYJ48 四相步进电机（保留原样，供扩展接口使用）"""

    def __init__(self, pin1, pin2, pin3, pin4, delay_time_ms=2):
        self.pin1 = Pin(pin1, Pin.OUT)
        self.pin2 = Pin(pin2, Pin.OUT)
        self.pin3 = Pin(pin3, Pin.OUT)
        self.pin4 = Pin(pin4, Pin.OUT)
        self.delay_time_ms = delay_time_ms
        self.init_value()

    def init_value(self):
        self.pin1.value(0)
        self.pin2.value(0)
        self.pin3.value(0)
        self.pin4.value(0)

    def roll(self, rotation_number, direction=1):
        for n in range(rotation_number * 2050):
            value_list = [0, 0, 0, 0]
            value_list[n % 4] = 1
            if direction == 1:
                self.pin1.value(value_list[0])
                self.pin2.value(value_list[1])
                self.pin3.value(value_list[2])
                self.pin4.value(value_list[3])
            elif direction == -1:
                self.pin4.value(value_list[0])
                self.pin3.value(value_list[1])
                self.pin2.value(value_list[2])
                self.pin1.value(value_list[3])
            time.sleep_ms(self.delay_time_ms)
        self.init_value()


# ==========================================================================
# 二、硬件构建工厂
# ==========================================================================

# 上电引脚自检的结果，供网页显示（build_motor_bus() 里填充）
BOOT_SAFETY = {
    "ok": True,
    "report": "",
    "problems": [],
}


def build_motor_bus():
    """按 hardware_config.py 的配置创建"共享电机 + 4 路电磁离合"总线。

    ⚠️ 上电时会确保所有离合都处于断开状态，绝不会出现两路同时吸合。

    另外这里还会做两件和"接上负载就重启"直接相关的事：
        1. 先跑一遍引脚安全自检（strapping 脚不能当输出用），结果打到串口
           并且存进 BOOT_SAFETY，网页上也能看到；
        2. 等 MOTOR_BOOT_SETTLE_MS 毫秒再挂 H 桥 —— 上电瞬间电机/离合的
           浪涌最容易把 3.3V 拉塌，而 ESP32-C3 一旦欠压就复位。
    """
    ok, report = boot_safety_report()
    # 用 update 原地修改，不要重新赋值 —— 网页那边 import 的是同一个字典对象
    BOOT_SAFETY.update({
        "ok": ok,
        "report": report,
        "problems": [] if ok else [
            line.strip() for line in report.split("\n") if line.strip().startswith("×")
        ],
    })
    logout(report)
    if not ok:
        logout("!! 引脚配置有问题，硬件仍会被创建（好让你能打开网页看诊断），"
               "但请立刻按上面的提示改接线！")

    # 等电源稳定：欠压复位最常见的触发点就是"上电后立刻驱动负载"
    if MOTOR_BOOT_SETTLE_MS and MOTOR_BOOT_SETTLE_MS > 0:
        time.sleep_ms(MOTOR_BOOT_SETTLE_MS)

    motor = HBridgeMotor(MOTOR_PIN_IN1, MOTOR_PIN_IN2,
                         dead_time_ms=MOTOR_DEAD_TIME_MS,
                         name="filament_motor")
    bus = FilamentMotorBus(
        motor,
        CLUTCH_PINS,
        active_level=CLUTCH_ACTIVE_LEVEL,
        engage_ms=CLUTCH_ENGAGE_MS,
        release_ms=CLUTCH_RELEASE_MS,
        settle_ms=CLUTCH_SETTLE_MS,
        name="filament_bus",
    )
    logout("共享电机已初始化: IN1=GPIO%d IN2=GPIO%d，%d 路电磁离合 %s"
           % (MOTOR_PIN_IN1, MOTOR_PIN_IN2, bus.channel_count(), list(CLUTCH_PINS)))
    return bus


def build_materials(bus, limit_switch_pins=None):
    """按总线通道数创建料盘位对象列表。

    返回的列表下标 0 对应"料盘位 1"（物理位置），与电磁离合一一对应。
    注意：物理料盘位 ≠ 打印机的通道号，两者的映射由 access_list 决定。
    """
    pins = list(limit_switch_pins if limit_switch_pins is not None else LIMIT_SWITCH_PINS)
    channels = list(bus.channels)
    # 配置长度不足时补齐 None（未安装）
    while len(pins) < len(channels):
        pins.append(None)

    materials = []
    for index, channel in enumerate(channels):
        materials.append(material(channel, bus, limit_top=pins[index]))
    return materials


# ==========================================================================
# 三、料盘位（通道）对象
# ==========================================================================


class _HoldGuard:
    """material.hold() 的上下文管理器。

    进入：吸合本通道离合（自动断开其它所有通道）
    退出：停电机 + 断开全部离合（无论是否异常）
    """

    __slots__ = ("_material",)

    def __init__(self, mat):
        self._material = mat

    def __enter__(self):
        self._material.clutch_on()
        self._material._holding = True
        return self._material

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._material._holding = False
        self._material.bus.release_all()
        return False


class material:
    """单个料盘位（通道）的门面对象。

    参数
    ----
    channel       : 本料盘位对应的电磁离合通道号（1 起）
    bus           : FilamentMotorBus 实例
    limit_top     : 退料到位开关的 GPIO（未安装传 None 或省略）
    limit_bottom  : 进料到位开关的 GPIO（未安装传 None 或省略）
    step_ms       : 单步推送时长，默认取 hardware_config.FILAMENT_STEP_MS

    推荐用法::

        mat.feed(1500)                     # 进料 1.5 秒
        mat.retract(1500)                  # 退料 1.5 秒
        with mat.hold():                   # 长时间连续动作
            for _ in range(10):
                bus.motor.run(1, 500)
                if mat.limit_triggered(1):
                    break

    兼容用法（老代码）::

        mat.dianji_roll(-1, 500)           # 退料 0.5 秒
        mat.dianji_top.value()             # 读到断开关电平
    """

    def __init__(self, channel, bus, limit_top=None, limit_bottom=None, step_ms=None):
        self.channel = channel
        self.bus = bus
        self.step_ms = step_ms if step_ms else FILAMENT_STEP_MS
        self._holding = False

        self.limit_top = LimitSwitch(limit_top,
                                     active_level=LIMIT_SWITCH_ACTIVE_LEVEL,
                                     pull_up=LIMIT_SWITCH_PULL_UP,
                                     name="ch%d-top" % channel)
        self.limit_bottom = LimitSwitch(limit_bottom,
                                        active_level=LIMIT_SWITCH_ACTIVE_LEVEL,
                                        pull_up=LIMIT_SWITCH_PULL_UP,
                                        name="ch%d-bottom" % channel)

        # ---- 兼容老代码的属性名 ----
        # 老代码把这两个当"到位开关"读，这里正好语义一致；
        # 未安装开关时是 NullPin，.value() 不会抛异常。
        self.dianji_top = self.limit_top.pin
        self.dianji_bottom = self.limit_bottom.pin

    # ------------------------------------------------------------------
    # 电磁离合
    # ------------------------------------------------------------------
    def clutch_on(self):
        """吸合本通道的电磁离合（会自动断开其它所有通道）"""
        self.bus.engage(self.channel, owner="material-ch%d" % self.channel)
        return self

    def clutch_off(self):
        """断开全部离合并停止电机"""
        self.bus.motor.stop()
        self.bus.release_all()
        return self

    @property
    def clutch_engaged(self):
        return self.bus.clutches[self.channel].is_engaged()

    def hold(self):
        """返回上下文管理器，期间保持本通道离合吸合。"""
        return _HoldGuard(self)

    # ------------------------------------------------------------------
    # 到位开关
    # ------------------------------------------------------------------
    @property
    def has_limit_top(self):
        return self.limit_top.installed

    @property
    def has_limit_bottom(self):
        return self.limit_bottom.installed

    @property
    def has_limit(self):
        """本通道是否安装了任意一个到位开关"""
        return self.limit_top.installed or self.limit_bottom.installed

    def limit_triggered(self, orientation=1):
        """到位开关是否被触发。

        orientation =  1 表示查"进料到位"（dianji_bottom）
        orientation = -1 表示查"退料到位"（dianji_top）
        """
        if orientation == 1:
            return self.limit_bottom.triggered()
        return self.limit_top.triggered()

    def is_jam(self):
        """两个到位开关同时触发 → 判定为卡料或机构异常。

        未安装开关时永远返回 False。
        """
        if self.limit_top.installed and self.limit_bottom.installed:
            return self.limit_top.triggered() and self.limit_bottom.triggered()
        return False

    # ------------------------------------------------------------------
    # 送料动作
    # ------------------------------------------------------------------
    def dianji_roll(self, direction=1, times_ms=200):
        """兼容老接口：本通道送料/退料 times_ms 毫秒。

        direction = 1 进料，-1 退料。
        不在 hold() 上下文里时，会自动完成"吸合 → 转 → 停 → 断开"全过程。
        """
        if self._holding:
            # 已经在 hold() 里，离合保持吸合，只做转动
            self.bus.motor.run(direction, times_ms)
            self.bus.motor.stop()
        else:
            self.bus.run(self.channel, direction, times_ms,
                         release=True,
                         owner="material-ch%d" % self.channel)
        return self

    def feed(self, times_ms=1000):
        """进料"""
        return self.dianji_roll(1, times_ms)

    def retract(self, times_ms=1000):
        """退料"""
        return self.dianji_roll(-1, times_ms)

    def stop_roll(self):
        """停止电机（不改变离合状态）。

        注意：老代码在 dianji_roll 末尾调用它做收尾，这里只负责停电机，
        离合的断开由 dianji_roll 内部的 bus.run(release=True) 保证。
        """
        self.bus.motor.stop()
        return self

    def is_one(self, duration=10, orientation=-1):
        """在 duration 秒内尝试把料拉到位。

        到位返回 True；无到位开关时直接返回 False（无法判定）。
        """
        if not self.has_limit:
            logout("通道%d 未安装到位开关，is_one() 无法判定，返回 False" % self.channel)
            return False
        deadline = time.time() + duration
        with self.hold():
            while time.time() < deadline:
                if self.limit_triggered(orientation):
                    self.bus.motor.stop()
                    return True
                # 低速推进，给开关留出响应时间
                self.bus.motor.set_direction(orientation)
                time.sleep_ms(50)
            self.bus.motor.stop()
        return False

    def __repr__(self):
        return "<material ch=%d limits(top=%s,bottom=%s) engaged=%s>" % (
            self.channel, self.limit_top.installed, self.limit_bottom.installed,
            self.clutch_engaged)


# ==========================================================================
# 四、上电自检 / 手动点动（把本文件当脚本跑时执行）
# ==========================================================================
if __name__ == "__main__":
    from hardware_config import validate, describe

    ok, problems = validate()
    logout(describe())
    if not ok:
        for item in problems:
            logout("[配置问题] " + item, is_error=True)
        raise SystemExit("硬件配置有误，请检查 hardware_config.py")

    bus_test = build_motor_bus()
    for ch in bus_test.channels:
        logout("点动通道%d：进料 800ms" % ch)
        bus_test.run(ch, direction=1, times_ms=800, owner="selftest-ch%d" % ch)
        # 每一次点动结束后都必须回到"全部断开"状态
        left = bus_test.engaged_channels()
        logout("通道%d 点动结束，仍吸合的离合: %s" % (ch, left if left else "无"))
        time.sleep_ms(300)

    logout("自检完成，冲突次数 = %d（正常应为 0）" % bus_test.conflicts)
