"""
hardware_config.py —— 硬件引脚与运动参数集中配置
================================================

本文件是**唯一的硬件配置入口**，换硬件、换接线时只需要改这里。

--------------------------------------------------------------------------
一、硬件方案
--------------------------------------------------------------------------
    **1 个共享直流电机 + 4 路电磁离合（电磁离合器）**

                       ┌── 离合1 ──> 料盘位1 送料轮
        共享电机 ──────┼── 离合2 ──> 料盘位2 送料轮
        (H桥 IN1/IN2)  ├── 离合3 ──> 料盘位3 送料轮
                       └── 离合4 ──> 料盘位4 送料轮

    工作方式：
        需要哪个料盘位送料，就把该路的电磁离合吸合，让它的送料轮与电机主轴
        咬合，然后电机正转（进料）或反转（退料），动作完成后立刻断开离合。

    相比"每个料盘位一个电机"的老方案：
        老方案：4 个电机 + 8 个 GPIO（每路 H 桥 2 个 IO）
        新方案：1 个电机 + 4 路离合 + 6 个 GPIO（电机 2 个 + 离合 4 个）

    ⚠️ 硬性约束：**任何时刻最多只能有 1 路电磁离合处于吸合状态。**
       如果有 2 路同时吸合，电机会同时拖动两个料盘位的送料轮，
       两卷料互相拉扯 → 料线绷断、打滑、打印机报错。
       这条约束由 motor_clutch.FilamentMotorBus 在软件层强制保证，
       并且在启动、每次吸合前、主循环体检时反复校验。

--------------------------------------------------------------------------
二、ESP32-C3 引脚约束（重要！不要随意改）
--------------------------------------------------------------------------
    ESP32-C3 一共只有 GPIO0 ~ GPIO21 共 22 个可用 IO，其中相当一部分不能随便用：

      GPIO11 ~ GPIO17 : 模组内置 SPI Flash 的 SPI0/1 接口，**不可用**
                        （VDD_SPI 也在这组里）
      GPIO18 / GPIO19 : USB D- / D+（接了 USB 座就不能当普通 IO）
      GPIO20 / GPIO21 : UART0 RX / TX，默认日志与 REPL 口
      GPIO2 / GPIO3 / GPIO8 / GPIO9 : strapping 引脚，上电瞬间的电平决定启动模式
      GPIO4 ~ GPIO7   : JTAG 调试口，作普通 IO 可以用，但会占用 JTAG

    真正"干净"、可以安全当作**输出**用的引脚只有：
        GPIO0、GPIO1、GPIO4、GPIO5、GPIO6、GPIO7、GPIO10

--------------------------------------------------------------------------
★★★ 为什么 strapping 脚绝对不能拿来驱动电机 / 离合 ★★★
--------------------------------------------------------------------------
    这一节是拿真机踩出来的，改接线前**务必先读完**。

    1) ESP32-C3 上参与 Boot 模式采样的其实是 **四个** 脚：
           GPIO2、GPIO3、GPIO8、GPIO9
       （见《ESP32-C3 技术参考手册》第 7 章「芯片 Boot 控制」表 7.2-1：
        "复位释放后，GPIO2、GPIO3、GPIO8 和 GPIO9 共同控制 Boot 模式"）
       ⚠️ 注意 **GPIO3 也是 strapping 脚**！它和 GPIO2 一起决定是否进入
          SPI Download Boot 模式。本文件早期版本只把 GPIO2/8/9 当 strapping，
          漏掉了 GPIO3，那是错的，validate() 已经补上。

    2) 这四个脚里**只有 GPIO9 带内部弱上拉**。
       GPIO2、GPIO3、GPIO8 复位后的默认状态是 **浮空**（官方 datasheet
       表 4-1 写得很明确：GPIO2 = Floating，GPIO8 = Floating）。
       也就是说这两个脚在上电瞬间没有任何东西把它们拉到高电平，
       外部接什么，它就是什么。

    3) AT8236 的 IN1 / IN2 是"逻辑输入，**内置下拉电阻**"（数据手册管脚表原文）。
       ULN2803 的输入是达林顿基极，同样呈低阻。
       把这类负载挂到 GPIO2 / GPIO3 上，上电瞬间这两个脚会被**拉低**。

    4) GPIO2 / GPIO3 被拉低 → 芯片采样到的不是"正常启动"的组合 →
       典型现象就是：**反复复位、串口日志来回刷、Wi-Fi 和网页永远起不来**；
       同时每次复位后固件都会重新驱动一遍引脚，H 桥就跟着"叫"。

    5) 还有一条更直接的：GPIO2 上挂着 1kHz 的状态灯 PWM
       （见 AMS_WEB.py 里的 PWM(Pin(LED_PIN))）。
       如果 IN1 也接在 GPIO2 上，电机输入就变成"跟着状态灯闪"：
           · MQTT 已连  → duty(1000) 常亮 → 电机**长鸣**
           · 只连上 WiFi → 0.5s 亮 / 0.5s 灭 → 电机**间隔响**
       这个"一会长鸣、一会间隔响"的规律，就是状态灯在驱动 H 桥的铁证。

    结论：**电机 IN1/IN2、电磁离合、状态灯，一律只能用上面那 7 只干净的脚。**
          GPIO2 只留给板载 LED（LED 在低电平时不导通，对 strapping 影响很小）。
          GPIO3 目前被 4 号离合占着，能启动但属于"带病运行"，
          建议尽早把 4 号离合挪到 GPIO1（见下面 CLUTCH_PINS 的注释）。

    注意：老版本代码里的 GPIO22 / GPIO23 是经典 ESP32（38 脚）的编号，
    ESP32-C3 上**并不存在**这两个引脚，必须改掉。

--------------------------------------------------------------------------
三、默认接线表（按需修改下面的常量）
--------------------------------------------------------------------------
      ┌────────────────┬──────────┬──────────────────────────────────┐
      │ 信号           │ GPIO     │ 说明                             │
      ├────────────────┼──────────┼──────────────────────────────────┤
      │ 电机 H桥 IN1   │ GPIO4    │ 方向 1 = 进料（正转）            │
      │ 电机 H桥 IN2   │ GPIO5    │ 方向 -1 = 退料（反转）           │
      │ 电磁离合 1     │ GPIO6    │ 料盘位 1                         │
      │ 电磁离合 2     │ GPIO7    │ 料盘位 2                         │
      │ 电磁离合 3     │ GPIO10   │ 料盘位 3                         │
      │ 电磁离合 4     │ GPIO3    │ 料盘位 4（⚠️ strapping 脚，见下）│
      │ 状态 LED       │ GPIO2    │ 板载蓝灯（⚠️ strapping 脚，见下）│
      │ 到位开关 1~4   │ 未安装   │ 预留，见第四节                   │
      └────────────────┴──────────┴──────────────────────────────────┘

    ⚠️ 电机为什么必须留在 GPIO4 / GPIO5：
        曾经试过把 IN1/IN2 改到 GPIO2 / GPIO3（看着像两只"空脚"），
        结果 AT8236 输入级的内置下拉把这两个 strapping 脚拉低，
        芯片根本进不了正常启动模式 —— 反复复位、网页打不开、电机一直叫。
        这两只脚**不是空脚**，是启动模式选择脚，不能拿来驱动负载。

    关于状态 LED 用 GPIO2：
        板载 LED 在低电平时不导通、呈高阻，对 strapping 影响很小，
        所以"LED 挂在 GPIO2"本身可以接受。
        但它绝不能和任何功率器件共用 GPIO2 —— 1kHz 的 LED PWM
        会把 H 桥当灯闪，电机就"一会长鸣、一会间隔响"。
        如果要把 LED 挪走，改 LED_PIN 为 0 或 1 即可；
        不想要状态灯就把 LED_PIN 设成 None，代码会自动跳过。

    关于电磁离合的硬件注意（**强烈建议**）：
        1. 电磁离合是感性负载，线圈两端**必须**反向并联续流二极管
           （1N4148 / 1N5819 均可），否则断开瞬间的高压反电动势
           会把 GPIO 或驱动管打坏。
        2. GPIO 输出电流只有 20mA 左右，**不要用 GPIO 直接驱动离合线圈**，
           中间要加三极管（S8050 / 2N2222）、MOS 管（AO3400 / IRLZ44N）
           或光耦隔离的驱动板。三极管方案要把基极串 1kΩ 电阻。
        3. 4 路离合不建议同时吸合的原因除了机械问题，还有供电问题：
           4 路线圈同时吸合的浪涌电流很容易把 5V 电源拉塌，
           进而导致 ESP32 复位。软件层的互斥约束同时也保护了电源。

--------------------------------------------------------------------------
四、到位开关（限位/微动开关）—— 当前未安装，接口已预留
--------------------------------------------------------------------------
    到位开关的作用：
        · now_filament()  —— 探测"当前正在用的是哪个料盘位"
        · fileament_move() —— 判断料有没有真的推动，是否卡料/到底

    探测原理（老代码的原始设计意图）：
        当前料盘位里有料，反向转动时料线绷紧，会把送料臂/浮动轮推到位，
        触发行程开关；空料盘位反转时轮子空转，开关不触发。

    当前没有装开关，配置为 None，程序会自动进入**降级模式**：
        · 不再探测当前料盘，直接沿用 config.json 里持久化的 filament_current
        · 送料/退料改为按时间推进，时长由第五节的 NO_LIMIT_* 参数决定
        · 这里的时长**必须按实机实测调整**，否则会送料不足或过冲

    以后装了开关，只要把引脚填进 LIMIT_SWITCH_PINS 即可自动启用探测逻辑。
    推荐接法（开关一端接 GPIO、另一端接 GND，内部上拉，低电平触发）：
        · 通道 1 / 2 ：建议 GPIO0、GPIO1（最干净）
        · 通道 3 / 4 ：可用 GPIO2、GPIO3、GPIO8 或 GPIO9
          —— 这四个是 strapping 脚，但"内部上拉 + 开关对地"的接法
             空闲时正好是高电平，符合 strapping 要求，因此作**输入**是安全的。
             （注意：作**输出**就危险了，见第二节。）
"""

# ==========================================================================
# 零、Strapping（启动模式）引脚 —— 只读，不要改
# ==========================================================================
# 上电瞬间这几个脚的电平决定芯片从哪儿启动。作**输入**（内部上拉+开关对地）
# 是安全的；作**输出**（电机 / 离合 / LED）会把它们在上电瞬间拉低，
# 导致芯片进不了正常启动模式，表现为反复复位 + 网页打不开 + 电机乱叫。
#
# 依据：《ESP32-C3 技术参考手册》第 7 章「芯片 Boot 控制」表 7.2-1
#       —— 复位释放后 GPIO2、GPIO3、GPIO8、GPIO9 共同控制 Boot 模式。
#       （datasheet 表 4-1：GPIO2/GPIO8 默认浮空，只有 GPIO9 带内部弱上拉）
STRAPPING_PINS = (2, 3, 8, 9)

# 可以安全用作**输出**的引脚（避开 Flash 11~17 / USB 18~19 / UART 20~21 / strapping）
SAFE_OUTPUT_PINS = (0, 1, 4, 5, 6, 7, 10)

# ==========================================================================
# 一、共享电机（H 桥两线：只有方向，没有调速）
# ==========================================================================
# ⚠️⚠️ 这两个脚**不要**改成 GPIO2 / GPIO3！
#      AT8236 的 IN1/IN2 内置下拉电阻，会把 strapping 脚在上电瞬间拉低，
#      芯片将无法进入正常启动模式 —— 症状就是"接了负载后一直重启、
#      网页打不开、电机一直有电流声"。
#      真要换脚，只能从 SAFE_OUTPUT_PINS 里挑（推荐 GPIO0 / GPIO1）。
MOTOR_PIN_IN1 = 4      # H 桥 IN1，方向 1 = 进料（正转）
MOTOR_PIN_IN2 = 5      # H 桥 IN2，方向 -1 = 退料（反转）
MOTOR_DEAD_TIME_MS = 30  # 换向死区：先双脚拉低再换向，防止 H 桥上下管直通烧毁
MOTOR_BOOT_SETTLE_MS = 200  # 上电后等电源稳定再做任何电机动作（防欠压复位）

# ==========================================================================
# 二、电磁离合（4 路，与 4 个料盘位一一对应）
# ==========================================================================
# 通道 4 落在 GPIO3 上，而 GPIO3 是 strapping 脚（见文件头部第二节）。
# ULN2803 的输入是达林顿基极，需要约 1.4V 才导通，空闲时接近高阻，
# 所以现在这样能用，但属于"带病运行"—— 一旦换用输入带下拉的驱动板，
# 就会和电机踩 GPIO2/GPIO3 一样起不来。建议尽早改成：
#     CLUTCH_PINS = (6, 7, 10, 1)
# 只剩 GPIO0 可用，留给状态灯或以后的到位开关。
CLUTCH_PINS = (6, 7, 10, 3)  # 料盘位 1/2/3/4 对应的电磁离合 GPIO
CLUTCH_ACTIVE_LEVEL = 1      # 高电平吸合；如果你的驱动板是低电平吸合，改成 0
CLUTCH_ENGAGE_MS = 80        # 吸合后的机械稳定等待（离合咬合需要时间）
CLUTCH_RELEASE_MS = 60       # 断开后的机械稳定等待
CLUTCH_SETTLE_MS = 20        # 从一路切到另一路时的额外静默时间

# ==========================================================================
# 三、到位开关（限位/微动开关）—— 现在未安装
# ==========================================================================
# 每个料盘位一个开关，None 表示该通道未安装。例：LIMIT_SWITCH_PINS = (0, 1, 2, 8)
LIMIT_SWITCH_PINS = (None, None, None, None)
LIMIT_SWITCH_ACTIVE_LEVEL = 0  # 低电平触发（内部上拉 + 开关对地）
LIMIT_SWITCH_PULL_UP = True    # True = 内部上拉（配合低电平触发）；改 False 需同步改 ACTIVE_LEVEL

# ==========================================================================
# 四、送料动作参数
# ==========================================================================
FILAMENT_STEP_MS = 500      # 单步推送时长（对应老代码里 dianji_roll 的 times_ms）
RETRACT_STEPS = 15          # 退料最多推几步（有到位开关时，触发即提前结束）
LOAD_RETRY_TIMES = 10       # 进料最多重试几轮（有到位开关时，到位即提前结束）
LOAD_ASSIST_MS = 1000       # 打印机拉料时的辅助送料时长

# ---- 降级模式（未安装到位开关）下的动作时长上限，单位 ms ----
#      ⚠️ 这两个值必须按你的送料机构实测调整：
#         太小 → 料没送到挤出机，打印机报"耗材缺失"
#         太大 → 料被顶弯、料在缓冲区堆积、甚至顶坏挤出机
NO_LIMIT_RETRACT_MS = 6000   # 退料：把料从挤出机收回缓冲区
NO_LIMIT_LOAD_MS = 8000      # 进料：把料从料盘推到挤出机
NO_LIMIT_PROBE_MS = 4000     # 探测当前料盘时每通道的最大反转时间

# ==========================================================================
# 五、指示灯
# ==========================================================================
# 板载蓝灯。GPIO2 是 strapping 脚，但 LED 在低电平时不导通、呈高阻，
# 对启动影响很小，所以可以继续用。**但绝不能让 IN1 之类的功率信号共用它。**
# 不想要状态灯（或要把 GPIO2 彻底让出来）就设成 None，代码会自动跳过。
LED_PIN = 2  # 板载蓝灯；None = 不用状态灯

# ==========================================================================
# 六、配置文件
# ==========================================================================
CONFIG_FILE = "config.json"  # 持久化 wifi / mqtt / 通道映射 / 当前料盘

# ==========================================================================
# 自检：把上面这些常量本身也检查一遍，配置写错时上电就报出来
# ==========================================================================

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

    # ---- ESP32-C3 保留引脚 ----
    reserved = {}
    for pin in range(11, 18):
        reserved[pin] = "内置 SPI Flash (SPI0/1)"
    reserved[18] = "USB D-"
    reserved[19] = "USB D+"
    reserved[20] = "UART0 RX"
    reserved[21] = "UART0 TX"

    for name, pin in used.items():
        if pin is None:
            continue
        if not isinstance(pin, int) or pin < 0 or pin > 21:
            errors.append("GPIO%s（%s）超出 ESP32-C3 的 GPIO0~GPIO21 范围" % (pin, name))
        elif pin in reserved:
            errors.append("GPIO%d（%s）被 %s 占用，不要用" % (pin, name, reserved[pin]))

    # ---- strapping 引脚 ----
    # 为什么电机是"错误"，离合/LED 只是"警告"？看驱动级的输入阻抗：
    #   · AT8236 的 IN1/IN2 **内置下拉电阻**（数据手册管脚表原文），
    #     上电瞬间会把 GPIO2/GPIO3 实实在在拉低 → 启动模式被改掉 → 复位循环
    #   · ULN2803 的输入是达林顿基极，要 1.4V 以上才导通，空闲时接近高阻，
    #     等于把引脚"悬空"，而 GPIO2/GPIO3/GPIO8 的官方默认状态本来就是浮空
    #   · LED 在低电平时不导通，同样是高阻
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
                "建议尽早改到 %s 里的引脚" % (pin, name, list(SAFE_OUTPUT_PINS)))

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
    lines.append("共享电机 : IN1=GPIO%d  IN2=GPIO%d  换向死区=%dms"
                 % (MOTOR_PIN_IN1, MOTOR_PIN_IN2, MOTOR_DEAD_TIME_MS))
    for index, pin in enumerate(CLUTCH_PINS, 1):
        lines.append("电磁离合%d: GPIO%d  (%s吸合, 吸合等待%dms)"
                     % (index, pin, "高电平" if CLUTCH_ACTIVE_LEVEL else "低电平", CLUTCH_ENGAGE_MS))
    for index, pin in enumerate(LIMIT_SWITCH_PINS, 1):
        lines.append("到位开关%d: %s" % (index, "GPIO%d" % pin if pin is not None else "未安装"))
    lines.append("状态 LED : %s" % ("GPIO%d" % LED_PIN if LED_PIN is not None else "未使用"))
    return "\n".join(lines)


def boot_safety_report():
    """上电自检：返回 (是否安全, 多行文本)，boot.py 和主程序启动时各调一次。"""
    errors, warnings = validate_detail()
    lines = [describe()]
    if errors:
        lines.append("!! 引脚配置有 %d 处错误，硬件不应被启用：" % len(errors))
        for item in errors:
            lines.append("   × " + item)
    for item in warnings:
        lines.append("   ! " + item)
    if not errors and not warnings:
        lines.append("引脚配置自检通过：全部输出脚都在安全引脚上。")
    return (len(errors) == 0, "\n".join(lines))
