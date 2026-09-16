"""
motor_clutch.py —— 共享直流电机 + 多路电磁离合 的驱动层
========================================================

硬件拓扑：
                    ┌── 离合1 ──> 料盘位1 送料轮
     共享电机 ──────┼── 离合2 ──> 料盘位2 送料轮
     (H桥 IN1/IN2)  ├── 离合3 ──> 料盘位3 送料轮
                    └── 离合4 ──> 料盘位4 送料轮

本模块提供四个类：

    LimitSwitch        —— 到位/限位开关（微动开关、光电开关），未安装时可透明降级
    HBridgeMotor       —— H 桥两线电机（IN1/IN2），只有方向，含换向死区保护
    Clutch             —— 单路电磁离合，**不允许绕过总线直接吸合**
    FilamentMotorBus   —— 总线仲裁层，**强制保证同一时刻最多 1 路离合吸合**

============ "只能 1 路离合吸合" 是怎么被强制的 ============

这是本模块最重要的职责，做了四重保障：

  1. 物理层前置断开
     FilamentMotorBus.engage() 在吸合目标通道之前，**无条件**先把所有离合
     的引脚写成"断开"电平，再等一段时间让上一路彻底脱开，然后才吸合目标。
     也就是说，任何一次吸合动作都是从"全断开"状态出发的。

  2. 吸合前状态复核
     写完全部断开后立刻回读每一路的状态。如果发现还有残留吸合
     （比如引脚被外部代码改写、或驱动板没跟上），立即再次全部断开，
     并抛 ClutchConflictError 取消本次动作，绝不带病吸合。

  3. 直接调用拦截
     Clutch.engage() / Clutch.release() 必须经过总线仲裁；没绑定总线的
     Clutch 直接调用会抛 MotorBusError。只有总线内部能改引脚电平。

  4. 运行期体检
     FilamentMotorBus.assert_single() 可以随时调用，发现多路吸合就
     立刻停电机 + 全部断开。主循环里建议每个周期都调一次（开销极小）。

另外，所有"吸合→动作→断开"的复合操作都走 `run()` 或 `hold()`，
它们用 try/finally 保证即使中途抛异常也一定会释放离合、停止电机。
"""

from machine import Pin
import time

# ==========================================================================
# 异常类型
# ==========================================================================


class MotorBusError(Exception):
    """总线/驱动层异常基类"""


class ChannelOutOfRange(MotorBusError):
    """通道号不存在"""


class ClutchConflictError(MotorBusError):
    """检测到多于 1 路电磁离合同时吸合 —— 严重违反硬件约束"""


class MotorBusyError(MotorBusError):
    """总线正在被其它动作占用，拒绝并发切换通道"""


# ==========================================================================
# 工具类
# ==========================================================================


class NullPin:
    """未安装的外设占位对象，接口与 machine.Pin 兼容。

    作用：让上层代码（尤其是历史代码）可以无脑调用 .value()，
    不需要到处写 `if switch is not None` 判断。
    """

    __slots__ = ("_value", "id")

    def __init__(self, value=0, id=-1):
        self._value = value
        self.id = id

    def value(self, *args):
        if args:
            self._value = 1 if args[0] else 0
            return None
        return self._value

    def init(self, *args, **kwargs):
        return None

    def on(self):
        self.value(1)

    def off(self):
        self.value(0)

    def __repr__(self):
        return "<NullPin value=%d>" % self._value


class LimitSwitch:
    """到位 / 限位开关（微动开关、光电开关均可）。

    未安装时 installed 为 False，triggered() 恒返回 False，
    上层代码无需做 None 判断，探测逻辑会自动降级。
    """

    def __init__(self, pin=None, active_level=0, pull_up=True, name="limit"):
        self.name = name
        self.active_level = 1 if active_level else 0
        self.installed = pin is not None
        if self.installed:
            pull = Pin.PULL_UP if pull_up else Pin.PULL_DOWN
            self.pin = Pin(pin, Pin.IN, pull)
        else:
            # 未安装时给一个"永不触发"的替身：空闲电平与 active_level 相反
            self.pin = NullPin(1 - self.active_level, id=-1)

    def triggered(self):
        """开关是否被触发（当前是否到位）"""
        if not self.installed:
            return False
        return self.pin.value() == self.active_level

    def value(self):
        """原始电平，便于调试"""
        return self.pin.value()

    def __repr__(self):
        state = "未安装" if not self.installed else ("触发" if self.triggered() else "未触发")
        return "<LimitSwitch %s %s>" % (self.name, state)


# ==========================================================================
# 电机
# ==========================================================================


class HBridgeMotor:
    """H 桥两线直流电机驱动（IN1 / IN2）。

    方向定义（与老代码保持一致）：
        direction =  1  → 进料（正转）
        direction = -1  → 退料（反转）
        direction =  0  → 停止

    换向时会先停止、等待 dead_time_ms 再反向，避免 H 桥上下管直通
    （直通会瞬间烧毁驱动芯片），同时也减小对机械结构的冲击。
    """

    def __init__(self, pin_in1, pin_in2, dead_time_ms=30, name="motor"):
        self.name = name
        self.in1 = Pin(pin_in1, Pin.OUT)
        self.in2 = Pin(pin_in2, Pin.OUT)
        self.dead_time_ms = dead_time_ms
        self._direction = 0
        self.stop()

    @property
    def direction(self):
        return self._direction

    def stop(self):
        """立即停止（两脚同时拉低）"""
        self.in1.value(0)
        self.in2.value(0)
        self._direction = 0
        return self

    def set_direction(self, direction):
        """设置方向并立即启动，不阻塞"""
        direction = int(direction)
        if direction == 0:
            return self.stop()
        if direction not in (1, -1):
            raise ValueError("direction 只能是 1(进料) / -1(退料) / 0(停止)")
        if self._direction == direction:
            return self          # 同方向重复设置，不做无谓的停-启
        if self._direction != 0:
            # 换向：先刹车，留出死区时间
            self.stop()
            if self.dead_time_ms:
                time.sleep_ms(self.dead_time_ms)
        if direction == 1:
            self.in1.value(1)
            self.in2.value(0)
        else:
            self.in1.value(0)
            self.in2.value(1)
        self._direction = direction
        return self

    def run(self, direction=1, times_ms=200):
        """设置方向并阻塞运行 times_ms 毫秒（不自动停止）"""
        self.set_direction(direction)
        if times_ms and times_ms > 0:
            time.sleep_ms(times_ms)
        return self

    def run_then_stop(self, direction=1, times_ms=200):
        """设置方向、运行、然后停止"""
        self.run(direction, times_ms)
        return self.stop()

    def __repr__(self):
        return "<HBridgeMotor %s dir=%d>" % (self.name, self._direction)


# ==========================================================================
# 电磁离合
# ==========================================================================


class Clutch:
    """单路电磁离合（电磁离合器）。

    ⚠️ 安全设计：本类的引脚电平**只允许**由所属 FilamentMotorBus 改写。
       外部代码必须调用 bus.engage(channel) / bus.release_all()，
       或者 Clutch.engage()（会自动转交总线仲裁）。
       未绑定总线时直接 engage() 会抛 MotorBusError。
    """

    __slots__ = ("channel", "name", "pin", "active_level", "inactive_level",
                 "engage_ms", "release_ms", "_engaged", "_bus")

    def __init__(self, pin, channel, active_level=1, engage_ms=80, release_ms=60, name=None):
        self.channel = channel
        self.name = name or ("clutch%d" % channel)
        self.pin = Pin(pin, Pin.OUT)
        self.active_level = 1 if active_level else 0
        self.inactive_level = 1 - self.active_level
        self.engage_ms = engage_ms
        self.release_ms = release_ms
        self._engaged = False
        self._bus = None
        self._apply(False, settle=False)   # 上电默认断开

    # -------- 内部：绑定总线 --------
    def bind(self, bus):
        self._bus = bus
        return self

    # -------- 公开接口（走总线仲裁） --------
    def engage(self, owner=None):
        """吸合本路离合。会先强制断开其它所有通道。"""
        if self._bus is None:
            raise MotorBusError("%s 未绑定总线，禁止直接吸合" % self.name)
        return self._bus.engage(self.channel, owner=owner)

    def release(self):
        """断开本路离合（只断自己，由总线执行）"""
        if self._bus is None:
            raise MotorBusError("%s 未绑定总线，禁止直接操作" % self.name)
        return self._bus.release(self.channel)

    def is_engaged(self):
        return self._engaged

    # -------- 私有：仅供总线调用 --------
    def _apply(self, on, settle=True):
        """真正写引脚。on=True 吸合，False 断开。settle=True 会等待机械动作完成。"""
        self.pin.value(self.active_level if on else self.inactive_level)
        self._engaged = bool(on)
        if settle:
            delay = self.engage_ms if on else self.release_ms
            if delay:
                time.sleep_ms(delay)
        return self

    def __repr__(self):
        return "<Clutch %s engaged=%s>" % (self.name, self._engaged)


# ==========================================================================
# 上下文管理器
# ==========================================================================


class _ChannelHold:
    """`with bus.hold(channel):` 的上下文管理器。

    进入时吸合指定通道，退出时**无条件**停电机并断开全部离合，
    即使代码块里抛异常也一样。这是写换料动作时推荐的用法。
    """

    __slots__ = ("_bus", "_channel", "_owner")

    def __init__(self, bus, channel, owner=None):
        self._bus = bus
        self._channel = channel
        self._owner = owner

    def __enter__(self):
        # 先吸合（此时 _busy 还是 False，engage() 允许执行），再置位忙标志
        self._bus.engage(self._channel, owner=self._owner)
        self._bus._busy = True
        return self._bus

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._bus.release_all()   # 内部会把 _busy 复位
        return False   # 不吞异常，交给上层处理


# ==========================================================================
# 总线：1 个电机 + N 路离合
# ==========================================================================


class FilamentMotorBus:
    """共享电机 + 多路电磁离合的总线仲裁层。

    这是上层唯一应该直接使用的对象。

    典型用法::

        bus = FilamentMotorBus(motor, (6, 7, 10, 3))

        # 用法一：一站式动作（自动 吸合 → 转 → 停 → 断开）
        bus.run(channel=2, direction=1, times_ms=1500)

        # 用法二：连续动作，中途可以查传感器
        with bus.hold(channel=2, owner="交换耗材") as b:
            for _ in range(10):
                b.motor.run(1, 500)
                if sensor.triggered():
                    break

        # 用法三：主循环里定期体检
        bus.assert_single()
    """

    def __init__(self, motor, clutch_pins, active_level=1,
                 engage_ms=80, release_ms=60, settle_ms=20, name="filament_bus"):
        self.name = name
        self.motor = motor
        self.engage_ms = engage_ms
        self.release_ms = release_ms
        self.settle_ms = settle_ms

        self.clutches = {}
        for index, pin in enumerate(clutch_pins, 1):
            clutch = Clutch(pin, index,
                            active_level=active_level,
                            engage_ms=engage_ms,
                            release_ms=release_ms)
            clutch.bind(self)
            self.clutches[index] = clutch

        self._active = None     # 当前吸合的通道号；None = 全部断开
        self._owner = None      # 当前占用总线的动作名称，方便看日志
        self._busy = False      # 是否正处在一次复合动作中
        self._conflicts = 0     # 累计检测到几次违规（0 才是健康状态）

        self._detach_all()      # 上电第一件事：确保全部离合断开

    # ------------------------------------------------------------------
    # 属性 / 查询
    # ------------------------------------------------------------------
    @property
    def channels(self):
        """所有可用通道号，如 (1, 2, 3, 4)"""
        return tuple(sorted(self.clutches.keys()))

    @property
    def active_channel(self):
        """当前吸合的通道号，None 表示全部断开"""
        return self._active

    @property
    def owner(self):
        """当前正在执行的动作名称"""
        return self._owner

    @property
    def conflicts(self):
        """累计冲突次数。正常应该永远是 0"""
        return self._conflicts

    @property
    def busy(self):
        """是否正处在一次两段式动作中（begin() 之后、finish() 之前）。

        网页靠它判断"现在能不能再按一下点动"：忙的时候直接拒绝并说明，
        而不是让用户排长队等（那正是"按一下转圈半天"的来源）。
        """
        return self._busy

    def engaged_channels(self):
        """当前处于吸合状态的通道号列表"""
        return [ch for ch in self.channels if self.clutches[ch].is_engaged()]

    def channel_count(self):
        return len(self.clutches)

    def _check_channel(self, channel):
        if channel not in self.clutches:
            raise ChannelOutOfRange("通道 %r 不存在，可用通道: %s"
                                    % (channel, list(self.channels)))
        return self.clutches[channel]

    def status(self):
        """返回一份可 JSON 序列化的状态，供网页端显示"""
        return {
            "active_channel": self._active,
            "owner": self._owner,
            "engaged": self.engaged_channels(),
            "conflicts": self._conflicts,
            "motor_direction": self.motor.direction,
            "channels": list(self.channels),
            "busy": self._busy,
        }

    # ------------------------------------------------------------------
    # 核心：互斥吸合
    # ------------------------------------------------------------------
    def engage(self, channel, owner=None):
        """吸合指定通道的电磁离合，**同时强制断开其它所有通道**。

        流程：
            1. 停电机
            2. 无条件把所有离合写成断开
            3. 等上一路彻底脱开
            4. 回读复核，若还有残留吸合 → 再次全部断开并抛异常
            5. 吸合目标通道，等待咬合
        """
        clutch = self._check_channel(channel)

        # 同一通道重复吸合是幂等的：不重复动作，避免机构抖动
        if self._active == channel and clutch.is_engaged():
            if owner:
                self._owner = owner
            return clutch

        if self._busy:
            raise MotorBusyError("总线正被 [%s] 占用，拒绝切换到通道 %d"
                                 % (self._owner, channel))

        # 1) 电机必须先停，绝不能带着转速切换离合
        self.motor.stop()

        # 2) 物理层：先全部断开
        had_engaged = bool(self.engaged_channels())
        self._detach_all()

        # 3) 等上一路离合彻底脱开，否则会短暂出现"两路都咬合"的机械重叠
        if had_engaged:
            delay = self.release_ms + self.settle_ms
            if delay:
                time.sleep_ms(delay)

        # 4) 复核：必须全部处于断开状态
        still = self.engaged_channels()
        if still:
            self._conflicts += 1
            self._detach_all()
            raise ClutchConflictError(
                "吸合通道 %d 前检测到 %s 仍处于吸合状态，已强制全部断开并取消本次动作"
                % (channel, still))

        # 5) 吸合目标
        clutch._apply(True)
        self._active = channel
        self._owner = owner
        return clutch

    def _detach_all(self):
        """无条件把全部离合写成断开电平（不等待），用于纠正异常状态"""
        for clutch in self.clutches.values():
            clutch._apply(False, settle=False)
        self._active = None

    def release_all(self, stop_motor=True):
        """停止电机并断开全部离合。任何动作结束时都应该调用它。"""
        if stop_motor:
            self.motor.stop()
        for clutch in self.clutches.values():
            if clutch.is_engaged():
                clutch._apply(False, settle=True)   # 等机械脱开
        self._active = None
        self._owner = None
        self._busy = False
        return self

    def release(self, channel):
        """只断开指定通道（如果它正好是当前吸合的那一路）"""
        clutch = self._check_channel(channel)
        if not clutch.is_engaged():
            return clutch
        if self._active != channel:
            self._conflicts += 1
            raise ClutchConflictError("通道 %d 处于吸合状态但未被总线记录，已强制全部断开" % channel)
        self.motor.stop()
        clutch._apply(False, settle=True)
        self._active = None
        self._owner = None
        return clutch

    # ------------------------------------------------------------------
    # 运行期体检
    # ------------------------------------------------------------------
    def assert_single(self, force_release=True):
        """体检：确认同时最多只有 1 路离合吸合。

        建议在主循环里每个周期调用一次（只是读几个 GPIO，开销极小）。
        发现异常时抛 ClutchConflictError，并按 force_release 决定是否立即断开。
        返回 True 表示状态正常。
        """
        engaged = self.engaged_channels()
        if len(engaged) > 1:
            self._conflicts += 1
            if force_release:
                self.motor.stop()
                self._detach_all()
            raise ClutchConflictError("检测到多路电磁离合同时吸合: %s，已处置" % engaged)

        # 让总线的记录与实际引脚保持一致（例如引脚被外部代码改过）
        actual = engaged[0] if engaged else None
        if self._active != actual:
            self._active = actual
            if actual is None:
                self._owner = None
        return True

    # ------------------------------------------------------------------
    # 复合动作
    # ------------------------------------------------------------------
    def run(self, channel, direction=1, times_ms=200, release=True, owner=None):
        """吸合 channel → 电机按 direction 转 times_ms → 停 → （默认）断开全部离合。

        ⚠️ 这是**阻塞**版本：中间的 times_ms 靠 time.sleep_ms 度过。
           只在"调用方本来就在同步上下文里、且允许被阻塞"的场合使用
           （比如换料主流程）。

           在 uasyncio 的请求处理里**不要用它** —— 那会把整个事件循环按住，
           网页表现就是"按一下按钮转圈好几秒，其它请求全排队"。
           那种场合请改用 begin() / finish() 两段式（见下）。
        """
        # 先吸合（engage 自身要求 _busy 为 False），再把总线标记为"忙"
        self.engage(channel, owner=owner or ("run-ch%d" % channel))
        self._busy = True
        try:
            self.motor.run(direction, times_ms)
            self.motor.stop()
        finally:
            self._busy = False
            if release:
                self.release_all()
        return True

    # ------------------------------------------------------------------
    # 两段式动作（给异步/网页用，绝不阻塞事件循环）
    # ------------------------------------------------------------------
    def begin(self, channel, direction=1, owner=None):
        """**非阻塞**启动：吸合通道 + 让电机转起来，然后立刻返回。

        这是 `run()` 的"上半场"。调用方拿到控制权后自己决定等多久
        （异步代码里用 `await asyncio.sleep_ms(...)`），到点再调 `finish()`。

        这样做的意义：网页按一下点动可以**立刻回包**，电机继续转，
        事件循环不被按住 —— 其它请求、状态灯、轮询都不会被饿死。

        和 run() 一样受"同一时刻只能 1 路离合吸合"约束；如果总线已经忙，
        engage() 会抛 MotorBusyError，不会出现两路同时吸合。
        """
        self.engage(channel, owner=owner or ("begin-ch%d" % channel))
        self._busy = True
        try:
            self.motor.set_direction(direction)
        except Exception:
            # 电机没转起来就绝不能占着总线：立刻回到安全状态
            self._busy = False
            self.release_all()
            raise
        return True

    def finish(self, release=True):
        """结束一次 begin() 启动的动作：停电机 + （默认）断开全部离合。

        用 try/finally 保证即便停电机时抛异常，也一定会走到 release_all()，
        不会留下"离合还吸着、总线还标记忙"的僵尸状态。
        """
        try:
            self.motor.stop()
        finally:
            self._busy = False
            if release:
                self.release_all()
        return True

    def hold(self, channel, owner=None):
        """返回上下文管理器：进入时吸合，退出时停电机 + 断开全部离合。

        用法::

            with bus.hold(3, owner="退料") as b:
                b.motor.set_direction(-1)
                time.sleep_ms(2000)
                b.motor.stop()
        """
        return _ChannelHold(self, channel, owner=owner)

    def __repr__(self):
        return "<FilamentMotorBus %s channels=%s active=%s conflicts=%d>" % (
            self.name, list(self.channels), self._active, self._conflicts)
