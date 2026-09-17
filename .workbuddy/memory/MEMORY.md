# YAO_AMS 项目长期备忘

## 项目定位

适配拓竹（Bambu Lab）打印机的 AMS 自动换料系统。**两套并存实现，互不影响**：
- `python_code/` —— MicroPython（ESP32-C3 / ESP32-S3 42 针，双板型）
- `esp-ams-s3/` —— ESP-IDF（C，ESP32-S3，见文末专节）

上游是 YBA-AMS，本项目主要差异是执行机构改为「1 个共享直流电机 + 4 路电磁离合」。
GitHub: https://github.com/liu-85/esp-ams （remote origin，main 分支）

## 双开发板架构【重要：改引脚前先读这一段】

同一套业务代码 + **两套引脚表** + 编译期写死板型。

- `python_code/board_c3.py` / `board_s3.py`：只描述"这块芯片哪些脚能用"
  （默认接线 / 保留脚 / strapping / SAFE_OUTPUT_PINS / SPARE_PINS）。**改接线改这里**
- `python_code/hardware_config.py`：选板型 + 业务参数（时序、降级时长）+ 上电自检，
  导出全部引脚常量。上层代码只写 `from hardware_config import MOTOR_PIN_IN1`，一行不改
- `python_code/board_select.py`：**构建产物，不进 git**，内容就一行 `BOARD = "c3"/"s3"/"auto"`
- `board_override.py`：可选现场覆盖文件（板子上放一个即可改引脚，不进 git）

板型优先级：编译期 board_select.py > board_override.py > `os.uname().machine`
（认不出按 C3 处理并提示）。

引脚差异（其余相同）：
| | 电机 | 离合 1~4 | 状态灯 |
|---|---|---|---|
| C3（GPIO 上限 21） | 4/5 | 6, 7, 10, **3**（3 是 strapping，带病运行） | 2 |
| S3（GPIO 上限 48） | 4/5 | 6, 7, 8, 9（**全干净**） | 2 |

S3 剩余可用 IO（未装到位开关时）：**18 只干净脚**
`1,10,11,12,13,14,15,16,17,18,21,38,39,40,41,42,47,48`
+ 4 只空闲 strapping 脚 `0,3,45,46`。S3 保留脚：19/20(USB)、26~32(Flash)、
33~37(八线 PSRAM)、43/44(UART0)。S3 的 strapping：0/3/45/46。

**为什么推荐 S3**：C3 只有 400KB SRAM，应用加载后空闲堆仅约 60KB，
WiFi 驱动收发要申请**连续**缓冲 → 传 40KB+ 的 index.html 会 `OSError(113)`、
射频卡死只能复位（见下）。加 OTA 后应用变大，问题从「偶发」变「必然」。


## 硬件约定

- **具体引脚号在 `board_c3.py` / `board_s3.py` 里，不在 hardware_config.py**
  （hardware_config 只负责选板型 + 业务参数）
- 共享电机 H 桥：IN1=GPIO4 方向 1 进料，IN2=GPIO5 方向 -1 退料（两块板相同）
- 电磁离合 1~4：C3 = 6/7/10/3，S3 = 6/7/8/9，高电平吸合
- 状态 LED：GPIO2（两块板相同）
- **硬性约束：任何时刻最多 1 路电磁离合吸合**，由
  `motor_clutch.FilamentMotorBus` 四重机制强制（前置断开 / 吸合前复核 /
  直接调用拦截 / 运行期 assert_single）
- ESP32-C3 禁用引脚：11~17（内置 Flash）、18/19（USB）、20/21（UART0）；
  2/8/9 是 strapping，谨慎使用
- **WiFi 必须关省电**：ESP32 默认 `pm=1`（modem sleep），持续传输时会周期性
  休眠 → STA 掉线（串口报 `ECONNABORTED` / `ECONNRESET`），表现是"网页传一半
  就断"。main.py 第 0 步与 network_model.__init__ 都已 `sta/ap.config(pm=0)`，
  而且**必须在干净堆上做**（它动 WiFi 配置，同 swcith_ap 那类 0x0101 陷阱）。
- **C3 传大文件会失败是内存问题，不是网络问题**：应用加载后空闲堆仅约 60KB，
  WiFi 驱动要申请**连续**缓冲 → `OSError(113)`（EHOSTUNREACH）→ 重传约 9 秒
  放弃 → 射频卡死只能复位。ping 通、TCP 连不上就是这个症状。
  **根治办法是换 ESP32-S3**；C3 上只能靠 .mpy 省内存勉强跑。
- **WiFi 驱动必须抢在应用加载之前初始化**：`network.WLAN()` 第一次调用
  要一次性申请约 24KB **连续**堆；应用 import 之后堆碎片化就抢不到，
  报 `Wifi Unknown Error 0x0101`（= ESP_ERR_NO_MEM，在错误表外）。
  main.py 顶部已前置 `WLAN(AP_IF)+WLAN(STA_IF)`，之后再调只花 16 字节
  （对象复用）。**往 main.py 前面加 import 时，别把这段挪到后面。**

### 「干净堆预分配」总规则【本项目最容易复发的一类 bug】

MicroPython 的 GC **不做压缩**。应用 import 完，空闲还有 64KB，
但**最大连续块只剩 3,584 字节**，`gc.collect()` 也只多合出 2KB。
于是**凡是"要一大块连续内存、且只在启动时做一次"的操作，都必须抢在
应用加载之前做完**，否则要么抛 `0x0101`，要么**直接硬复位（无 traceback）**。

main.py 的阶段划分就是这条规则的产物，不要在中间插 import：
0. 建 AP/STA 对象 + `sta.active(True)` 真开射频（AP 已起时 STA 只花 48 字节）
0.5 `auto_connection()`，失败才 `swcith_ap(1)` 开热点
0.6 `boot_resources.prepare_web_server()` 预建 `:80` 监听 socket
   然后才 `import AMS_WEB`

已确认会踩的同源坑（都修了，别改回去）：
- `network_model.__init__` **不许 `active(False)`**：AMS_WEB→AMS→Bambu_mqtt_cliet
  这条继承链会让 `__init__` 在 main_task 里再跑一次，把刚开好的口关掉
- `swcith_ap(1)` 必须**幂等**：热点已 active 时**绝不能再 `config()`** ——
  实测碎堆上 `ap.config(essid/password/authmode)` 任一参数都抛 0x0101
  （`active()`、`ifconfig()` 都正常，`active(True)` 重复调是 no-op）
- 运行期开关热点仍有风险：配网成功 `swcith_ap(0)` 安全，之后想再开热点
  会在碎堆上 `esp_wifi_start` → 复位。要换 WiFi 请重启走第 0.5 步

## 代码约定

- 所有硬件参数只在 `hardware_config.py` 里改，业务代码不写死引脚
- 离合操作只能通过 `bus.run()` / `bus.hold()`，不直接写 Clutch 的引脚
- 复合动作必须用 try/finally 保证「停电机 + 断开全部离合」
- `boot.py` / `main.py` 必须保持 `.py`，不能编译成 `.mpy`
- `config.json` / `wifi.dat` 含密码，已在 .gitignore 里，不要提交
- **同一文件不要并行发两个 Edit**，会互相覆盖丢改动（USER.md 明确要求）

## 当前运行模式

未安装到位开关（`LIMIT_SWITCH_PINS = (None,)*4`），程序走降级模式：
- `now_filament()` 不探测，直接读 `config.json` 里的 `filament_current`
  → **不要手动插拔料盘**，否则记录与实际不一致
- 送料按时间推进，`NO_LIMIT_LOAD_MS` / `NO_LIMIT_RETRACT_MS` 需实测调整

## AP 热点：已实测证明**设备侧完全正常**（2026-09-17 21:19 推翻旧结论）

⛔ **旧结论「射频发射侧坏了 / 天线 / 欠压」是错的，已作废**（那段曾写在
本文件里）。当时是拿"板子自己扫不到自己的热点"和 `ap.config('channel')`
读回 1 当证据 —— 两条都是**无效证据**：

1. **APSTA 模式下 `sta.scan()` 扫不到自己的 SoftAP**（实测 `SELF_SEEN 0`，
   而同一时刻电脑网卡扫到它 99%）。所以"板子扫不到自己"什么都证明不了。
2. `config('channel')` 读回 1 也不是故障。翻了板子那版 MicroPython 1.23.0 的
   `ports/esp32/network_wlan.c`：读 channel 走的是**实时** `esp_wifi_get_channel()`，
   写 channel 走 `esp_wifi_set_channel()`。信道 1 就是 AP 默认值，而且它工作正常。
   → 这条只能说明"set_channel 没改成 6"，不能说明"射频没起来"。
3. 另一个逻辑硬伤：STA 能关联路由器拿 IP **本身就必须发帧**，"发射坏了"和它矛盾。

### ★ 判决性做法：用**独立射频**（电脑网卡）扫，别信板子自己的说法

```powershell
netsh wlan show networks mode=bssid   # 找 AMS_WIFI，看 信号/信道/BSSID
```

实测（ESP32-S3，板子日志 `配置热点已打开: AMS_WIFI`）：

```
SSID 1 : AMS_WIFI   WPA2-个人  CCMP   BSSID b4:3a:45:a6:09:b9
        信号 99%   2.4GHz   信道 1
```

BSSID 与板子自报 MAC 一致（板子打印成 `b'\xb4:E\xa6\t\xb9'`，`:` `E` 其实是
字节 `0x3A 0x45` 被当字符打出来 —— 别误读成 MAC 少了字节）。中断应用时和
应用正常运行中都各扫一次，两次都在 → **热点一直在正常发射**。

### 健康读数（S3，同一时刻取的）

| 项 | 值 | 说明 |
|---|---|---|
| 复位原因 | `PWRON_RESET`（code 1） | **不是欠压复位**，旧结论的"欠压嫌疑"不成立 |
| STA TX 功率 | 20.0 dBm | 满功率，不是被降过 |
| `pm` | 0 | 省电已关 |
| AP `ifconfig` | 192.168.4.1 | IP 层正常 |
| `max_clients` | 10 | 不会被"占满" |
| Web | `监听 0.0.0.0:80（3 个 worker）` | 服务已起 |

启动链干净：`没有已保存的 WiFi 记录 → 配置热点已运行(幂等) → Web 服务已启动`，
无 traceback、无复位循环（`启动计数` 只随手动复位 +1）。

### 所以问题在**客户端**，优先排查（按命中率排序）

1. **手机上"忘记"这个网络再重连** —— 手机存了旧的 AMS_WIFI 档案（改过密码/
   改过加密方式），自动重连用旧密码**静默失败且不再弹密码框**。这是"看得见
   但连不上"的第一名。顺带会清掉以前设过的**静态 IP**。
2. **关掉"智能网络切换 / WLAN+ / WLAN 助理"** —— AMS_WIFI 没有外网，小米、
   华为等会把它判为"无网络"并自动切回家里那个有网的热点，表现是连上又掉。
3. 手动"添加网络"输入 `AMS_WIFI` / `A12345678`（注意大小写，结尾别带空格）。
4. 手机别开"仅 5GHz"；iOS 弹"无 Internet 连接"要点"保持连接"。

### 不依赖手机的绕行（一直可用）

`tools/serial_provision.ps1` —— 串口直写 `wifi.dat` 配网，让板子走 STA 连
路由器，完全不碰热点。

- ⚠️ **板子 ping 不通是正常的**（MicroPython 的 lwIP 不回 ICMP），
  别用 ping 判断板子在不在线，用 HTTP 或看串口日志。

## 常用命令

```bash
python tests/run_tests.py      # 桌面自测，无需板子（当前 124 项）
python tools/build_mpy.py      # 默认 --board both：两块板各一个包（dist/c3、dist/s3）
python tools/build_mpy.py --board c3|s3|auto   # 只编一个板型
python tools/make_firmware_bin.py --board c3|s3 --chip esp32c3|esp32s3 \
    --firmware-url <官方固件> --out dist/<芯片>-ams-firmware.bin
python tools/make_update_pack.py [--board keep|c3|s3|auto]   # 网页 OTA 的 .ams 包
```

`mpy-cross` 版本必须与板子固件一致，当前 CI 用 1.23.0（mpy v6.3）。
本机已装在隔离环境
`C:\Users\lkfcs\.workbuddy\binaries\python\envs\default\Scripts\python.exe`，
用这个解释器跑构建/自测。**本机没有 gcc**，单文件固件只能在 CI 里出。

官方固件缓存于 `dist/_cache/`。两个芯片的 vfs 分区都是 `0x200000` 起，
但大小不同：**C3 = 2MB（整片 4MB）**，**S3 = 6MB（整片 8MB）**。
→ **S3 单文件固件必须烧在 8MB 及以上 Flash 的模组**（N8R8 / N16R8 满足）。
S3 官方固件 `ESP32_GENERIC_S3-20240602-v1.23.0.bin`
SHA256 `b91080af2e9b78bad4308f98bb6187567cae24ed77cd7f48ef99b47af3ef0555`；
C3 那份是 `8058b7d6eb55f8124fbdcc797e2e8b39ae947a18df635567e02c8786874c04fd`。
分区表由脚本从官方固件里解析，不写死偏移。


## 单文件固件（一键烧录）约定【重要】

- 产物 `dist/<芯片>-ams-firmware.bin` = 官方固件(0x0) + littlefs 镜像(0x200000)，
  C3 共 4MB、S3 共 8MB，`esptool write_flash 0x0` 一把烧完，设备开机即跑。
- 构建时会**先把源码拷到 `dist/_work/src-<board>/` 暂存目录**，在那里写入
  按板型生成的 `board_select.py`，再打包 —— 不往版本库的 python_code/ 写生成物。
- **文件系统必须是 littlefs**：v1.23.0 的 `vfs` 分区 subtype 虽标 0x81，
  实际由 `flashbdev.py` + `inisetup.py` 走 `VfsLfs2`。镜像 block0/1 的
  offset 8 必须有 `"littlefs"` 魔数，否则 `_boot.py` 挂载失败 → 死循环报
  "filesystem appears to be corrupted"。
- **镜像必须用与设备同源的 littlefs 2.8.0 生成**（`tools/lfs_mkfs.c` 链接
  上游源码），不要用 pip 的 littlefs-python 直接生成（它带 2.11，有小文件
  inline 特性，存在兼容风险；只用于反向校验）。
- 升级 MicroPython 时**这些必须同步**：`MICROPYTHON_VERSION`、
  `LITTLEFS_VERSION`、`MPY_CROSS_VERSION`，以及 **CI 里 firmware job 的
  matrix**（两块板各自的固件 URL + SHA256）。
- CI：push 任意分支/ tag 即触发；push main 更新滚动 `latest` 预发布，
  打 `v*` tag 发正式 Release。产物：**两块板各自的** `.mpy` 压缩包 + 固件 BIN。
- `.mpy` 包与固件都带 `board_select.py`，两个通道用同一个约定，不会配置不一致。

## ESP-IDF 版分支 `esp-ams-s3/`（2026-09-17 新增，与 MicroPython 版并存）

同一套硬件，用 ESP-IDF（C）重写。**两套实现互不影响**：MicroPython 版仍在
`python_code/`，各有独立 CI 流水线。重写的最大收益：Python 版
「干净堆预分配」那一整类约束（射频抢连续内存、handler 必须非阻塞）从架构上消失
—— IDF 下 WiFi 用静态缓冲、`esp_http_server` / `esp-mqtt` 各跑在自己的任务里，
**handler 可以放心写阻塞代码**；并且有了真正的 ota_0/ota_1 双分区整机 OTA。

### 硬约束（踩过或差点踩，别改回去）

- **必须 ESP-IDF v5.x，不能降到 v4.x**：v4 的 `esp_mqtt_client_config_t` 还是扁平
  字段（`uri`/`username`/`password`），会直接编译失败。
- MQTT 模块必须叫 `bambu_mqtt.c/h`，**不能叫 `mqtt_client.c/h`** —— IDF 自带
  `mqtt_client.h`，同名会把官方头文件遮住，`esp_mqtt_client_init` 找不到。
- **电机 IN1/IN2 绝不同时为高**（H 桥直通烧驱动芯片）→ `motor_apply()` 必须
  "先双路清零、再给目标通道赋值"。写成先写目标通道，会在换向瞬间直通。
- 离合最多 1 路吸合 → `clutch.c` 四重仲裁（与 Python 版 FilamentMotorBus 同级）。
- `main/CMakeLists.txt` 的 `REQUIRES` **必须留 `esp_psram`**（哪怕一行它的 API 都
  没调）：`CONFIG_SPIRAM*` 定义在 `esp_psram` 的 Kconfig 里，组件不在依赖列表里
  就可能不被解析 → 配置被当未知项丢掉、PSRAM 悄悄没打开（不报错，只是内存少）。
- `sdkconfig.defaults` 里**不许写** `CONFIG_ESP_WIFI_POWER_SAVE_NONE`（该符号根本
  不存在）和 `CONFIG_SPIRAM_TYPE_AUTO`（v5.0 已删）。WiFi 省电在 IDF 里是**运行时**
  设置，唯一正确位置是 `esp_wifi_set_ps(WIFI_PS_NONE)`（在 `wifi_mgr_init()`）。
- `main.c` 末尾 `if (web_ok) esp_ota_mark_app_valid_cancel_rollback();` **不能省**，
  否则"升级成功但一重启变回旧版"，极难查。放最后 = 只有体检通过才确认。
- IDF v5.0 起 `esp_chip_info.h` / `esp_random.h` / `esp_mac.h` 不再由 `esp_system.h`
  间接包含，必须显式 include。取芯片版本用 `esp_chip_info()`，**没有
  `esp_get_revision()` 这个 API**。

### 引脚与流程

引脚：电机 4/5，离合 6/7/8/9，LED 2；**每路 3 个微动**（停止/开始/自吸），
通道1~4 = 11/12/13、14/15/16、17/18/21、38/47/48；挤出机到位 GPIO1。
剩余可用 10、39~42 + strapping 0/3/45/46。改接线只改 `main/board_pins.h`。
自吸流程：自吸微动 → 送料到停止微动 → 等挤出机到位（GPIO 或 MQTT 事件，最长 15s）
→ **蠕动送料 3 次**（慢速短脉冲）→ 停电机 + 断离合。

### 验证与工具

- `esp-ams-s3/tools/lint_c.py`：两条核心判据 ——
  ① **"代码位置出现中文字符"**（C 标识符只能是 ASCII，中文只能出现在字符串/注释里，
     越界即字符串被提前截断）。★ **不要退回"数引号奇偶"** —— 引号成对的 bug 它看不见
     （已实际踩到）。
  ② **"文件带 UTF-8 BOM"**（以**字节**为入口检查）。带 `--selftest`（**12 条用例**）。
     扫描范围含 `.csv`（分区表最怕 BOM，但它不是 .c/.h）。两个命令都返回非 0，可当 CI 门禁。
- CI `.github/workflows/esp-ams-s3-build.yml`：**只在该目录改动时触发**，
  用 `espressif/esp-idf-ci-action@v1`（镜像 `espressif/idf:v5.3.2`）跑 `idf.py build`，
  并检查应用体积 ≤ `ota_0` 上限 2031616 字节。不发布产物。
- ⏳ **本机没有 ESP-IDF 工具链（也没有 gcc），`idf.py build` 从未跑过** ——
  改完要验证编译只能推分支让 CI 跑。
- 分区表按 8MB Flash 排（占 5.06MB）：nvs 0x9000/24K、otadata 0xF000/8K、
  phy_init 0x11000/4K、ota_0 0x20000/1.94M、ota_1 0x210000/1.94M、
  storage 0x400000/1M、**coredump 0x500000/64K**。4MB 模组的替代表见 `esp-ams-s3/README.md`。
- ✅ **分区表可以离线验证**（不用装工具链）：`gen_esp32part.py` 是纯 Python，
  从 IDF v5.3.2 tag 下下来直接跑
  `python gen_esp32part.py --flash-size 8MB --offset 0x8000 partitions.csv out.bin`
  → `Verifying table...` 退出码 0，再把 out.bin 回读自洽。当前结果是
  `ota_0 = 1984K = 2031616 字节`，与 CI 体积上限一致。

### ★ `partitions.csv` 绝对不能带 UTF-8 BOM（已踩，必挂）

`gen_esp32part.py` 以**二进制**读入后 `data.decode()`（默认 utf-8，**不是** utf-8-sig），
再用 `line.strip().startswith('#')` 跳注释 —— **不做 BOM 剥离**。带 BOM 时首字符是
U+FEFF 而不是 `#`，跳过逻辑失效 → 首行注释被当成一条分区定义解析 →
`Field 'type' can't be left empty.`，**构建直接失败**，且报错完全指不到 BOM。

⚠️ **写文件工具会保留/回写 BOM** —— 用编辑器重写后 BOM 会回来。剥 BOM 必须用
**字节级操作**（Python `rb` 读 → 切掉前 3 字节 → `wb` 写），改完**再跑一次 lint 复核**。

### 推送 / CI 的现实约束（2026-09-17）

- 分支 `esp-ams-s3-idf` 已推到 origin（= `54e953b`，**不含** CI 文件）。
  `origin/main` 保持 `1633d1b` **未动**。
- ⛔ **HTTPS + PAT 不允许推送 `.github/workflows/` 下的新增或修改**，除非 token 带
  `workflow` scope（细粒度 token 要 `Workflows: Read and write`）。当前凭据缺这个
  scope，所以 CI 文件留在**本地一个待推提交**。SSH 不受此限，但本机
  `~/.ssh/id_ed25519` **没注册到 GitHub**。给 classic PAT **加 scope 不会改变 token 值**，
  补完直接重推即可，不必重新登录。
- ⚠️ 本环境 **`git checkout -b feat/xxx`（带斜杠）建不出引用**：返回 0、HEAD 变 unborn，
  接着 `git add` 会把全仓库文件标成新增（差点提交出"整仓库都是新文件"）。
  **用不带斜杠的分支名**，并且建完立刻 `git rev-parse --verify refs/heads/<名字>` 复核。
  恢复无损：`git symbolic-ref HEAD refs/heads/main` + `git reset`（不动工作区）。
- 推分支会同时触发**既有那条 MicroPython 流水线**（它监听任意分支 push），
  看到"有 run 在跑"不等于 IDF 编译跑了 —— 要核对 run 的名字。

