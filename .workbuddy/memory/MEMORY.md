# YAO_AMS 项目长期备忘

## 项目定位

适配拓竹（Bambu Lab）打印机的 AMS 自动换料系统，ESP32-C3 + MicroPython。
上游是 YBA-AMS，本项目主要差异是执行机构改为「1 个共享直流电机 + 4 路电磁离合」。
GitHub: https://github.com/liu-85/esp-ams （remote origin，main 分支）

## 硬件约定

- 共享电机 H 桥：IN1=GPIO4 方向 1 进料，IN2=GPIO5 方向 -1 退料
- 电磁离合 1~4：GPIO6 / GPIO7 / GPIO10 / GPIO3，高电平吸合
- 状态 LED：GPIO2
- **硬性约束：任何时刻最多 1 路电磁离合吸合**，由
  `motor_clutch.FilamentMotorBus` 四重机制强制（前置断开 / 吸合前复核 /
  直接调用拦截 / 运行期 assert_single）
- ESP32-C3 禁用引脚：11~17（内置 Flash）、18/19（USB）、20/21（UART0）；
  2/8/9 是 strapping，谨慎使用
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

## 常用命令

```bash
python tests/run_tests.py      # 桌面自测，无需板子（当前 116 项）
python tools/build_mpy.py      # mpy-cross 交叉编译 + 打包，产物 dist/ 与 esp32c3-ams-mpy.zip
python tools/make_firmware_bin.py   # 生成单文件一键烧录固件 dist/esp32c3-ams-firmware.bin
```

`mpy-cross` 版本必须与板子固件一致，当前 CI 用 1.23.0（mpy v6.3）。

## 单文件固件（一键烧录）约定【重要】

- 产物 `dist/esp32c3-ams-firmware.bin` = 官方固件(0x0) + littlefs 镜像(0x200000)，
  共 4MB，`esptool write_flash 0x0` 一把烧完，设备开机即跑。
- **文件系统必须是 littlefs**：C3 v1.23.0 的 `vfs` 分区 subtype 虽标 0x81，
  实际由 `flashbdev.py` + `inisetup.py` 走 `VfsLfs2`。镜像 block0/1 的
  offset 8 必须有 `"littlefs"` 魔数，否则 `_boot.py` 挂载失败 → 死循环报
  "filesystem appears to be corrupted"。
- **镜像必须用与设备同源的 littlefs 2.8.0 生成**（`tools/lfs_mkfs.c` 链接
  上游源码），不要用 pip 的 littlefs-python 直接生成（它带 2.11，有小文件
  inline 特性，存在兼容风险；只用于反向校验）。
- 升级 MicroPython 时**五个变量必须同步**：`MICROPYTHON_VERSION`、
  `LITTLEFS_VERSION`、`MPY_CROSS_VERSION`、固件下载 URL、固件 SHA256。
- CI：push 任意分支/ tag 即触发；push main 更新滚动 `latest` 预发布，
  打 `v*` tag 发正式 Release。产物：`.mpy` 压缩包 + 单个固件 BIN。
