#!/usr/bin/env python3
"""
tools/build_mpy.py —— 把 python_code/ 交叉编译成 .mpy 部署包（支持 ESP32-C3 / ESP32-S3）
======================================================================================

MicroPython 的源码可以直接丢到板子上运行，但先在本机交叉编译成 .mpy 有两个好处：

    1. 省 RAM —— .mpy 是字节码，导入时不需要在设备上重新编译源码。
       ESP32-C3 只有 400KB SRAM，这个项目又要跑 MQTT(TLS) + Web 服务，
       省下来的这几十 KB 很关键。
    2. 省 Flash、启动更快 —— 字节码比源码小得多。

哪些文件不能编译：
    boot.py / main.py  —— MicroPython 上电时会直接 exec 这两个文件的内容，
                          必须是 .py 放在文件系统上，不能是 .mpy。

--------------------------------------------------------------------------
★ 两块开发板：编译时自动套用对应的引脚配置
--------------------------------------------------------------------------
本项目的执行机构是「1 个共享直流电机 + 4 路电磁离合」，最少要 7 只 IO。
C3 和 S3 的可用 IO 完全不同，所以引脚表拆成两份：

    python_code/board_c3.py   ← ESP32-C3 的引脚表 / 保留脚 / strapping
    python_code/board_s3.py   ← ESP32-S3 的引脚表 / 保留脚 / strapping / 剩余可用 IO

编译时本脚本会在输出目录里生成一个 `board_select.py`：

    BOARD = "c3"        # --board c3
    BOARD = "s3"        # --board s3
    BOARD = "auto"      # --board auto

设备上的 hardware_config.py 读到它就加载对应那份引脚表，
所以「两个开发板的固件」是同一套业务代码、两套引脚配置，互不干扰。

用法：
    python tools/build_mpy.py                    # 默认 --board both：C3 + S3 各出一个包
    python tools/build_mpy.py --board c3         # 只编 C3
    python tools/build_mpy.py --board s3         # 只编 S3
    python tools/build_mpy.py --board auto       # 出一份通用包，设备上按芯片自动识别
    python tools/build_mpy.py -o build/out       # 指定输出根目录（默认 dist/）
    python tools/build_mpy.py --bytecode 6       # 指定 .mpy 字节码版本
    python tools/build_mpy.py --compat 1.23.0    # 按指定 MicroPython 版本编译
    python tools/build_mpy.py --no-zip           # 不打包 zip

产物布局（--board both）：
    dist/c3/           C3 的文件系统内容（含 BOARD = "c3" 的 board_select.py）
    dist/s3/           S3 的文件系统内容（含 BOARD = "s3" 的 board_select.py）
    dist/esp32c3-ams-mpy.zip
    dist/esp32s3-ams-mpy.zip

依赖：
    pip install mpy-cross
    版本要和板子上的 MicroPython 固件版本匹配（版本号就是 MicroPython 的发布版本号），
    对不上会在设备上报 "incompatible .mpy file"。
    mpy-cross 也提供 --compat / --bytecode 参数来生成老版本兼容的 .mpy。
"""

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(PROJECT_ROOT, "python_code")
DEFAULT_OUT_DIR = os.path.join(PROJECT_ROOT, "dist")

# 必须保持 .py 的启动脚本（MicroPython 直接 exec 文件内容，不能是 .mpy）
KEEP_AS_SOURCE = {"boot.py", "main.py"}
# 原样拷贝的静态资源（设备上要用 open() 读）
COPY_AS_IS = {"index.html", "config.json"}
# 不参与打包的目录
SKIP_DIRS = {".idea", "__pycache__", ".git", ".vscode", ".pytest_cache"}
# 由本脚本生成的文件：源码目录里就算有也不参与编译，避免和生成结果打架
GENERATED_SOURCES = {"board_select.py"}

BOARD_SELECT_FILE = "board_select.py"

# ===========================================================================
# 板型表：--board 的取值 → 输出子目录 / zip 名 / 芯片名
# ===========================================================================
BOARD_TARGETS = {
    "c3": {
        "board_id": "c3",
        "subdir": "c3",
        "zip": "esp32c3-ams-mpy",
        "chip": "esp32c3",
        "label": "ESP32-C3",
    },
    "s3": {
        "board_id": "s3",
        "subdir": "s3",
        "zip": "esp32s3-ams-mpy",
        "chip": "esp32s3",
        "label": "ESP32-S3（42 针）",
    },
    "auto": {
        "board_id": "auto",
        "subdir": "auto",
        "zip": "esp32-ams-mpy",
        "chip": "esp32",
        "label": "通用包（设备上自动识别芯片）",
    },
}

BOARD_CHOICES = ("both", "c3", "s3", "auto")


class Colors:
    OK = "\033[92m"
    WARN = "\033[93m"
    ERR = "\033[91m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    END = "\033[0m"

    @classmethod
    def disable(cls):
        cls.OK = cls.WARN = cls.ERR = cls.DIM = cls.BOLD = cls.END = ""


def log(msg, color=""):
    print("%s%s%s" % (color, msg, Colors.END))


def find_mpy_cross():
    """找到可用的 mpy-cross，返回命令行前缀（列表）"""
    exe = shutil.which("mpy-cross")
    if exe:
        return [exe]
    exe = shutil.which("mpy-cross.exe")
    if exe:
        return [exe]
    # 退路：python -m mpy_cross
    try:
        subprocess.run([sys.executable, "-m", "mpy_cross", "--version"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return [sys.executable, "-m", "mpy_cross"]
    except Exception:
        return None


def mpy_version(mpy_cross):
    try:
        out = subprocess.run(mpy_cross + ["--version"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return out.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return "unknown"


def compile_one(mpy_cross, src, dst, extra_args):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    cmd = list(mpy_cross) + list(extra_args) + ["-O2", "-o", dst, src]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise RuntimeError("编译失败: %s\n%s"
                           % (os.path.relpath(src, PROJECT_ROOT),
                              result.stdout.decode("utf-8", "replace")))
    return os.path.getsize(dst)


def collect_files():
    """遍历 python_code/，返回 [(绝对路径, 相对路径)]，跳过 .idea 等目录"""
    files = []
    for root, dirs, names in os.walk(SRC_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if name in GENERATED_SOURCES:
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, SRC_DIR)
            files.append((full, rel))
    return sorted(files, key=lambda item: item[1])


# ---------------------------------------------------------------------------
# 读板型文件：把板子自己的引脚表渲染进部署说明，保证"说明和配置一致"
# ---------------------------------------------------------------------------
def load_board_module(board_id):
    """加载 python_code/board_<id>.py；board_id == "auto" 或文件不存在时返回 None"""
    if board_id not in ("c3", "s3"):
        return None
    path = os.path.join(SRC_DIR, "board_%s.py" % board_id)
    if not os.path.isfile(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("_board_%s" % board_id, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001
        return None


def render_board_pins(board_id):
    """返回几行"这块板实际用哪些脚、还剩哪些脚"，写进部署说明"""
    module = load_board_module(board_id)
    if module is None:
        return ["  板型      : 通用包 —— 设备上按 os.uname().machine 自动选择引脚表"]

    def gpio(value):
        return "GPIO%s" % value if value is not None else "未使用"

    lines = [
        "  板型      : %s" % module.BOARD_NAME,
        "  芯片      : %s" % module.CHIP,
        "  电机 IN1  : %s   （方向 1 = 进料）" % gpio(module.MOTOR_PIN_IN1),
        "  电机 IN2  : %s   （方向 -1 = 退料）" % gpio(module.MOTOR_PIN_IN2),
    ]
    for index, pin in enumerate(module.CLUTCH_PINS, 1):
        lines.append("  电磁离合%d : %s" % (index, gpio(pin)))
    lines.append("  状态 LED  : %s" % gpio(module.LED_PIN))

    limit = getattr(module, "LIMIT_SWITCH_PINS", ())
    if limit and all(p is None for p in limit):
        lines.append("  到位开关  : 未安装（降级模式：按时间推进）")
    else:
        for index, pin in enumerate(limit, 1):
            lines.append("  到位开关%d : %s" % (index, gpio(pin)))

    spare = getattr(module, "SPARE_PINS", ())
    lines.append("  剩余可用  : %s"
                 % (", ".join("GPIO%d" % p for p in spare) if spare else "无"))
    strap_spare = getattr(module, "STRAPPING_SPARE_PINS", ())
    if strap_spare:
        lines.append("  剩余可用(strapping 脚，仅建议作输入): %s"
                     % ", ".join("GPIO%d" % p for p in strap_spare))
    recommended = getattr(module, "RECOMMENDED_LIMIT_SWITCH_PINS", ())
    if recommended and all(p is None for p in limit):
        lines.append("  到位开关推荐脚: %s"
                     % ", ".join("GPIO%d" % p for p in recommended))
    return lines


def write_board_select(out_dir, board_id):
    """在输出目录生成 board_select.py —— 这就是"编译时自动套用对应配置"的开关"""
    path = os.path.join(out_dir, BOARD_SELECT_FILE)
    text = (
        "# board_select.py -- 由 tools/build_mpy.py 自动生成，请勿手改\n"
        "# ==========================================================\n"
        "# 这个文件决定了设备上 hardware_config.py 加载哪一份引脚表：\n"
        "#\n"
        "#     BOARD = \"c3\"    -> python_code/board_c3.py（ESP32-C3）\n"
        "#     BOARD = \"s3\"    -> python_code/board_s3.py（ESP32-S3 42 针）\n"
        "#     BOARD = \"auto\"  -> 由 os.uname().machine 现场识别芯片\n"
        "#\n"
        "# 之所以编译期就写死，是为了让两个开发板各自的固件\"天生\"用对配置，\n"
        "# 不依赖运行时字符串识别。删掉本文件也不会坏：\n"
        "# hardware_config.py 会自动退回按芯片识别。\n"
        "\n"
        "BOARD = \"%s\"\n" % board_id
    )
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def write_deploy_note(out_dir, mpy_version_str, target):
    """每个板型一份部署说明，里面直接写出"这块板用哪些脚" """
    note = """YAO_AMS 部署包说明
====================

本目录是通过 tools/build_mpy.py 自动生成的，可以直接整体上传到
{label}（芯片 {chip}）。

构建信息
--------
  mpy-cross 版本 : {mpy_version}
  目标板型       : {label}
  编译期配置     : board_select.py  里 BOARD = "{board_id}"

★ 本包对应的引脚配置（编译时已自动选好，不要再手动改 board_select.py）
{board_pins}

目录内容
--------
  *.mpy           已交叉编译的模块（板子上按普通模块 import）
  boot.py         启动前脚本（保持源码，MicroPython 直接 exec）
  main.py         上电自动启动入口（保持源码）
  board_select.py 板型选择（本脚本生成，决定用哪份引脚表）
  board_c3.mpy    两份引脚表都会随包发出（编译成 .mpy），
  board_s3.mpy    设备按上面的选择只加载其中一份
  bambu/          MQTT 与拓竹指令相关模块
  index.html      Web 配置页面（运行时用 open() 读取，必须放文件系统）
  config.json     配置文件（首次可以不放，设备会自动生成）

上传方法（任选其一）
--------------------
  1) mpremote（推荐，pip install mpremote）
       mpremote connect /dev/ttyACM0 fs cp -r ./ :
       # Windows 下 COM 口形如 COM5，用
       #   mpremote connect COM5 fs cp -r . :
       # ⚠️ 上传前先确认包里的 board_select.py 是你要的那块板

  2) Thonny
       打开 Thonny -> 工具 -> 文件，连上串口后把本目录的文件拖到设备根目录
       （bambu 子目录要手动新建并上传进去）

  3) ampy / rshell / 其他工具同理

  4) 干脆不手动传：直接用单文件固件（一个 BIN 烧进去就带全套代码）
       esp32c3-ams-firmware.bin / esp32s3-ams-firmware.bin —— 见项目 README

注意事项
--------
  · .mpy 的字节码版本必须和板子上的 MicroPython 固件版本匹配，
    否则会报 ValueError: incompatible .mpy file。
    本包由 mpy-cross {mpy_version} 生成。
    如果版本不匹配，请改用对应版本重新构建：
        pip install mpy-cross==<固件版本号>
        python tools/build_mpy.py --board {board_id}

  · 想换引脚：改 python_code/board_{board_id_file}.py（板型专属）
    或 hardware_config.py（业务参数），然后重新执行本脚本再上传。
    现场临时改也可以在板子上放一个 board_override.py，不用动仓库代码。

  · 上传完断电重启，设备会自动运行 main.py；
    连不上 WiFi 时会打开热点 AMS_WIFI（密码 A12345678）供初次配置。
""".format(
        label=target["label"],
        chip=target["chip"],
        mpy_version=mpy_version_str,
        board_id=target["board_id"],
        board_id_file=target["board_id"] if target["board_id"] in ("c3", "s3") else "c3",
        board_pins="\n".join(render_board_pins(target["board_id"])),
    )
    path = os.path.join(out_dir, "部署说明.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(note)
    return path


def zip_dir(src_dir, base_name):
    """把目录内容打成 zip，返回 zip 路径"""
    return shutil.make_archive(base_name, "zip", root_dir=src_dir)


def build_one(mpy_cross, extra_args, out_dir, target, version_str, keep):
    """编译一个板型的部署包，返回 (compiled, copied, total_bytes, errors)"""
    log("")
    log("===== 板型 %s（%s）=====" % (target["board_id"], target["chip"]), Colors.BOLD)

    if os.path.exists(out_dir) and not keep:
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    compiled, copied, total_bytes, errors = 0, 0, 0, []

    for full, rel in collect_files():
        base = os.path.basename(rel)
        dst = os.path.join(out_dir, rel)

        if base in KEEP_AS_SOURCE or base in COPY_AS_IS or not base.endswith(".py"):
            # 启动脚本 / 静态资源：原样拷贝
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(full, dst)
            copied += 1
            log("  拷贝  %-42s %6d B" % (rel, os.path.getsize(dst)), Colors.DIM)
        else:
            try:
                size = compile_one(mpy_cross, full, dst[:-3] + ".mpy", extra_args)
            except RuntimeError as e:
                errors.append(str(e))
                log("  失败  %s" % rel, Colors.ERR)
                continue
            compiled += 1
            total_bytes += size
            log("  编译  %-42s %6d B" % (rel[:-3] + ".mpy", size), Colors.DIM)

    select_path = write_board_select(out_dir, target["board_id"])
    log("  生成  %-42s      -> BOARD = \"%s\""
        % (BOARD_SELECT_FILE, target["board_id"]), Colors.OK)

    write_deploy_note(out_dir, version_str, target)

    return compiled, copied, total_bytes, errors, select_path


def main():
    parser = argparse.ArgumentParser(
        description="把 python_code/ 编译为 .mpy 部署包（支持 ESP32-C3 / ESP32-S3）")
    parser.add_argument("--board", default="both", choices=list(BOARD_CHOICES),
                        help="目标板型：both = C3 与 S3 各编一个包（默认），"
                             "auto = 一份包靠芯片自动识别")
    parser.add_argument("-o", "--out", default=DEFAULT_OUT_DIR,
                        help="输出根目录（默认 dist/），每个板型占一个子目录")
    parser.add_argument("--bytecode", default=None,
                        help="指定 .mpy 字节码版本，如 6")
    parser.add_argument("--compat", default=None,
                        help="按指定 MicroPython 版本编译，如 1.23.0")
    parser.add_argument("--no-zip", action="store_true", help="不生成 zip")
    parser.add_argument("--keep", action="store_true", help="保留已有的输出目录内容")
    parser.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    args = parser.parse_args()

    if args.no_color or not sys.stdout.isatty():
        Colors.disable()

    mpy_cross = find_mpy_cross()
    if not mpy_cross:
        log("找不到 mpy-cross，请先安装：pip install mpy-cross", Colors.ERR)
        log("（也可以 pip install mpy-cross==1.23.0 指定和固件一致的版本）", Colors.DIM)
        return 2

    version_str = mpy_version(mpy_cross)
    log("mpy-cross : %s" % version_str, Colors.DIM)

    extra_args = []
    if args.bytecode:
        extra_args += ["--bytecode", str(args.bytecode)]
    if args.compat:
        extra_args += ["--compat", str(args.compat)]
    if extra_args:
        log("额外编译参数: %s" % " ".join(extra_args), Colors.DIM)

    out_root = os.path.abspath(args.out)
    os.makedirs(out_root, exist_ok=True)

    board_ids = ["c3", "s3"] if args.board == "both" else [args.board]
    log("目标板型 : %s" % ", ".join(board_ids), Colors.DIM)
    log("输出根目录: %s" % out_root, Colors.DIM)

    all_errors = []
    results = []

    for board_id in board_ids:
        target = BOARD_TARGETS[board_id]
        out_dir = os.path.join(out_root, target["subdir"])
        compiled, copied, total_bytes, errors, _select = build_one(
            mpy_cross, extra_args, out_dir, target, version_str, args.keep)
        all_errors.extend(errors)
        results.append((target, out_dir, compiled, copied, total_bytes, errors))

        log("  小结  : %d 个 .mpy（共 %d B），%d 个原样拷贝"
            % (compiled, total_bytes, copied), Colors.OK if not errors else Colors.ERR)

        if not args.no_zip and not errors:
            base_name = os.path.join(out_root, target["zip"])
            zip_path = zip_dir(out_dir, base_name)
            log("  打包  : %s" % zip_path, Colors.OK)

    log("")
    log("=" * 62, Colors.OK)
    log("全部完成：%d 个板型" % len(results), Colors.OK)
    log("=" * 62, Colors.OK)
    for target, out_dir, compiled, copied, total_bytes, errors in results:
        status = "OK" if not errors else ("失败 %d" % len(errors))
        log("  %-10s %-28s %3d mpy / %6d B / %d 拷贝   [%s]"
            % (target["board_id"], os.path.relpath(out_dir, PROJECT_ROOT),
               compiled, total_bytes, copied, status),
            Colors.OK if not errors else Colors.ERR)

    if all_errors:
        log("")
        for e in all_errors:
            log(e, Colors.ERR)
        return 1

    log("")
    log("下一步：把 dist/<板型>/ 整体上传到板子，或直接烧 dist/<芯片>-ams-firmware.bin", Colors.DIM)
    return 0


if __name__ == "__main__":
    sys.exit(main())
