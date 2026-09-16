"""
reset_info.py —— 复位原因诊断 + 上电安全态 + 启动计数
=====================================================

为什么需要这个模块？
    "接上负载之后一直重启、网页打不开、电机一直有电流声" 这类现象，
    光看是分不清下面哪一类的，而处理方式完全不同：

        · 电源被负载浪涌拉塌        → 欠压复位 BROWN_OUT_RESET
        · 某个任务长时间不让出 CPU  → 看门狗复位 WDT_RESET
        · 引脚接错进不了启动模式    → 每次都是冷启动 PWRON_RESET，反复循环
        · 程序自己跑飞              → 各种 RESET 乱跳

    所以本模块做三件事：

      1. capture()     上电第一件事：把 machine.reset_cause() 记下来
      2. make_safe()   在任何外设对象被创建之前，把电机 / 离合 / LED
                       全部置到"不上电"的电平
                       —— 刚上电时 GPIO 是**浮空**的，浮空的 H 桥输入
                          后果不可预测（可能自己转、可能上下管直通发热）
      3. record_boot() 把启动次数写进 boot_stat.json
                       —— 次数疯涨，就是复位循环的铁证

    这三件事都必须在 main.py 之前做完，所以由 boot.py 调用。
"""

import time

try:
    import ujson
except ImportError:          # CPython（跑桌面自测时）
    import json as ujson

try:
    import machine
except ImportError:          # 理论上不会发生
    machine = None

import hardware_config

BOOT_STAT_FILE = "boot_stat.json"

# ---------------------------------------------------------------------------
# 复位原因的中文解释。不同 MicroPython 版本里常量的名字不完全一样，
# 所以一律用 getattr(machine, 名字) 兜底，不写死数字。
# ---------------------------------------------------------------------------
_CAUSE_TEXT = {
    "PWRON_RESET":
        "冷启动（刚上电）。如果串口里一直在刷这一条，说明芯片在反复掉电"
        " —— 八成是电源带不动电机/离合，重点查电源功率和共地",
    "HARD_RESET":
        "硬复位（按了 EN 键或复位按钮）",
    "WDT_RESET":
        "看门狗复位。程序卡住超过了看门狗时间被强行拉回"
        " —— 一般是某个任务阻塞了事件循环（比如旧的 wait_msg()）",
    "RTC_WDT_RESET":
        "RTC 看门狗复位：程序卡死了",
    "TASK_WDT_RESET":
        "任务看门狗复位：某个任务长时间不让出 CPU",
    "BROWN_OUT_RESET":
        "★ 欠压复位：供电电压掉到了阈值以下。负载浪涌把电源拉塌了，"
        "先查电源功率、线径、共地和 VM 的滤波电容",
    "SOFT_RESET":
        "软复位（软件主动复位，或 REPL 里按了 Ctrl-D）",
    "DEEPSLEEP_RESET":
        "从深度睡眠唤醒",
    "EXT_RESET":
        "外部复位",
    "PIN_RESET":
        "引脚复位",
    "TOUCH_RESET":
        "触摸复位",
}

# ---------------------------------------------------------------------------
# 本模块的内存状态（每次上电重新填）
# ---------------------------------------------------------------------------
CAUSE_NAME = None       # 例如 "BROWN_OUT_RESET"
CAUSE_CODE = None       # 原始数字
CAUSE_DESC = ""         # 中文解释
BOOT_MS = 0             # 记录时刻的 ticks_ms
BOOT_COUNT = 0          # 累计启动次数（来自 boot_stat.json）


def _ticks_ms():
    try:
        return time.ticks_ms()
    except AttributeError:
        return int(time.monotonic() * 1000)


def _cause_name():
    """把 reset_cause() 的数字翻译成可读名字，返回 (名字, 数字)"""
    if machine is None or not hasattr(machine, "reset_cause"):
        return "UNKNOWN", None
    try:
        code = machine.reset_cause()
    except Exception:
        return "UNKNOWN", None

    for name in dir(machine):
        if not name.endswith("_RESET"):
            continue
        try:
            if getattr(machine, name) == code:
                return name, code
        except Exception:
            continue
    return "UNKNOWN(%r)" % (code,), code


def capture():
    """记录本次启动的复位原因。**必须是上电后最早执行的动作之一。**"""
    global CAUSE_NAME, CAUSE_CODE, CAUSE_DESC, BOOT_MS
    CAUSE_NAME, CAUSE_CODE = _cause_name()
    CAUSE_DESC = _CAUSE_TEXT.get(CAUSE_NAME, "未识别的复位原因（可能是硬件异常）")
    BOOT_MS = _ticks_ms()
    return CAUSE_NAME


def uptime_ms():
    """本次启动已经运行了多久（毫秒）"""
    if not BOOT_MS:
        return 0
    return _ticks_ms() - BOOT_MS


def is_power_problem():
    """本次启动是不是"供电"问题引起的（欠压 / 反复冷启动）"""
    return CAUSE_NAME in ("BROWN_OUT_RESET", "PWRON_RESET")


# ---------------------------------------------------------------------------
# 上电安全态
# ---------------------------------------------------------------------------

def make_safe():
    """把电机 / 离合 / LED 全部写到"不上电"电平，返回被处理的 GPIO 列表。

    ⚠️ 必须在任何外设对象创建**之前**调用。
       松手不管的 GPIO 是浮空的，而 H 桥输入浮空 = 输出状态不确定。
    """
    if machine is None:
        return []

    done = []
    inactive = 1 - (1 if hardware_config.CLUTCH_ACTIVE_LEVEL else 0)

    targets = [
        (hardware_config.MOTOR_PIN_IN1, 0),     # H 桥 IN1 = 0
        (hardware_config.MOTOR_PIN_IN2, 0),     # H 桥 IN2 = 0
    ]
    for pin in hardware_config.CLUTCH_PINS:
        targets.append((pin, inactive))          # 离合 = 断开电平
    if hardware_config.LED_PIN is not None:
        targets.append((hardware_config.LED_PIN, 0))

    seen = set()
    for pin, level in targets:
        if pin in seen:
            continue
        seen.add(pin)
        try:
            machine.Pin(pin, machine.Pin.OUT).value(level)
            done.append(pin)
        except Exception:
            # 单个脚失败不能拖垮启动流程
            pass
    return done


# ---------------------------------------------------------------------------
# 启动计数
# ---------------------------------------------------------------------------

def _load_stat(path=BOOT_STAT_FILE):
    try:
        with open(path, "r") as handle:
            data = ujson.load(handle)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_stat(data, path=BOOT_STAT_FILE):
    try:
        with open(path, "w") as handle:
            ujson.dump(data, handle)
        return True
    except Exception:
        return False


def record_boot(path=BOOT_STAT_FILE):
    """启动次数 +1 并落盘。返回累计次数。

    次数疯涨 = 板子在反复复位，这是"一直在重启"最直接的证据。
    网页上可以一键清零。
    """
    global BOOT_COUNT
    data = _load_stat(path)
    try:
        count = int(data.get("count", 0)) + 1
    except Exception:
        count = 1
    data["count"] = count
    data["last_cause"] = CAUSE_NAME
    _save_stat(data, path)
    BOOT_COUNT = count
    return count


def clear_boot_count(path=BOOT_STAT_FILE):
    """把启动计数清零（网页上的「重置启动计数」按钮用）"""
    global BOOT_COUNT
    data = _load_stat(path)
    data["count"] = 0
    _save_stat(data, path)
    BOOT_COUNT = 0
    return 0


# ---------------------------------------------------------------------------
# 给串口日志 / 网页用的汇总
# ---------------------------------------------------------------------------

def boot_banner():
    """上电时打到串口的一段话"""
    lines = [
        "======== 上电自检 ========",
        "复位原因 : %s（%s）" % (CAUSE_NAME, CAUSE_DESC),
        "启动计数 : 第 %d 次（次数持续 +1 说明在反复复位）" % BOOT_COUNT,
    ]
    if is_power_problem():
        lines.append("★ 提示：连续出现冷启动/欠压复位，优先怀疑供电不足或引脚被拉低")
    lines.append("==========================")
    return "\n".join(lines)


def summary():
    """可 JSON 序列化，供 /status 返回给网页"""
    return {
        "cause": CAUSE_NAME,
        "cause_desc": CAUSE_DESC,
        "boot_count": BOOT_COUNT,
        "uptime_ms": uptime_ms(),
        "power_suspect": is_power_problem(),
    }
