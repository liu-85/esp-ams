"""
tests/mpy_stubs/machine.py
==========================
`machine` 模块的桌面端桩实现，只在 PC / CI 上用于跑逻辑自测。

它把 GPIO 的状态记录下来，测试代码可以通过 `level(gpio)` 读取，
从而验证"同一时刻最多 1 路电磁离合吸合"这类硬件约束。
真正烧进 ESP32-C3 的是 MicroPython 自带的 machine 模块，与本文件无关。
"""

# GPIO 号 -> Pin 实例
_PINS = {}


def level(gpio):
    """读取某个 GPIO 当前被写成的电平，未初始化返回 None"""
    pin = _PINS.get(gpio)
    return pin._value if pin else None


def levels(gpios):
    """批量读取，返回列表"""
    return [level(g) for g in gpios]


def reset():
    """清空所有引脚状态（测试用例之间调用）"""
    _PINS.clear()


class Pin:
    IN = 0
    OUT = 1
    OPEN_DRAIN = 2
    ALT = 3

    PULL_UP = 1
    PULL_DOWN = 2

    IRQ_RISING = 1
    IRQ_FALLING = 2
    IRQ_LOW_LEVEL = 4
    IRQ_HIGH_LEVEL = 8

    def __init__(self, id, mode=-1, pull=-1, value=None, drive=0, alt=-1):
        self.id = id
        self._mode = Pin.IN if mode == -1 else mode
        self._pull = pull
        # 上拉输入空闲时读到高电平，下拉/悬空读到低电平
        self._value = 1 if pull == Pin.PULL_UP else 0
        if value is not None:
            self._value = 1 if value else 0
        _PINS[id] = self

    def init(self, mode=-1, pull=-1, value=None, **kwargs):
        if mode != -1:
            self._mode = mode
        if pull != -1:
            self._pull = pull
        if value is not None:
            self._value = 1 if value else 0

    def value(self, *args):
        if args:
            self._value = 1 if args[0] else 0
            return None
        return self._value

    def on(self):
        self.value(1)

    def off(self):
        self.value(0)

    def irq(self, *args, **kwargs):
        return None

    def __repr__(self):
        return "Pin(%r, value=%r)" % (self.id, self._value)


class PWM:
    def __init__(self, pin, freq=None, duty=None, duty_u16=None, duty_ns=None):
        if isinstance(pin, Pin):
            self.pin = pin
            self._gpio = pin.id
        else:
            self.pin = Pin(pin, Pin.OUT)
            self._gpio = pin
        self._freq = freq or 0
        self._duty = duty or 0

    def freq(self, *args):
        if args:
            self._freq = args[0]
            return None
        return self._freq

    def duty(self, *args):
        if args:
            self._duty = args[0]
            return None
        return self._duty

    def duty_u16(self, *args):
        return self.duty(*args)

    def deinit(self):
        return None


class Signal(Pin):
    def __init__(self, pin, invert=False):
        super().__init__(pin if isinstance(pin, int) else pin.id)
        self._invert = invert


class ADC:
    def __init__(self, pin):
        self.pin = pin

    def read(self):
        return 0

    def read_u16(self):
        return 0


def reset_cause():
    return 1


def freq(*args):
    return 160000000
