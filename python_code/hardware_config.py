"""
hardware_config.py —— 硬件配置入口（板型自动选择 + 运动参数）
=============================================================

本文件是**唯一的硬件配置入口**。它分三部分：

    一、板型选择 —— 同一个固件要同时支持 ESP32-C3 和 ESP32-S3，
        引脚能力差异放在两个独立文件里：
            board_c3.py  ← C3 的引脚表 / 保留脚 / strapping
            board_s3.py  ← S3 的引脚表 / 保留脚 / strapping / 剩余可用 IO
        本文件负责"认出当前是哪块板"，然后把对应引脚表导出来，
        这样上层代码（motor_clutch / AMS_MODEL / reset_info …）
        只管 `from hardware_config import MOTOR_PIN_IN1`，一行都不用改。

    二、业务参数 —— 动作时长、离合等待、点动范围、降级模式时长…
        这些和芯片无关，改接线不用动它们。

    三、自检 —— 引脚合法性、重复占用、strapping 误用，上电就报。

--------------------------------------------------------------------------
一、板型是怎么定下来的（三条路，优先级从高到低）
--------------------------------------------------------------------------
    1. 构建时写死的 `board_select.py`：
           BOARD = "c3" | "s3" | "auto"
       tools/build_mpy.py --board s3 会生成这一行，
       所以 CI 打出来的两个固件各自"天生长在对应的板子上"。

    2. 现场覆盖文件 `board_override.py`（不进 git，可选）：
           放在板子上就能覆盖本文件里的任何引脚常量，例如
               CLUTCH_PINS = (6, 7, 10, 1)
               LED_PIN = None
       适合"手上这块板子接线和别人不一样"的情况，不用改仓库代码。

    3. 自动识别：`os.uname().machine`
           "ESP32S3 module with ESP32S3" → S3
           "ESP32C3 module with ESP32C3" → C3
       认不出来时**默认 C3**（并在自检里提示）。

--------------------------------------------------------------------------
★★★ 为什么必须有 S3 这条路（实测结论，不是拍脑袋）★★★
--------------------------------------------------------------------------
    C3 只有 400KB SRAM。本应用加载完之后空闲堆约 60KB，
    而 ESP32 的 WiFi 驱动收发数据要动态申请**连续**内存做缓冲：

        未加载应用（空闲 149KB）：65KB 的 index.html 5.7 秒发完，链路正常
        加载应用后（空闲  60KB）：第一个 TCP 分段就 OSError(113)
                                  （重传 9.4 秒后放弃，随后射频卡死，只能复位）

    表现就是"网页一直转圈/打不开"，而且加上 OTA 之后应用更大了，
    问题从"偶发"变成"必然"。S3 的可用堆大得多，这条路才走得通。

    所以：C3 是"能跑但网页很勉强"，要稳就用 S3（见 board_s3.py）。

--------------------------------------------------------------------------
二、硬件方案（两块板通用）
--------------------------------------------------------------------------
    **1 个共享直流电机 + 4 路电磁离合**

                       ┌── 离合1 ──> 料盘位1 送料轮
        共享电机 ──────┼── 离合2 ──> 料盘位2 送料轮
        (H桥 IN1/IN2)  ├── 离合3 ──> 料盘位3 送料轮
                       └── 离合4 ──> 料盘位4 送料轮

    需要哪个料盘位送料，就把该路离合吸合让送料轮与电机主轴咬合，
    然后电机正转（进料）/ 反转（退料），动作完成立刻断开离合。

    ⚠️ 硬性约束：**任何时刻最多只能有 1 路电磁离合吸合。**
       两路同时吸合会让两卷料互相拉扯 → 料线绷断、打滑。
       这条约束由 motor_clutch.FilamentMotorBus 在软件层四重强制。

--------------------------------------------------------------------------
三、离合驱动的硬件注意（**强烈建议**照做）
--------------------------------------------------------------------------
    1. 离合是感性负载，线圈两端必须反向并联续流二极管（1N4148 / 1N5819）。
    2. GPIO 只能出 20mA，**不要**直接驱动线圈，中间要加三极管
       （S8050 / 2N2222）、MOS（AO3400 / IRLZ44N）或光耦驱动板；
       三极管方案基极串 1kΩ。
    3. 4 路不要同时吸合，除了机械原因还有供电：浪涌很容易把 5V 拉塌
       导致 ESP32 复位。软件互斥同时也保护了电源。

--------------------------------------------------------------------------
四、到位开关（限位/微动开关）—— 当前未安装，接口已预留
--------------------------------------------------------------------------
    作用：now_filament() 探测当前料盘位；fileament_move() 判断是否卡料/到底。
    原理：该料盘有料时反转会把送料臂推到位触发开关，空盘反转只空转。

    现在配置为 None，程序自动进入**降级模式**：
        · 不探测当前料盘，沿用 config.json 里的 filament_current
        · 送料/退料按时间推进，时长见下面 NO_LIMIT_*（**必须实测调整**）

    装开关后把引脚填进 LIMIT_SWITCH_PINS 即可自动启用探测。
    推荐接法（一端接 GPIO、一端接 GND，内部上拉，低电平触发）：
        · C3：用 0 / 1 / 8 / 9（strapping 脚作输入是安全的）
        · S3：用 11~14（全干净，排针集中）
"""

# ==========================================================================
# 零、选板型
# ==========================================================================
# 1) 构建时写死的选择（tools/build_mpy.py --board 会生成这个文件）
try:
    from board_select import BOARD as _BUILT_BOARD
except ImportError:
    _BUILT_BOARD = "auto"


def _machine_name():
    try:
        import os

        return os.uname().machine.upper()
    except Exception:
        return ""


def _detect_board():
    """返回 "c3" 或 "s3"，以及判定依据（给自检打印用）。"""
    if _BUILT_BOARD in ("c3", "s3"):
        return _BUILT_BOARD, "构建时指定（board_select.py）"

    machine = _machine_name()
    if "S3" in machine:
        return "s3", "自动识别 os.uname().machine=%r" % machine
    if "C3" in machine:
        return "c3", "自动识别 os.uname().machine=%r" % machine
    return "c3", "认不出芯片（machine=%r），按 C3 处理" % machine


BOARD_ID, BOARD_SOURCE = _detect_board()

if BOARD_ID == "s3":
    import board_s3 as _board
else:
    import board_c3 as _board

BOARD_NAME = _board.BOARD_NAME
CHIP = _board.CHIP
GPIO_MAX = _board.GPIO_MAX
STRAPPING_PINS = _board.STRAPPING_PINS
SAFE_OUTPUT_PINS = _board.SAFE_OUTPUT_PINS
RESERVED_PINS = _board.RESERVED_PINS
SPARE_PINS = _board.SPARE_PINS
STRAPPING_SPARE_PINS = getattr(_board, "STRAPPING_SPARE_PINS", ())
RECOMMENDED_LIMIT_SWITCH_PINS = getattr(_board, "RECOMMENDED_LIMIT_SWITCH_PINS", ())
BOARD_NOTES = _board.BOARD_NOTES

# ---- 引脚默认值（下面可能被 board_override.py 覆盖）----
MOTOR_PIN_IN1 = _board.MOTOR_PIN_IN1
MOTOR_PIN_IN2 = _board.MOTOR_PIN_IN2
CLUTCH_PINS = tuple(_board.CLUTCH_PINS)
LED_PIN = _board.LED_PIN
LIMIT_SWITCH_PINS = tuple(_board.LIMIT_SWITCH_PINS)

# ---- 现场覆盖（可选）----
# 板子上放一个 board_override.py 就能改引脚，不用动仓库代码。可覆盖的常量：
#     MOTOR_PIN_IN1 / MOTOR_PIN_IN2 / CLUTCH_PINS / LED_PIN / LIMIT_SWITCH_PINS
#     CLUTCH_ACTIVE_LEVEL / LIMIT_SWITCH_ACTIVE_LEVEL / LIMIT_SWITCH_PULL_UP
#     FILAMENT_STEP_MS / NO_LIMIT_* / JOG_* …（任何本文件里的名字都行）
_OVERRIDABLE = (
    "MOTOR_PIN_IN1", "MOTOR_PIN_IN2", "CLUTCH_PINS", "LED_PIN",
    "LIMIT_SWITCH_PINS", "CLUTCH_ACTIVE_LEVEL", "LIMIT_SWITCH_ACTIVE_LEVEL",
    "LIMIT_SWITCH_PULL_UP", "FILAMENT_STEP_MS", "NO_LIMIT_LOAD_MS",
    "NO_LIMIT_RETRACT_MS", "NO_LIMIT_PROBE_MS", "JOG_TIME_MS",
    "MOTOR_DEAD_TIME_MS", "MOTOR_BOOT_SETTLE_MS", "CLUTCH_ENGAGE_MS",
    "CLUTCH_RELEASE_MS", "CLUTCH_SETTLE_MS",
)
OVERRIDE_APPLIED = []
try:
    import board_override as _override
except ImportError:
    _override = None

if _override is not None:
    for _name in _OVERRIDABLE:
        _value = getattr(_override, _name, None)
        if _value is not None:
            globals()[_name] = _value
            OVERRIDE_APPLIED.append(_name)

# ==========================================================================
# 一、共享电机（H 桥两线：只有方向，没有调速）
# ==========================================================================
MOTOR_DEAD_TIME_MS = 30      # 换向死区：先双脚拉低再换向，防 H 桥上下管直通
MOTOR_BOOT_SETTLE_MS = 200   # 上电后等电源稳定再做电机动作（防欠压复位）

# ==========================================================================
# 二、电磁离合（4 路，与 4 个料盘位一一对应）
# ==========================================================================
CLUTCH_ACTIVE_LEVEL = 1      # 高电平吸合；驱动板是低电平吸合就改 0
CLUTCH_ENGAGE_MS = 80        # 吸合后的机械稳定等待（离合咬合需要时间）
CLUTCH_RELEASE_MS = 60       # 断开后的机械稳定等待
CLUTCH_SETTLE_MS = 20        # 从一路切到另一路时的额外静默时间

# ==========================================================================
# 三、到位开关（限位/微动开关）—— 现在未安装
# ==========================================================================
LIMIT_SWITCH_ACTIVE_LEVEL = 0  # 低电平触发（内部上拉 + 开关对地）
LIMIT_SWITCH_PULL_UP = True    # True = 内部上拉；改 False 需同步改 ACTIVE_LEVEL

# ==========================================================================
# 四、送料动作参数
# ==========================================================================
FILAMENT_STEP_MS = 500      # 单步推送时长
RETRACT_STEPS = 15          # 退料最多推几步（有到位开关时触发即停）
LOAD_RETRY_TIMES = 10       # 进料最多重试几轮（有到位开关时到位即停）
LOAD_ASSIST_MS = 1000       # 打印机拉料时的辅助送料时长

# ---- 降级模式（未安装到位开关）下的动作时长上限，单位 ms ----
#      ⚠️ 必须按你的送料机构实测调整：
#         太小 → 料没送到挤出机，打印机报"耗材缺失"
#         太大 → 料被顶弯、缓冲堆积、甚至顶坏挤出机
NO_LIMIT_RETRACT_MS = 6000   # 退料：把料从挤出机收回缓冲区
NO_LIMIT_LOAD_MS = 8000      # 进料：把料从料盘推到挤出机
NO_LIMIT_PROBE_MS = 4000     # 探测当前料盘时每通道的最大反转时间

# ---- 网页「硬件调试」里手动点动的时长（进退响应时间），单位 ms ----
#      只是出厂默认值，网页改过之后以 config.json 的 jog_ms 为准。
JOG_TIME_MS = 1000           # 默认按一次转 1 秒
JOG_MIN_MS = 200             # 下限：再短离合还没咬合就停了，没意义
JOG_MAX_MS = 60000           # 上限：防止手滑把关在里面的料顶坏

# ==========================================================================
# 六、配置文件
# ==========================================================================
CONFIG_FILE = "config.json"  # 持久化 wifi / mqtt / 通道映射 / 当前料盘

# ==========================================================================
# 自检：把上面这些常量本身也检查一遍，配置写错时上电就报出来
# ==========================================================================
# 为什么电机是"错误"、离合/LED 只是"警告"？看驱动级的输入阻抗：
#   · AT8236 的 IN1/IN2 **内置下拉电阻**（数据手册管脚表原文），
#     上电瞬间会把 strapping 脚实实在在拉低 → 启动模式被改 → 复位循环
#   · ULN2803 输入是达林顿基极，要 1.4V 以上才导通，空闲时接近高阻
#   · LED 在低电平时不导通，同样是高阻
_DRIVER_HINT = {
    "c3": "C3 上建议从 %s 里挑（推荐 GPIO0 / GPIO1）",
    "s3": "S3 上建议从 %s 里挑",
}


def output_pins():
    """所有会**主动驱动电平**的引脚：{角色名: GPIO}"""
    pins = {
        "电机 IN1": MOTOR_PIN_IN1,
        "电机 IN2": MOTOR_PIN_IN2,
    }
    for index, pin in enumerate(CLUTCH_PINS, 1):
        pins["电磁离合%d" % index] = pin
    pins["状态 LED"] = LED_PIN
    return pins


def input_pins():
    """所有只做输入的引脚：{角色名: GPIO}（未安装的通道不出现）"""
    pins = {}
    for index, pin in enumerate(LIMIT_SWITCH_PINS, 1):
        if pin is not None:
            pins["到位开关%d" % index] = pin
    return pins


def board_info():
    """一行板型摘要，方便打日志 / 显示在网页上。"""
    return "%s  芯片=%s  引脚上限=GPIO%d" % (BOARD_NAME, CHIP, GPIO_MAX)


def validate_detail():
    """完整校验，返回 (错误列表, 警告列表)。

    两个级别的区别很重要：
        · 错误 —— 真的会导致起不来/进不了启动模式，固件应当拒绝启用硬件
        · 警告 —— 现在能用，但属于"带病运行"，上电日志里提示一下
    """
    errors = []
    warnings = []

    outputs = output_pins()
    inputs = input_pins()
    used = {}
    used.update(outputs)
    used.update(inputs)

    if MOTOR_PIN_IN1 == MOTOR_PIN_IN2:
        errors.append("电机 IN1 与 IN2 不能是同一个引脚")

    # ---- 引脚重复检查 ----
    seen = {}
    for name, pin in used.items():
        if pin is None:
            continue
        if pin in seen:
            errors.append("引脚 GPIO%d 被 %s 和 %s 同时占用" % (pin, seen[pin], name))
        seen[pin] = name

    # ---- 范围与保留脚（按当前板型判断，不再写死 C3 的 11~21）----
    for name, pin in used.items():
        if pin is None:
            continue
        if not isinstance(pin, int) or isinstance(pin, bool) or pin < 0 or pin > GPIO_MAX:
            errors.append("GPIO%s（%s）超出 %s 的 GPIO0~GPIO%d 范围"
                          % (pin, name, BOARD_NAME, GPIO_MAX))
        elif pin in RESERVED_PINS:
            errors.append("GPIO%d（%s）被 %s 占用，不要用"
                          % (pin, name, RESERVED_PINS[pin]))

    # ---- strapping 引脚 ----
    for name, pin in outputs.items():
        if pin not in STRAPPING_PINS:
            continue
        if name.startswith("电机"):
            errors.append(
                "GPIO%d（%s）是 strapping 启动模式脚，绝对不能做电机输出！"
                "AT8236 的 IN1/IN2 内置下拉电阻，上电瞬间会把它拉低，"
                "芯片将进不了正常启动模式（现象：反复复位、网页打不开、电机一直叫）。"
                "请改到 %s 里的引脚" % (pin, name, list(SAFE_OUTPUT_PINS)))
        else:
            warnings.append(
                "GPIO%d（%s）是 strapping 启动模式脚，当前驱动级空闲时呈高阻，"
                "所以能用；但如果换用输入带下拉的驱动板，就会起不来。"
                "建议改到 %s 里的引脚" % (pin, name, list(SAFE_OUTPUT_PINS)))

    for name, pin in inputs.items():
        if pin in STRAPPING_PINS:
            warnings.append(
                "GPIO%d（%s）是 strapping 脚，作输入（内部上拉 + 开关对地）"
                "空闲时为高电平，是安全的；但接线必须保证空闲就是高" % (pin, name))

    # ---- 数量与动作参数 ----
    if len(CLUTCH_PINS) != len(LIMIT_SWITCH_PINS):
        errors.append("CLUTCH_PINS 有 %d 路，LIMIT_SWITCH_PINS 有 %d 路，数量必须一致"
                      % (len(CLUTCH_PINS), len(LIMIT_SWITCH_PINS)))

    if NO_LIMIT_LOAD_MS <= 0 or NO_LIMIT_RETRACT_MS <= 0:
        errors.append("降级模式的动作时长 NO_LIMIT_* 必须大于 0")

    return errors, warnings


def validate():
    """校验引脚配置，返回 (是否通过, 问题列表)。

    只有**错误**才会让"是否通过"变成 False；警告会一并返回，但带"警告："前缀。
    """
    errors, warnings = validate_detail()
    return (len(errors) == 0, errors + ["警告：%s" % w for w in warnings])


def describe():
    """返回一份人类可读的接线表，方便上电自检时打印到串口"""
    lines = ["---- 硬件配置 ----"]
    lines.append("开发板   : %s" % BOARD_NAME)
    lines.append("板型来源 : %s%s"
                 % (BOARD_SOURCE, "（已应用 board_override.py）" if OVERRIDE_APPLIED else ""))
    lines.append("共享电机 : IN1=GPIO%d  IN2=GPIO%d  换向死区=%dms"
                 % (MOTOR_PIN_IN1, MOTOR_PIN_IN2, MOTOR_DEAD_TIME_MS))
    for index, pin in enumerate(CLUTCH_PINS, 1):
        lines.append("电磁离合%d: GPIO%d  (%s吸合, 吸合等待%dms)"
                     % (index, pin, "高电平" if CLUTCH_ACTIVE_LEVEL else "低电平", CLUTCH_ENGAGE_MS))
    for index, pin in enumerate(LIMIT_SWITCH_PINS, 1):
        lines.append("到位开关%d: %s" % (index, "GPIO%d" % pin if pin is not None else "未安装"))
    lines.append("状态 LED : %s" % ("GPIO%d" % LED_PIN if LED_PIN is not None else "未使用"))
    lines.append("剩余可用 : %s"
                 % (", ".join("GPIO%d" % p for p in SPARE_PINS) if SPARE_PINS else "无"))
    if STRAPPING_SPARE_PINS:
        lines.append("剩余可用(strapping,仅建议作输入): %s"
                     % ", ".join("GPIO%d" % p for p in STRAPPING_SPARE_PINS))
    return "\n".join(lines)


def boot_safety_report():
    """上电自检：返回 (是否安全, 多行文本)，boot.py 和主程序启动时各调一次。"""
    errors, warnings = validate_detail()
    lines = [describe()]
    for note in BOARD_NOTES:
        lines.append("   i " + note)
    if errors:
        lines.append("!! 引脚配置有 %d 处错误，硬件不应被启用：" % len(errors))
        for item in errors:
            lines.append("   × " + item)
    for item in warnings:
        lines.append("   ! " + item)
    if not errors and not warnings:
        lines.append("引脚配置自检通过：全部输出脚都在安全引脚上。")
    return (len(errors) == 0, "\n".join(lines))
