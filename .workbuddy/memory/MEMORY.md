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

## 代码约定

- 所有硬件参数只在 `hardware_config.py` 里改，业务代码不写死引脚
- 离合操作只能通过 `bus.run()` / `bus.hold()`，不直接写 Clutch 的引脚
- 复合动作必须用 try/finally 保证「停电机 + 断开全部离合」
- `boot.py` / `main.py` 必须保持 `.py`，不能编译成 `.mpy`
- `config.json` / `wifi.dat` 含密码，已在 .gitignore 里，不要提交

## 当前运行模式

未安装到位开关（`LIMIT_SWITCH_PINS = (None,)*4`），程序走降级模式：
- `now_filament()` 不探测，直接读 `config.json` 里的 `filament_current`
  → **不要手动插拔料盘**，否则记录与实际不一致
- 送料按时间推进，`NO_LIMIT_LOAD_MS` / `NO_LIMIT_RETRACT_MS` 需实测调整

## 常用命令

```bash
python tests/run_tests.py      # 桌面自测，无需板子（18 项）
python tools/build_mpy.py      # mpy-cross 交叉编译 + 打包，产物 dist/ 与 esp32c3-ams-mpy.zip
```

`mpy-cross` 版本必须与板子固件一致，当前 CI 用 1.23.0（mpy v6.3）。
