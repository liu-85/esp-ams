# YAO_AMS

**基于 YBA-AMS 改进的拓竹打印机自动换料系统（ESP32-C3 + MicroPython）**

<img src="./assets/83bc5bb869cc607bf0961988dcada98.jpg" alt="YAO_AMS 整机" style="zoom:40%;" />

---

## 目录

- [简介](#简介)
- [功能特性](#功能特性)
- [硬件方案](#硬件方案)
  - [1 个电机 + 4 路电磁离合](#1-个电机--4-路电磁离合)
  - [ESP32-C3 引脚约束](#esp32-c3-引脚约束)
  - [默认接线表](#默认接线表)
  - [电气注意事项](#电气注意事项)
  - [物料清单](#物料清单)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [换料时序与软件架构](#换料时序与软件架构)
- [联网与配网](#联网与配网)
- [硬件配置项](#硬件配置项)
- [调试](#调试)
- [GitHub Actions 自动编译与发布](#github-actions-自动编译与发布)
- [本地开发与自测](#本地开发与自测)
- [常见问题](#常见问题)
- [附录 A：部分 MQTT 命令说明](#附录-a部分-mqtt-命令说明)
- [附录 B：G-code 参考](#附录-bg-code-参考)
- [致谢与许可](#致谢与许可)

---

## 简介

本项目是适配拓竹（Bambu Lab）打印机的 AMS 自动换料系统，基于 YBA-AMS 改进而来。

- **主控**：ESP32-C3，使用 MicroPython 开发
- **通信**：MQTT over TLS 与打印机通信；TCP + Web 页面与操作者交互
- **执行机构**：**1 个共享直流电机 + 4 路电磁离合**（见下文）
- **已验证**：A1 mini，4 通道；主板预留扩展接口，最多支持 8 通道
- **固件要求**：打印机固件版本需低于 `01.03.01.00`
- **换料速度**：约 1 分半（从停止打印到恢复打印，含冲刷时间）

> ⚠️ **与上游版本的主要差异**：上游 YBA-AMS 是「每个料盘位一个电机」，本项目
> 改为「一个共享电机 + 每通道一个电磁离合」。这带来两个变化：
> 1. GPIO 从 8 个降到 6 个，且不再使用 ESP32-C3 上不存在的 GPIO22/23；
> 2. **引入了新的安全约束：任何时刻最多只能有 1 路电磁离合吸合**，
>    软件层用四重机制强制保证（见 [换料时序与软件架构](#换料时序与软件架构)）。

---

## 功能特性

| 能力 | 说明 |
| --- | --- |
| 自动换料 | 监听打印机 MQTT 状态，检测到换料请求后自动退料 + 进料，再恢复打印 |
| 多通道 | 默认 4 通道，最多 8 通道 |
| 颜色映射 | 网页上把打印机的 T 通道号映射到任意物理料盘位，可逐个设置料卷颜色，配置持久化 |
| 掉电保持 | WiFi / MQTT / 通道映射 / 颜色 / 当前料盘全部存在 `config.json`，断电重启不丢 |
| 状态指示 | 板载 LED：熄 = 未联网，闪 = WiFi 已连，常亮 = MQTT 已连 |
| 快速联网 | 首次配网成功后**不再扫描 WiFi**，开机直接用保存的账号密码直连，几十毫秒进网络 |
| 热点兜底 | 自动连接最多试 3 次，全部失败就打开热点 `AMS_WIFI`，用手机连上去重新配网 |
| 网页配置 | 响应式页面，手机 / 电脑都能用；一个 `/status` 接口拿全状态并每 2 秒自动刷新 |
| 取色器 | 通道颜色用 Windows 风格的调色板对话框：基本颜色 + HSV 渐变 + RGB/HSV 数值 + 自定义颜色 |
| 硬件调试 | 网页上实时显示 4 路离合状态，并可手动点动任意通道 |
| 电磁离合互斥 | 软件层强制同一时刻最多 1 路吸合，含启动检查 / 吸合前复核 / 运行期体检 |
| 一键烧录 | 一个 `.bin` 覆盖整片 Flash（引导程序 + 分区表 + MicroPython + 全部代码 + umqtt 库），烧完直接上电即用 |
| CI 自动构建 | 每次提交自动编译：语法检查 + 自测 + 单文件固件 + `.mpy` 包；主分支更新 `latest` 预发布版，打 tag 发正式 Release |

---

## 硬件方案

### 1 个电机 + 4 路电磁离合

```
                        ┌── 电磁离合 1 ──> 料盘位 1 送料轮
                        ├── 电磁离合 2 ──> 料盘位 2 送料轮
   共享直流电机 ────────┤
   (H 桥 IN1 / IN2)     ├── 电磁离合 3 ──> 料盘位 3 送料轮
                        └── 电磁离合 4 ──> 料盘位 4 送料轮

   动作：需要哪个料盘位送料，就吸合那一路离合，让它的送料轮咬上电机主轴，
        然后电机正转（进料）或反转（退料），动作完成后立刻断开离合。
```

**为什么必须保证只有 1 路吸合？** 如果有 2 路同时吸合，电机会同时拖动两个
料盘位的送料轮，两卷料互相拉扯，结果是断料、打滑或打印机报错。除了机械损坏，
4 路线圈同时吸合的浪涌电流也容易把 5V 电源拉塌，导致主控复位。

软件层的保证机制（`python_code/motor_clutch.py`）：

1. **物理层前置断开** —— 任何一次吸合之前，都先无条件下发「全部断开」电平
2. **吸合前状态复核** —— 写完全部断开后回读每一路，发现残留就再次断开并取消本次动作
3. **直接调用拦截** —— `Clutch` 的引脚只能由总线改写，绕过总线调用会抛异常
4. **运行期体检** —— 主循环每个周期调用 `assert_single()`，发现多路吸合立即全部断开

这四条都有对应的自动化测试覆盖，见 [本地开发与自测](#本地开发与自测)。

### ESP32-C3 引脚约束

ESP32-C3 只有 `GPIO0 ~ GPIO21`，其中相当一部分不能当普通 IO 用：

| GPIO | 用途 | 能否使用 |
| --- | --- | --- |
| `GPIO2` / `GPIO3` / `GPIO8` / `GPIO9` | **strapping 启动模式脚**，上电瞬间电平决定从哪启动（GPIO9 = BOOT 键） | ⛔ **绝不能做输出** |
| `GPIO11` ~ `GPIO17` | 模组内置 SPI Flash（含 VDD_SPI） | **不可用** |
| `GPIO18` / `GPIO19` | USB D- / D+ | 接了 USB 座就不能用 |
| `GPIO20` / `GPIO21` | UART0 RX / TX，默认日志与 REPL 口 | 不建议用 |
| `GPIO4` ~ `GPIO7` | JTAG 调试口 | 可以用，代价是放弃 JTAG |

因此可以安全当**输出**的引脚只有这 7 只：
**GPIO0、GPIO1、GPIO4、GPIO5、GPIO6、GPIO7、GPIO10**。

> ⚠️ **GPIO2 和 GPIO3 不是「空脚」，是启动模式选择脚。**
>
> 依据《ESP32-C3 技术参考手册》第 7 章表 7.2-1：复位释放后由
> **GPIO2、GPIO3、GPIO8、GPIO9 共同控制 Boot 模式**。
> 而且 datasheet 表 4-1 写得很明确：GPIO2、GPIO8 复位后是**浮空**，
> 这四个脚里只有 GPIO9 带内部弱上拉。
>
> 把电机 H 桥的 IN1/IN2 接到 GPIO2 / GPIO3 上，AT8236 输入级的
> **内置下拉电阻**会把这两个脚在上电瞬间拉低，芯片就进不了正常启动模式 ——
> 现象正是**反复重启、Wi-Fi 能连但网页打不开、电机一直有电流声**。
> （另有一条更直接的：GPIO2 上还挂着 1kHz 的状态灯 PWM，
> IN1 也接在 GPIO2 的话，电机就变成「跟着呼吸灯闪」。）
>
> `hardware_config.validate()` 已经把「电机接在 strapping 脚上」列为**硬错误**，
> 上电能直接在串口日志和网页的「上电诊断」里看到。

> 📌 上游代码里的 **GPIO22 / GPIO23 是经典 ESP32（38 脚）的编号，ESP32-C3 上不存在**，
> 必须改掉。本项目已全部重排。

### 默认接线表

| 信号 | GPIO | 说明 |
| --- | --- | --- |
| 电机 H 桥 IN1 | `GPIO4` | 方向 `1` = 进料（正转） |
| 电机 H 桥 IN2 | `GPIO5` | 方向 `-1` = 退料（反转） |
| 电磁离合 1 | `GPIO6` | 料盘位 1 |
| 电磁离合 2 | `GPIO7` | 料盘位 2 |
| 电磁离合 3 | `GPIO10` | 料盘位 3 |
| 电磁离合 4 | `GPIO3` | 料盘位 4（⚠️ strapping 脚，能跑但建议改到 `GPIO1`） |
| 状态 LED | `GPIO2` | 板载蓝灯（⚠️ strapping 脚；设成 `None` 可彻底关掉） |
| 到位开关 1~4 | *未安装* | 预留，见下方说明 |

改接线只需要改 `python_code/hardware_config.py` 里的一组常量，不用动业务代码。

**关于状态 LED 用 GPIO2**：板载 LED 在低电平时不导通、呈高阻，对 strapping
影响很小，所以 LED 挂在 GPIO2 上是可以接受的。但它**绝不能和任何功率器件
共用 GPIO2** —— 1kHz 的 LED PWM 会把 H 桥当灯闪。不想用状态灯就把
`hardware_config.py` 里的 `LED_PIN` 设成 `None`，代码会自动跳过。

**关于电磁离合 4 用 GPIO3**：ULN2803 的输入是达林顿基极，需要约 1.4V 才导通，
空闲时接近高阻，等于把引脚「悬空」，而 GPIO3 的官方默认状态本来就是浮空，
所以现在这样能用。但这属于**带病运行** —— 一旦换用输入带下拉的驱动板就会
起不来。建议尽早把 `CLUTCH_PINS` 改成 `(6, 7, 10, 1)`。

**关于到位开关（限位/微动开关）**：当前**未安装**，程序自动进入降级模式。
它的作用是探测「当前正在用哪个料盘」和判断「料有没有真的推动」。装了开关后，
把引脚填进 `hardware_config.py` 的 `LIMIT_SWITCH_PINS` 即可自动启用探测逻辑。

推荐接法（开关一端接 GPIO、另一端接 GND，内部上拉，低电平触发）：

- 通道 1 / 2：`GPIO0`、`GPIO1`（最干净）
- 通道 3 / 4：可用 `GPIO2`、`GPIO3`、`GPIO8` 或 `GPIO9` —— 这四个是 strapping 脚，
  但「内部上拉 + 开关对地」的接法空闲时正好是高电平，符合 strapping 要求，
  作**输入**是安全的（作输出就危险了，见上）

### 电气注意事项

> ⚠️ **这两条不做大概率会烧板子，请务必看完。**

1. **电磁离合线圈必须反向并联续流二极管**（1N4148 / 1N5819 均可）。
   线圈是感性负载，断开瞬间会产生几十伏的反电动势，会击穿 GPIO 或驱动管。
2. **不要用 GPIO 直接驱动离合线圈**。GPIO 输出只有 20mA 左右，离合线圈通常
   需要 100mA 以上。中间必须加三极管（S8050 / 2N2222）、MOS 管
   （AO3400 / IRLZ44N）或光耦隔离的驱动板；三极管方案基极要串 1kΩ 电阻。

另外：4 路离合的供电建议单独走一路 5V，不要和 ESP32-C3 共用同一根细线。

3. **电机驱动的 VM 必须单独供电，且和 ESP32 共地到同一点。**
   AT8236 的 VM 是 5.5V~36V 的功率电源，不要从开发板的 5V 引脚取电。
   电机启动瞬间的浪涌电流很容易把 3.3V 拉塌，而 ESP32-C3 一旦欠压就会复位 ——
   表现就是「空载时好好的，一接上负载就一直重启」。
4. **AT8236 的 ISEN 要接检流电阻或直接接地**，VREF 决定峰值限流。
   如果限流设得太小（比如 `VREF=2.0V / RISEN=0.2Ω` → 只有 1A），
   电机带载时会「堵转式」地嗡嗡响却转不起来 —— 这也是「有电流声但没启动」的
   常见原因之一。先把 `IN1/IN2` 接好、限流放宽，再逐步收紧。

### 物料清单

| 部件 | 数量 | 备注 |
| --- | --- | --- |
| ESP32-C3 开发板 | 1 | 任意 C3 模组板均可 |
| 直流减速电机 | 1 | 带动共享主轴 |
| 电磁离合器 | 4 | 每个料盘位一个 |
| H 桥驱动模块 | 1 | 如 L9110 / DRV8833 / TB6612 |
| 续流二极管 | 4 | 1N4148 / 1N5819 |
| 三极管或 MOS 管 | 4 | 驱动离合线圈，或直接用 4 路继电器/光耦模块 |
| 到位微动开关（可选） | 0~4 | 现在没有也能跑，见降级模式说明 |
| 3D 打印件 | — | 见 [`打印件/`](./打印件) 目录 |

---

## 项目结构

```
yaoams/
├── python_code/                  # ★ 要上传到 ESP32-C3 的全部代码
│   ├── main.py                   # 上电自动启动入口（保持 .py，不能编译成 .mpy）
│   ├── boot.py                   # 上电最先跑：置安全电平 + 记复位原因 + 累加启动计数
│   ├── reset_info.py             # ★ 复位诊断：欠压 / 看门狗 / 冷启动，网页可见
│   ├── AMS_WEB.py                # Web 配置服务 + 任务调度入口（继承 AMS）
│   ├── AMS_MODEL.py              # ★ 换料业务逻辑：探测料盘 / 退料 / 进料 / MQTT 调度
│   ├── device_processing.py      # ★ 硬件驱动：料盘位对象(material)、电机/离合总线工厂
│   ├── motor_clutch.py           # ★ 共享电机 + 电磁离合驱动层（互斥约束在这里强制）
│   ├── hardware_config.py        # ★ 唯一的硬件配置入口：引脚、时序、降级参数
│   ├── network_model.py          # WiFi 连接管理（AP / STA）
│   ├── info_load.py              # 配置文件读写（wifi.dat / config.json）
│   ├── logout.py                 # 日志输出
│   ├── index.html                # Web 配置页面（含硬件调试面板）
│   ├── umqtt/                    # MQTT 客户端库（第三方，纳入仓库以保证烧录后自包含）
│   │   └── simple.py             #   来自 micropython-lib（MIT）
│   └── bambu/                    # 与拓竹打印机 MQTT 通信相关
│       ├── bambu_mqtt.py         # MQTT 客户端封装（连接、订阅、发布）
│       ├── bambu_commands.py     # 常用 MQTT 命令（resume / pushall ...）
│       ├── bambu_const.py        # 打印机状态码、HMS 错误码中英对照
│       ├── bambu_G_code.py       # 常用 G-code 片段（擦拭、切料、调温）
│       └── get_event_info.py     # 从推送报文里解析换料 / 状态 / 报错信息
├── tests/                        # ★ 桌面端自测（PC / CI 运行，不烧进板子）
│   ├── run_tests.py              # 测试入口：python tests/run_tests.py
│   └── mpy_stubs/                # machine / network / ujson / uasyncio / umqtt 桩模块
├── tools/
│   ├── make_firmware_bin.py      # ★ 合成「单文件可烧录固件」（官方固件 + 本项目代码）
│   ├── lfs_mkfs.c                # ★ 生成 / 校验文件系统镜像（littlefs 2.8，与固件内置同源）
│   ├── build_mpy.py              # 交叉编译 .mpy 并打包部署 zip
│   └── preview_server.py         # 网页本地预览服务（假数据补全接口，PC 上调页面用）
├── .github/workflows/build.yml   # ★ GitHub Actions：检查 + 自测 + 固件 + .mpy + 发布
├── g_code/                       # Bambu Studio 换料 G-code（要粘贴到切片软件）
├── 打印件/                        # 结构件 STL / 3MF
├── assets/                       # 文档图片
├── dist/                         # 构建产物（已忽略，不进版本库）
├── .gitignore
├── LICENSE
└── README.md
```

---

## 快速开始

### 第 1 步：烧录（推荐：一个文件搞定）

到本仓库的 **Releases** 页面下载 `esp32c3-ams-firmware.bin`
（每次提交都会自动重新构建，标题为「最新构建」的那份永远是最新的），然后：

```bash
pip install esptool
esptool.py --chip esp32c3 --port COM5 write_flash -z 0x0 esp32c3-ams-firmware.bin
```

就这一条命令。该文件从地址 `0x0` 开始覆盖**整片 4MB Flash**：

| 地址 | 内容 |
| --- | --- |
| `0x000000` | ESP32-C3 引导程序 |
| `0x008000` | 分区表（nvs / phy_init / app / vfs） |
| `0x010000` | MicroPython v1.23.0（官方发布的固件，未做修改） |
| `0x200000` | 文件系统（littlefs），放着 `python_code/` 的全部代码 + `umqtt` 库 |

也就是说：**不用先烧固件、也不用再上传任何 .py**，烧完直接上电就能跑，
下面第 2 步可以跳过，直接看第 3 步。

> 正常情况下不需要先 `erase_flash` —— 这个文件覆盖整片 Flash，旧文件系统区域会被
> 整体重写。只有在出现异常（之前烧过别的固件、或想彻底清空）时才先补一条
> `esptool.py --chip esp32c3 --port COM5 erase_flash`，然后再烧。

### 第 2 步（备选）：自己烧固件 + 上传代码

需要频繁改代码调试、或者不想整片重烧时，用这种方式。

先烧官方 MicroPython 固件：

```bash
esptool.py --chip esp32c3 --port COM5 erase_flash
esptool.py --chip esp32c3 --port COM5 write_flash -z 0x0 ESP32_GENERIC_C3-xxxx.bin
```

固件到 [micropython.org/download/ESP32_GENERIC_C3](https://micropython.org/download/ESP32_GENERIC_C3/)
下载。记住版本号（例如 `v1.23.0`），CI 与 `.mpy` 都要和它对齐。

然后用 `mpremote` 上传代码：

```bash
pip install mpremote

# Windows
mpremote connect COM5 fs cp -r ./python_code/ :
# Linux / macOS
mpremote connect /dev/ttyACM0 fs cp -r ./python_code/ :
```

也可以直接用 Thonny 把 `python_code/` 里的文件拖到设备根目录（注意 `bambu/`
和 `umqtt/` 要建成同名子目录）。

或者用 CI 编译好的 `.mpy` 包（体积更小、启动更快）：

```bash
# 1) 从 GitHub Actions 下载 esp32c3-ams-mpy-v1.23.0 构件，或本地编译
pip install mpy-cross==1.23.0
python tools/build_mpy.py

# 2) 解压后整体上传
mpremote connect COM5 fs cp -r ./dist/ :
```

> ⚠️ `mpy-cross` 的版本必须和板子上的固件版本一致，否则设备会报
> `ValueError: incompatible .mpy file`。
>
> 注意 `boot.py` / `main.py` 必须是源码 `.py`（MicroPython 是直接执行这两个文件，
> 它们被编译成 `.mpy` 后不会被执行）。

### 第 3 步：修改 Bambu Studio 的换料 G-code

Bambu Studio → 打印机设置 → 机器 G-code → **Change filament G-code**，
把内容替换成 [`g_code/`](./g_code) 目录里的版本（二选一）：

- `切换耗材g_code.txt` —— 基础版
- `切换耗材（减少冲刷次数和加强切刀版本）.txt` —— 冲刷更少、切料更彻底（推荐）

关键点是里面必须有这一行，AMS 靠它知道「该换料了」：

```gcode
M73 P101 R[next_extruder]
M400 U1
```

![修改换料 G-code](./assets/image-20250113210443144.png)

### 第 4 步：首次配置

1. 给主板上电。如果还没配过 WiFi（或连接失败 3 次），它会自动开启热点
   **`AMS_WIFI`**，密码 **`A12345678`**
2. 电脑 / 手机连上该热点，浏览器打开 `192.168.4.1`
3. **WiFi 配置**：在列表里选你的 WiFi（只能选 **2.4G** 频段的）并填密码 → 点「连接这个 WiFi」
   （必须在和打印机同一个网络下）
4. 连上后「运行状态」里会显示主板分配到的新 IP，**记下它**，之后都用这个 IP 访问。
   页面会自动记住账号密码，以后开机会直接连，不再重新扫描
5. **打印机 MQTT 配置**：填入打印机的 IP、序列号、访问码（8 位）、MQTT 端口等 →
   点「保存并连接」
6. **打印机通道设置**：把「料盘 1~4」映射到打印机要用的通道号，点色块可以给每个
   料卷设颜色 —— 会弹出一个类似 Windows「颜色」对话框的取色器：左边是基本颜色和
   自定义颜色，右边是 HSV 渐变区 + 色相条，也可以用 RGB / HSV 数值精确指定 →
   点「保存通道与颜色」

> 页面会每 2 秒自动刷新状态，不用手动按 F5。
> WiFi 列表如果显示「正在扫描附近 WiFi…」，等一两秒即可（扫描本身要 1~2 秒）；
> 想重新扫点右上角「重新扫描」。

![初次配置页面](./assets/image-20250113204216748.png)

### 第 5 步：开始打印

1. 确认板载 LED **常亮**（= MQTT 已连接）。闪烁表示只连上了 WiFi。
2. 在切片软件里把耗材映射到对应通道：

   ![通道映射](./assets/image-20250113205448590.png)

3. 开始打印。打印机暂停并请求换料时，AMS 会自动完成换料并恢复打印。

---

## 换料时序与软件架构

### 分层结构

```
        ┌─────────────────────────────────────────────┐
        │  AMS_WEB.py     Web 配置 + asyncio 任务调度   │
        ├─────────────────────────────────────────────┤
        │  AMS_MODEL.py   换料业务逻辑（时序编排）      │
        │    now_filament / fileament_move             │
        │    exchange_fileament                        │
        ├─────────────────────────────────────────────┤
        │  device_processing.py                        │
        │    material（料盘位门面）                     │
        ├─────────────────────────────────────────────┤
        │  motor_clutch.py                             │
        │    FilamentMotorBus ★ 离合互斥仲裁            │
        │    HBridgeMotor / Clutch / LimitSwitch       │
        ├─────────────────────────────────────────────┤
        │  hardware_config.py   引脚与时序配置          │
        └─────────────────────────────────────────────┘
```

### 一次换料的完整时序

```
打印机                           ESP32-C3 (AMS)
  │                                   │
  │ 暂停 + 换料 G-code                  │
  │ M73 P101 R[next_extruder]         │
  │ M400 U1                           │
  ├────────── MQTT 推送 ─────────────>│  get_is_change_ams() 识别到换料请求
  │                                   │
  │<───────── 发 M83（相对挤出）────────┤
  │                                   │
  │<───────── 发 G1 E-50 F200 ─────────┤  打印机把喷嘴里的料退出来
  │                                   │
  │                                   │  ① 吸合【旧】通道离合
  │                                   │     电机反转 → 把料收进缓冲区
  │                                   │     停电机 → 断开全部离合
  │                                   │
  │                                   │  ② 吸合【新】通道离合
  │                                   │     电机正转 → 把新料推向挤出机
  │                                   │     停电机 → 断开全部离合
  │                                   │
  │<───────── 发 G1 E50 F200 ─────────┤  打印机把料拉进喷嘴
  │                                   │     同时 AMS 辅助推料 1s
  │                                   │
  │                                   │  ③ 更新 filament_current 并写入 config.json
  │                                   │
  │<───────── 发 resume ──────────────┤
  │ 继续打印                           │
```

### 关键实现细节

**离合互斥**：所有「吸合 → 动作 → 断开」的复合操作都走 `bus.run()` 或
`bus.hold()`，两者都用 `try/finally` 保证即使中途抛异常也一定停电机、
断开全部离合。主循环里每 200ms 还会做一次 `assert_single()` 体检。

**降级模式（未安装到位开关时）**：这是当前的实际运行模式，有两点要注意：

- 无法探测「当前在用哪个料盘」，程序直接沿用 `config.json` 里记录的
  `filament_current`。**所以这种情况下不要手动插拔料盘**，否则记录会和实际不一致。
- 送料/退料改为按时间推进，时长由 `NO_LIMIT_LOAD_MS` /
  `NO_LIMIT_RETRACT_MS` 决定。这两个值**必须按实机实测调整**：
  太小 → 料没送到挤出机，打印机会报「耗材缺失」；
  太大 → 料被顶弯、在缓冲区堆积，甚至顶坏挤出机。

**H 桥换向死区**：电机换向时先两脚拉低、等待 `MOTOR_DEAD_TIME_MS`，
再切换到新方向，避免 H 桥上下管直通烧驱动芯片。

---

## 联网与配网

### 上电联网：直连，不扫描

```
上电
 ├─ 读 wifi.dat（保存过的 WiFi 账号密码表）
 ├─ 有记录 → 逐个直接尝试连接（最多 3 次）
 │            └─ 成功 → 继续启动，连打印机 MQTT
 └─ 没有记录 / 3 次全失败
              └─ 打开热点 AMS_WIFI（密码 A12345678）
                 手机连上后访问 http://192.168.4.1 重新配网
```

**为什么不像以前那样先扫描？** `scan()` 是阻塞操作，在 ESP32-C3 上要 1.5~3 秒，
而且扫描时射频要切信道，会打断正在进行的连接。首次配网成功后账号密码已经存在
`wifi.dat` 里，直接连既快又稳。

只有在页面上点「重新扫描」或页面首次加载需要列出周围 WiFi 时才会真的扫描，
结果带 60 秒缓存（`SCAN_CACHE_MS`），不会每刷新一次就卡 2 秒。

### 连接成功的判定标准

必须**同时**满足：关联上 AP **且** 拿到非 `0.0.0.0` 的 IP。
只看 `isconnected()` 是不够的 —— 那时 DHCP 可能还没完成，直接去连 MQTT 必然失败。
密码错误、找不到该 AP 这类硬失败会立即返回，不会白等满超时。

### 联网相关参数

都在 `python_code/network_model.py` 顶部（与硬件引脚分开，改起来更直观）：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `AP_SSID` / `AP_PASSWORD` | `AMS_WIFI` / `A12345678` | 配置热点的名称与密码（WPA2 要求密码至少 8 位） |
| `WIFI_BOOT_ATTEMPTS` | `3` | 上电自动连接的总尝试次数，用完就开热点 |
| `CONNECT_TIMEOUT_MS` | `8000` | 单次连接最长等待（含 DHCP 拿 IP） |
| `RECONNECT_GAP_MS` | `500` | 一轮试完还没成功，歇一下再试下一轮 |
| `SCAN_CACHE_MS` | `60000` | 扫描结果缓存时长 |

### 网页接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 配置页面 |
| `GET` | `/status` | ★ 聚合状态：IP / WiFi / MQTT / 通道 / 颜色 / 硬件 / 复位诊断 / 内存余量，一次拿全 |
| `GET` | `/wifi_scan` | 强制重新扫描 WiFi（阻塞约 2 秒，仅用户点击时调用） |
| `GET` | `/boot_clear` | 把「启动次数」清零（排查复位循环时用） |
| `POST` | `/wifi_connect` | `{"name":ssid,"password":pwd}` |
| `POST` | `/mqtt_connect` | 打印机 IP / 序列号 / 访问码 / 端口等 |
| `POST` | `/access_set` | `{"access_list":[...],"color_list":[...]}` |
| `POST` | `/hardware_test` | `{"channel":1,"direction":1,"times_ms":1000}` 手动点动 |

**为什么要有 `/status`**：页面上有 IP、WiFi、MQTT、通道、硬件五块状态，早期是发 5 个
请求分别取。单线程服务端只能串行处理，累计延迟很明显。现在合成 1 个请求，
页面每 2 秒轮询一次就够了。

### 网页性能上的几个坑（都已修掉）

| 问题 | 旧做法 | 现在 |
| --- | --- | --- |
| 页面极慢 | `index.html` **逐行发送**，每行 `await sleep(10ms)`，400 多行要 4 秒以上 | 整份文件用 `os.stat` 取长度、带 `Content-Length` 分块流式发送 |
| **页面打不开**<br>`memory allocation failed` | 把 40KB 的页面 `f.read()` **整份读进内存**（还有一份 `.encode()` 拷贝），ESP32-C3 拿不到这么长的连续内存；而且缓存写不进去，**每个请求都在同一处再失败一次** | 每次只读 1KB（`FILE_CHUNK`）读一块发一块，单次最大分配 1KB；发送前 `gc.collect()` 收拢碎片 |
| 每个请求白等 | accept 循环里 `await sleep(500ms)` | 改成 20ms 轮询 |
| 偶尔打不开 | 读请求头用阻塞 `recv` + 3 秒超时，浏览器的空闲预连接会把服务端卡满 3 秒 | 非阻塞读 + `await` 让步，总上限 0.6 秒，等不到就丢掉连接 |
| 页面卡死 | 主循环用阻塞的 `wait_msg()` 收 MQTT，把整个事件循环按住 | 改用非阻塞的 `poll_msg()`，没消息立刻返回 |
| MQTT 频繁重连 | 「每 20 轮重连一次」，而每轮都阻塞，实际几秒断一次 | 改成按时间，每 5 分钟才重建 |
| 状态灯拖慢网页 | `check_mqtt_connection()` 每秒真的发一次 `PINGREQ` | 按 5 秒节流（`MQTT_PING_INTERVAL_MS`） |
| GC 过频 | `gc.threshold(1024)`：每分配 1KB 就回收一次 | 放宽到 16KB |

> 🔴 **`FILE_CHUNK` 不要调大。** 它直接决定"服务网页时的最大单次内存分配"。
> 1KB 在任何情况下都分配得出来；一调到 40KB 就退回上面那条 OOM 故障。

这些改动都有对应的自动化测试守着（见 [本地开发与自测](#本地开发与自测)），
防止以后改回去。

---

## 硬件配置项

所有硬件相关的参数都集中在 **`python_code/hardware_config.py`**，改完不用动业务代码。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `MOTOR_PIN_IN1` / `MOTOR_PIN_IN2` | `4` / `5` | H 桥两个输入脚 |
| `MOTOR_DEAD_TIME_MS` | `30` | 换向死区时间，单位 ms |
| `CLUTCH_PINS` | `(6, 7, 10, 3)` | 4 路电磁离合对应的 GPIO |
| `CLUTCH_ACTIVE_LEVEL` | `1` | `1` = 高电平吸合；驱动板是低电平吸合时改成 `0` |
| `CLUTCH_ENGAGE_MS` | `80` | 吸合后等待机械咬合的时间 |
| `CLUTCH_RELEASE_MS` | `60` | 断开后等待彻底脱开的时间 |
| `CLUTCH_SETTLE_MS` | `20` | 通道切换时的额外静默时间 |
| `LIMIT_SWITCH_PINS` | `(None, None, None, None)` | 到位开关，`None` = 未安装；填了 GPIO 就自动启用探测 |
| `FILAMENT_STEP_MS` | `500` | 单步推送时长 |
| `RETRACT_STEPS` / `LOAD_RETRY_TIMES` | `15` / `10` | 退料步数 / 进料重试次数（有开关时提前结束） |
| `NO_LIMIT_RETRACT_MS` | `6000` | ★ 降级模式退料时长，**需实测调整** |
| `NO_LIMIT_LOAD_MS` | `8000` | ★ 降级模式进料时长，**需实测调整** |
| `LED_PIN` | `2` | 状态指示灯 |
| `CONFIG_FILE` | `"config.json"` | 持久化配置文件 |

`hardware_config.validate()` 会在启动时校验引脚是否合法（重复占用、
越界、踩到 Flash/USB/UART 保留脚），有问题直接报出来。把
`device_processing.py` 当脚本跑一次就能打印完整接线表并逐个点动 4 个通道：

```bash
# 在设备 REPL 里（或 mpremote）执行
import device_processing
```

---

## 调试

### 上电诊断（复位原因 / 启动次数 / 引脚自检）

页面上的「上电诊断」卡片，以及串口每次上电都会打印的一段：

```
======== 上电自检 ========
复位原因 : BROWN_OUT_RESET（★ 欠压复位：供电电压掉到了阈值以下…）
启动计数 : 第 37 次（次数持续 +1 说明在反复复位）
==========================
---- 硬件配置 ----
共享电机 : IN1=GPIO4  IN2=GPIO5  换向死区=30ms
电磁离合1: GPIO6  (高电平吸合, 吸合等待80ms)
...
引脚配置自检通过：全部输出脚都在安全引脚上。
```

排查「接上负载就一直重启」的用法：网页点「重置启动计数」→ 拔电重插 →
回来看数字。**一次上电就涨了几十 = 复位循环**。

复位原因对照：

| 复位原因 | 含义 | 先查什么 |
| --- | --- | --- |
| `BROWN_OUT_RESET` | 欠压复位 | 供电功率、线径、共地、VM 滤波电容 |
| `PWRON_RESET`（反复出现） | 反复冷启动 | 同上；供电正常则查引脚是否被拉低 |
| `WDT_RESET` / `RTC_WDT_RESET` | 看门狗复位 | 有任务阻塞了事件循环 |
| `SOFT_RESET` | 软复位 | 正常（软件主动复位） |

卡片里还有一行 **空闲内存**（`/status` 的 `mem_free` 字段），低于 20KB 会标红。
配置页面有 40KB，是靠「分块发送」才发得出去的 —— 余量太小的时候，
任何一次大分配都可能失败，网页就会不稳甚至打不开。另外**任何请求出错时，
串口日志都会带上一句「空闲内存 xxx 字节」**，出问题先看这个数。

### 网页硬件面板

浏览器打开主板 IP，页面下半部分的「硬件调试」区块会显示：

- 电机当前方向（`1` 进料 / `-1` 退料 / `0` 停止）
- 4 路离合各自的吸合状态
- 累计冲突次数（**正常应始终为 0**；不为 0 说明离合驱动电路有问题）
- 每个通道的「进料 1s / 退料 1s」手动点动按钮

点动接口同样受互斥约束保护，不会出现两路同时吸合。

### 串口日志

`logout()` 会打印换料全过程，关键日志形如：

```
当前料盘:1新的料盘:3
开始退料
开始进料
换料第 1 次尝试
换料成功
```

出现 `离合体检异常` / `离合状态异常，已全部断开` 时，说明检测到了多路吸合，
请立刻断电检查离合的驱动电路是否有短路。

---

## GitHub Actions 自动编译与发布

`.github/workflows/build.yml` 分四个环节，**每次提交都会自动跑**（push 到任意分支、
提 PR、或在网页上手动点运行）：

| Job | 做什么 |
| --- | --- |
| **语法检查与逻辑自测** | `compileall` 检查全部 Python 文件；运行 `tests/run_tests.py` 验证离合互斥等硬件约束 |
| **交叉编译 .mpy 部署包** | 用 `mpy-cross` 把 `python_code/` 编译成 `.mpy`，打成一个 zip |
| **构建单文件固件 BIN** | 官方固件 + littlefs 文件系统镜像 → 合成可整片烧录的 BIN，并做四道校验 |
| **发布固件** | 主分支提交 → 更新滚动预发布版 `latest`；打 `v*` tag → 发正式 Release |

所有产物同时挂在 Actions 的 Artifacts 下（保留 30 天）和 Releases 页面。

**发布规则**

| 触发方式 | 结果 |
| --- | --- |
| push 到 `main` | 自动覆盖 Releases 里的 `latest`（预发布），下载地址固定不变 |
| push tag `v1.0.0` | 创建正式 Release `v1.0.0` |
| Pull Request | 只编译校验，不发布 |

```bash
git tag v1.0.0
git push origin v1.0.0
```

**单文件固件是怎么做出来的**

`tools/make_firmware_bin.py` 负责整个流程，核心是「文件系统必须与设备端同源」：

1. 从官方固件里**解析分区表**（不写死偏移），找到 `vfs` 分区（本板 `0x200000` 起，2MB）；
2. 用 `tools/lfs_mkfs.c` 生成 littlefs 镜像 —— 它链接的是**上游 littlefs 2.8**，
   与 MicroPython v1.23 内置的 `lib/littlefs` 完全一致，参数也照抄设备端
   `VfsLfs2.mkfs`（`block_size=4096`、`block_cycles=100`、`name_max=255` …）；
3. 与官方固件拼成从 `0x0` 覆盖整片 Flash 的 BIN；
4. 四道校验：用同一份 littlefs 代码回读并逐字节比对 → 产物重新解析镜像头与分区表 →
   校验块 0/1 偏移 8 处的 `littlefs` 魔数 → 再用另一套独立实现（littlefs-python）
   把 BIN 里的文件系统读一遍。

> ⚠️ 这些校验不是摆设。ESP32 端 `_boot.py` 挂载失败会走
> `inisetup.check_bootsec()`，只要首扇区不是 `0xFF` 就判定「文件系统损坏」并进入
> **死循环**，所以必须一次做对。脚本还会联网核对 littlefs 版本号，不一致直接报错停止。

**升级 MicroPython 版本**：改 `.github/workflows/build.yml` 顶部那 5 个变量即可，
文件里对每一项都有说明，其中 `MICROPYTHON_VERSION` 与 `LITTLEFS_VERSION` 的配套关系
由脚本自动检查。

**本地等价操作**：

```bash
python tests/run_tests.py                 # 自测
python tools/build_mpy.py                 # 交叉编译 .mpy 包

# 单文件固件（需要先编译那个小工具，CI 里是自动完成的）
gcc -O2 -o tools/build/lfs_mkfs tools/lfs_mkfs.c \
    <littlefs源码>/lfs.c <littlefs源码>/lfs_util.c -I<littlefs源码>
python tools/make_firmware_bin.py \
  --firmware-url "https://micropython.org/resources/firmware/ESP32_GENERIC_C3-20240602-v1.23.0.bin" \
  --out dist/esp32c3-ams-firmware.bin
```

> 本地生成固件需要 gcc；不想装就直接用 CI 产物（每次提交都会自动构建）。

---

## 本地开发与自测

核心的硬件安全逻辑可以在 PC 上直接验证，**不需要板子**。`tests/mpy_stubs/`
提供了一套 MicroPython 模块桩（`machine`、`network`、`ujson`、`uasyncio`、
`umqtt`），它会记录所有 GPIO 的写入，从而断言硬件约束。

```bash
python tests/run_tests.py
```

共 58 项测试，分四块。

**① 电磁离合安全约束**（核心，改动硬件层时必看）

| 测试 | 验证内容 |
| --- | --- |
| `test_only_one_clutch_engaged_at_a_time` | ★ 依次吸合 4 路离合，任何时刻吸合数都**不能超过 1** |
| `test_switch_channel_always_passes_through_all_off` | 切换通道时必须先经过「全部断开」，不允许出现两路咬合的瞬间 |
| `test_assert_single_detects_external_fault` | 模拟驱动电路故障导致两路吸合，体检必须发现并立即全部断开 |
| `test_hold_releases_on_exception` | 动作中途抛异常，也必须释放离合、停电机 |
| `test_motor_direction_and_dead_time` | 换向必须经过「两脚都拉低」的死区，H 桥不允许上下管同时导通 |
| `test_hardware_config_valid` | 引脚配置必须避开 ESP32-C3 的 Flash / USB / UART 保留脚 |

**② 联网与配网**

| 测试 | 验证内容 |
| --- | --- |
| `test_network_boot_connect_does_not_scan` | ★ 上电自动联网必须直连已保存的 WiFi，`scan()` 调用次数必须是 0 |
| `test_network_falls_back_to_ap_after_three_attempts` | 连不上时恰好尝试 3 次，然后返回 `None` 触发开热点 |
| `test_network_requires_real_ip_not_just_association` | 只关联上 AP、没拿到 IP 不算连接成功 |
| `test_network_scan_is_cached_and_sorted` | 扫描结果按信号排序去重，缓存有效期内不重复扫描 |
| `test_network_missing_wifi_dat_is_safe` | 全新板子没有 `wifi.dat` 时必须安全降级，不能抛异常 |

**③ 网页不会变慢 / 不会打不开的回归保护**

| 测试 | 验证内容 |
| --- | --- |
| `test_web_root_is_streamed_not_read_into_ram` | ★ 页面必须分块流式发送：不能有整份 `f.read()`、不能整份内存缓存、单块 ≤ 2KB |
| `test_web_streams_real_index_html_byte_for_byte` | ★ 拿真机上那份 40KB 页面实跑：逐字节一致，且**单次发送不超过 1KB** |
| `test_web_memory_error_sends_tiny_page_not_blank` | 内存不足时回一个极小的 503 提示页（且只在「一个字都没发出去」时才补） |
| `test_web_send_file_reports_missing_file` | 页面文件缺失要回 500，不能把异常抛给请求循环 |
| `test_web_header_read_is_not_blocking` | 读请求头必须非阻塞 + `await` 让步，且有总等待上限 |
| `test_ams_loop_does_not_block_on_wait_msg` | ★ 主循环里不能再出现阻塞收包，必须用 `poll_msg()` |
| `test_ams_loop_waits_for_wifi_before_mqtt` | WiFi 没连上时不要空转重连 MQTT（AP 模式下日志会被刷爆） |
| `test_ams_reconnect_is_time_based` | 定期重连要按时间节流（≥1 分钟），不能几秒断一次 |
| `test_mqtt_ping_throttling_behaviour` | 连续探测时，节流窗口内只应真的 `ping` 一次 |
| `test_web_status_aggregates_everything` | `/status` 必须把页面需要的字段一次给全（含 `mem_free`） |

**④ 复位诊断与引脚安全**（「接上负载就一直重启」的回归保护）

| 测试 | 验证内容 |
| --- | --- |
| `test_strapping_pins_include_gpio3` | ★ `GPIO3` 必须被列为 strapping 脚（早期版本漏了它） |
| `test_motor_on_strapping_pin_is_hard_error` | ★ 电机接到 `GPIO2` / `GPIO3` 必须直接判错，`validate()` 不通过 |
| `test_default_motor_pins_are_safe` | 默认电机引脚必须在安全输出集合里 |
| `test_clutch_on_strapping_pin_is_only_warning` | 离合挂 `GPIO3`（ULN2803 输入高阻）只警告，不能让板子起不来 |
| `test_make_safe_powers_everything_down` | ★ 上电第一件事必须把电机 / 离合 / LED 置到不上电电平 |
| `test_make_safe_survives_bad_pin` | 单个脚初始化失败也不能抛异常挡住启动 |
| `test_boot_py_safe_before_anything_else` | `boot.py` 必须是 `make_safe → capture → record_boot` 的顺序 |
| `test_reset_cause_is_captured_and_reported` | 复位原因必须能识别，且能序列化给网页 |
| `test_boot_count_increments_and_clears` | 启动计数能累加、能清零（数字疯涨 = 复位循环） |
| `test_status_exposes_reset_diagnostics` | ★ `/status` 必须带 `reset` 和 `boot_safety` 字段 |
| `test_led_can_be_disabled` | `LED_PIN = None` 时必须优雅跳过，不能 `AttributeError` |

改动相应模块后请先跑一遍再上传。

### 页面的本地预览

`index.html` 依赖 `/status` 等接口拿数据，直接用浏览器打开文件的话页面会一直
显示「加载中」，看不出真实效果。用这个小服务可以在电脑上完整跑起来：

```bash
python tools/preview_server.py        # 默认 http://127.0.0.1:8099
python tools/preview_server.py 9000   # 换端口
```

它只读 `python_code/index.html`，用假数据补齐接口，不写任何文件、不参与固件构建。
改页面时建议边改边看，尤其是颜色对话框和手机端的排版。

---

## 常见问题

<details>
<summary><b>★ 接上负载后一直重启、电机一直有电流声、网页打不开</b></summary>

**先对号入座，再动手改线：**

| 现象 | 八成是 | 怎么处理 |
| --- | --- | --- |
| 电机「一会长鸣、一会间隔响」 | IN1/IN2 接到了 `GPIO2`，而 `GPIO2` 上还挂着 1kHz 的状态灯 PWM | 把电机改回 `GPIO4` / `GPIO5` |
| 反复重启、网页永远打不开 | 电机接到了 `GPIO2` / `GPIO3`（strapping 脚），AT8236 输入的内置下拉把它拉低 | 把电机改回 `GPIO4` / `GPIO5` |
| 空载正常、一挂负载就重启 | 电源带不动（欠压复位） | 给 AT8236 的 VM 单独供电、加粗线径、共地 |
| 电机有电流声但转不起来 | AT8236 限流设得太小（VREF / ISEN） | 放宽限流，或把 ISEN 直接接地 |

**第一步：确认引脚没接错。**

电机 `IN1` / `IN2` 只能是 `GPIO0 / GPIO1 / GPIO4 / GPIO5 / GPIO6 / GPIO7 / GPIO10`
里的引脚。**`GPIO2` 和 `GPIO3` 是 ESP32-C3 的 strapping 启动模式脚，
AT8236 的输入内置下拉会把它们在上电瞬间拉低，一接上就起不来。**

**第二步：看「上电诊断」卡片 / 串口日志。**

```
======== 上电自检 ========
复位原因 : BROWN_OUT_RESET（★ 欠压复位：供电电压掉到了阈值以下…）
启动计数 : 第 37 次（次数持续 +1 说明在反复复位）
==========================
```

- `BROWN_OUT_RESET` → 供电问题。给 VM 单独供电、加粗线、共地到同一点。
- `PWRON_RESET` 且启动次数疯涨 → 芯片在反复掉电，同样先查供电；
  供电没问题就是引脚被拉低导致进不了启动模式。
- `WDT_RESET` → 有任务卡住了事件循环（老代码里的 `wait_msg()` 就是这个毛病，
  现已改成非阻塞轮询）。
- 「引脚自检」显示未通过 → 照着红字提示把线改到安全引脚上。

启动计数可以在网页上点「重置启动计数」清零：**清零 → 拔电重插 → 再看数字**，
一次上电就涨了几十，就确定是复位循环。

**第三步：把负载摘掉验证。**

断开电机和离合，只留 ESP32 供电。如果这样能正常启动、网页能开，
就说明是「负载把电源拉塌」而不是固件问题。再逐个接上电机 / 离合，
接哪一个出问题就查哪一路。

**为什么 GPIO2 上会有 1kHz 的 PWM？** 状态灯就是 1kHz
（`AMS_WEB.py` 里 `PWM(Pin(LED_PIN))`）：MQTT 连上时 `duty(1000)` 常亮，
只连上 WiFi 时 0.5 秒亮 / 0.5 秒灭。所以电机跟着它叫的规律正好是
「长鸣 ↔ 间隔响」，一看就能对上。
</details>

<details>
<summary><b>板子上电后没有反应 / 找不到 AMS_WIFI 热点</b></summary>

1. 确认 `main.py` 已经上传到设备**根目录**（不是子目录）。
   （用单文件固件整片烧录时，文件已经在文件系统里，这条基本不会踩到。）
2. 用串口工具（Thonny / `mpremote`）连上后看有没有报错，特别是
   `.mpy` 版本不匹配的 `ValueError: incompatible .mpy file`。
3. 确认 `boot.py` 和 `main.py` 是 `.py` 而不是 `.mpy` —— 这两个文件
   MicroPython 是直接 `exec` 文件内容的，不能编译成字节码。
</details>

<details>
<summary><b>串口反复打印 The filesystem appears to be corrupted / 一直重启</b></summary>

这是 MicroPython 挂载文件系统失败后的保护逻辑（`inisetup.check_bootsec()`）：
只要分区首扇区不是 `0xFF`，它就认定文件系统损坏，并进入死循环，不执行任何用户代码。

**正常烧录本仓库的单文件固件不会出现这个问题** —— 构建流程里有四道校验专门防它
（见 [GitHub Actions 自动编译与发布](#github-actions-自动编译与发布)）。一旦遇到，这样救回来：

```bash
# 1) 彻底擦除整片 Flash（关键，必须做）
esptool.py --chip esp32c3 --port COM5 erase_flash
# 2) 重新烧录
esptool.py --chip esp32c3 --port COM5 write_flash -z 0x0 esp32c3-ams-firmware.bin
```

反复出现的话，通常是固件版本与文件系统格式不配套（例如固件换了版本、但文件系统镜像
还是按旧版 littlefs 生成的），或者自己改过分区表。用本仓库 CI 产出的固件可避免。
</details>

<details>
<summary><b>LED 闪烁但不常亮，换料不动作</b></summary>

闪烁表示只连上了 WiFi，MQTT 没连上。检查：
- 打印机 IP / 序列号 / 访问码是否填对（访问码是 8 位，不是局域网密码）
- 主板和打印机是否在同一个网段
- 打印机固件版本是否低于 `01.03.01.00`
</details>

<details>
<summary><b>打印机报「耗材缺失」或料没送到位</b></summary>

当前没有到位开关，送料靠时间控制。把 `hardware_config.py` 里的
`NO_LIMIT_LOAD_MS` 调大一些（比如从 8000 调到 10000），逐步试到刚好够。
反过来，如果发现料被顶弯或在缓冲区堆积，就调小。
</details>

<details>
<summary><b>换料时出现断料 / 打印机报 AMS 异常</b></summary>

1. **首先检查是不是两路离合同时吸合了**。打开网页硬件面板看「冲突次数」，
   不为 0 就说明检测到过违规状态，通常是离合驱动电路短路或续流二极管没接。
2. 确认续流二极管已经反并联在每一路线圈两端。
3. 确认 4 路离合不是共用一根细电源线 —— 浪涌电流会拉塌电压。
</details>

<details>
<summary><b>网页打不开 / 找不到主板 IP</b></summary>

1. 首次使用：连热点 `AMS_WIFI`（密码 `A12345678`），访问 `192.168.4.1`。
2. 已配好的：在路由器后台找设备 IP，或看串口日志里打印的地址。
3. 换过 WiFi 或改了密码：板子直连失败 3 次后会自动重新开热点，连上去重新配。
4. 如果只连得上 WiFi 却拿不到 IP（路由器没开 DHCP、或连的是 5G 频段），
   串口会打印 `WiFi 连接失败: xxx（找不到该 WiFi）` 之类的状态，据此排查。
   **ESP32-C3 只支持 2.4G 频段**，5G SSID 是连不上的。
</details>

<details>
<summary><b>网页打开很慢 / 偶尔刷新不出来</b></summary>

这两个问题在本版本已经修掉了，对应原因分别是：

- **慢** —— `index.html` 以前是逐行发送、每行还 `await sleep(10ms)`，400 多行的
  页面光发 HTML 就要 4 秒以上；再加上 accept 循环里 `await sleep(500ms)`，
  每个请求都要多等几百毫秒。现在用 `os.stat` 取长度、分块流式发送，
  轮询间隔 20ms。
- **偶尔打不开** —— 读请求头原来是阻塞 `recv` + 3 秒超时。浏览器会开「预连接」
  套接字却什么都不发，服务端就在那里卡满 3 秒，期间所有任务停摆。现在是非阻塞
  读 + `await` 让步，总等待上限 0.6 秒。
- 还有一个隐藏元凶：主循环用阻塞的 `wait_msg()` 收 MQTT，没有消息时会把整个
  uasyncio 事件循环按住，Web 和状态灯任务全被饿死。现在改用非阻塞 `poll_msg()`。

如果你刷了旧固件还有这个问题，重新下载 Releases 里最新的
`esp32c3-ams-firmware.bin` 整片烧一次即可。

> 例外：**正在换料的那十几秒页面会卡一下**，这是有意为之。换料要精确控制电机
> 时序，不能被协程调度打断。换料结束后页面会自己恢复。
</details>

<details>
<summary><b>AP 能连上，但管理页打不开，串口一直刷 <code>memory allocation failed, allocating 36096 bytes</code></b></summary>

这是**页面太大、被整份读进内存**导致的（已修）。

ESP32-C3 的空闲堆只有几十 KB，而且碎片化严重；`index.html` 有 40KB，
`f.read()` 要一次性拿到连续 40KB —— 直接失败。**更坑的是**：原来第二次请求
想复用内存缓存，可是缓存根本没写进去，于是**每个请求都在同一个地方再失败一次**，
页面就永远打不开，串口一直刷同一行错误。

现在改成**分块流式发送**：`os.stat()` 取文件长度写进 `Content-Length`，
正文每次只读 1KB（`FILE_CHUNK`）读一块发一块，单次最大分配 1KB，
也不再有第二份 `.encode()` 拷贝；块与块之间 `await` 让步，发页面时换料主循环
和状态灯不会被饿死。

排查步骤：

1. 刷最新的 `esp32c3-ams-firmware.bin`（地址 `0x0`，整片）。
2. 看串口第一行：`配置页面 index.html（42088 字节）按 1024 字节分块发送，空闲内存 xxx 字节`。
   **空闲内存低于 20KB 就不正常**（说明别处有泄漏），把这行日志发出来。
3. 页面打开后看「上电诊断」卡片里的**空闲内存**一行，正常是几十 KB。

> 顺带说一句：以后只要在串口看到「…（空闲内存 xxx 字节）」，就先看这个数字 ——
> 所有请求异常都会带上它，OOM 类问题一眼就能判断余量。
</details>

<details>
<summary><b>AP 模式下串口一直在刷「未连接wifi」/「MQTT 未连接，尝试重连」</b></summary>

配置热点模式下 STA 本来就没连上路由器，反复去建 MQTT 是白费功夫。
现在 `run_ams_loop` 会先判断 WiFi：没连上就退避 30 秒、日志降频，
只打印 `WiFi 未连接，暂不重连 MQTT（等待配网）`。
连上 WiFi 之后才会按原来的节奏（10 秒一次、每 6 次打一条）重连。
</details>

<details>
<summary><b>为什么开机后 WiFi 列表是空的</b></summary>

因为**开机不再扫描 WiFi** 了（`scan()` 会阻塞 1.5~3 秒，拖慢开机和网页响应）。
列表由页面加载时触发的一次按需扫描填充，或者点「重新扫描」按钮手动刷新。
已保存的 WiFi 会直接连接，不需要出现在列表里。
</details>

<details>
<summary><b>想从 4 通道扩展到 8 通道</b></summary>

1. 在 `hardware_config.py` 里把 `CLUTCH_PINS` 扩成 8 个 GPIO，
   `LIMIT_SWITCH_PINS` 同步补齐长度。
2. `motor_clutch.FilamentMotorBus` 本身支持任意路数，不用改。
3. 注意 ESP32-C3 的干净引脚有限，8 路需要用到 strapping 脚
   （GPIO2/GPIO8/GPIO9）或放弃 USB（GPIO18/19），见
   [ESP32-C3 引脚约束](#esp32-c3-引脚约束)。
4. 8 路离合的供电要单独规划，建议用带使能的驱动模块逐路供电。
</details>

---

## 附录 A：部分 MQTT 命令说明

以下代码来自
[ha-bambulab/custom_components/bambu_lab/pybambu/commands.py](https://github.com/greghesp/ha-bambulab/blob/main/custom_components/bambu_lab/pybambu/commands.py)

```python
"""MQTT Commands"""
# 开灯
CHAMBER_LIGHT_ON = {
    "system": {"sequence_id": "0", "command": "ledctrl", "led_node": "chamber_light", "led_mode": "on",
               "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}}
# 关灯
CHAMBER_LIGHT_OFF = {
    "system": {"sequence_id": "0", "command": "ledctrl", "led_node": "chamber_light", "led_mode": "off",
               "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}}
# 设置速度
SPEED_PROFILE_TEMPLATE = {"print": {"sequence_id": "0", "command": "print_speed", "param": ""}}
# 获取版本
GET_VERSION = {"info": {"sequence_id": "0", "command": "get_version"}}
# 恢复打印
PAUSE = {"print": {"sequence_id": "0", "command": "pause"}}
# 暂停打印
RESUME = {"print": {"sequence_id": "0", "command": "resume"}}
# 停止打印
STOP = {"print": {"sequence_id": "0", "command": "stop"}}
# 获取所有
PUSH_ALL = {"pushing": {"sequence_id": "0", "command": "pushall"}}
# 开始推送
START_PUSH = {"pushing": {"sequence_id": "0", "command": "start"}}
# 发送 gcode
SEND_GCODE_TEMPLATE = {"print": {"sequence_id": "0", "command": "gcode_line", "param": ""}}
# param = GCODE_EACH_LINE_SEPARATED_BY_\n

# X1 only currently
GET_ACCESSORIES = {"system": {"sequence_id": "0", "command": "get_accessories", "accessory_type": "none"}}
```

本项目用到的换料相关命令定义在 `python_code/bambu/bambu_commands.py`：

```python
bambu_resume = '{"print":{"command":"resume","sequence_id":"1111111"},"user_id":"1"}'          # 继续打印
bambu_unload = '{"print":{"command":"ams_change_filament","curr_temp":220,...}}'              # 退料
bambu_load   = '{"print":{"command":"ams_change_filament","curr_temp":220,...}}'              # 进料
bambu_done   = '{"print":{"command":"ams_control","param":"done",...}}'                       # 确认进料完成
banbu_start  = '{"pushing": {"sequence_id": "1111111", "command": "pushall"}}'                # 获取设备信息
START_PUSH   = '{ "pushing": {"sequence_id": "1111111", "command": "start"}}'                 # 开始推送
```

---

## 附录 B：G-code 参考

### 擦拭插头

```gcode
M400
M106 P1 S178        ; 开启风扇
M400 S3             ; 设置打印速度
G1 X-3.5 F18000     ; 移动打印头进行擦拭
G1 X-13.5 F3000
G1 X-3.5 F18000
G1 X-13.5 F3000
G1 X-3.5 F18000
G1 X-13.5 F3000
M400                ; 等待所有动作完成
M106 P1 S0          ; 关闭风扇
```

### 切割材料

```gcode
G1 X180 F18000      ; 跑到切割待机位
G1 X200 F300        ; 进行切割
G1 X-13.5 F6000     ; 跑回冲刷区
G1 E-5 F100         ; 退出 5mm 的丝料
```

### 设置安全距离

```gcode
G91                 ; 相对定位
G1 Z10 F600         ; 将 Z 轴提升 10mm，避免与打印物接触
G90                 ; 绝对定位
```

### 挤出机

```gcode
M109 S[nozzle_temperature_range_high]   ; 设置喷嘴温度
M82                                     ; 激活绝对挤出模式
M83                                     ; 激活相对挤出模式
G92 E0                                  ; 重置挤出量
```

### 风扇

```gcode
M106 P1 S0          ; 关闭风扇 1（散热风扇）
M106 P1 S255        ; 风扇开到最大
```

### 切料软件配置

```gcode
M73 P101 R[next_extruder]
```

---

## 致谢与许可

- 上游项目：[YBA-AMS](https://github.com/yuanbao-yu/YBA-AMS)
- MQTT 命令参考：[greghesp/ha-bambulab](https://github.com/greghesp/ha-bambulab)
- 许可证：[MIT](./LICENSE)

第三方组件：

| 组件 | 用途 | 许可证 |
| --- | --- | --- |
| [MicroPython](https://micropython.org/) 官方固件 | ESP32-C3 运行时（构建时下载，未修改） | MIT |
| [littlefs](https://github.com/littlefs-project/littlefs) 2.8.0 | 生成设备文件系统镜像（构建时下载使用） | BSD-3-Clause |
| [umqtt.simple](https://github.com/micropython/micropython-lib) | MQTT 客户端，已纳入 `python_code/umqtt/` | MIT |

> 本项目为个人 DIY 项目，涉及对打印机的远程控制与自研送料机构。使用前请确认
> 你了解相关风险，尤其是电磁离合的驱动电路与供电设计。因硬件设计不当导致
> 的设备损坏不在本项目责任范围内。
