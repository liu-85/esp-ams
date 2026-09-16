#!/usr/bin/env python3
"""
tools/build_mpy.py —— 把 python_code/ 交叉编译成 ESP32-C3 可用的 .mpy 并打包
===========================================================================

MicroPython 的源码可以直接丢到板子上运行，但先在本机交叉编译成 .mpy 有两个好处：

    1. 省 RAM —— .mpy 是字节码，导入时不需要在设备上重新编译源码。
       ESP32-C3 只有 400KB SRAM，这个项目又要跑 MQTT(TLS) + Web 服务，
       省下来的这几十 KB 很关键。
    2. 省 Flash、启动更快 —— 字节码比源码小得多。

哪些文件不能编译：
    boot.py / main.py  —— MicroPython 上电时会直接 exec 这两个文件的内容，
                          必须是 .py 放在文件系统上，不能是 .mpy。

用法：
    python tools/build_mpy.py                    # 默认输出到 dist/
    python tools/build_mpy.py -o build/out       # 指定输出目录
    python tools/build_mpy.py --bytecode 6       # 指定 .mpy 字节码版本
    python tools/build_mpy.py --compat 1.23.0    # 按指定 MicroPython 版本编译
    python tools/build_mpy.py --no-zip           # 不打包 zip

依赖：
    pip install mpy-cross
    版本要和板子上的 MicroPython 固件版本匹配（版本号就是 MicroPython 的发布版本号），
    对不上会在设备上报 "incompatible .mpy file"。
    mpy-cross 也提供 --compat / --bytecode 参数来生成老版本兼容的 .mpy。
"""

import argparse
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


class Colors:
    OK = "\033[92m"
    WARN = "\033[93m"
    ERR = "\033[91m"
    DIM = "\033[2m"
    END = "\033[0m"

    @classmethod
    def disable(cls):
        cls.OK = cls.WARN = cls.ERR = cls.DIM = cls.END = ""


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
    """遍历 python_code/，返回 (相对路径列表)，跳过 .idea 等目录"""
    files = []
    for root, dirs, names in os.walk(SRC_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, SRC_DIR)
            files.append((full, rel))
    return sorted(files, key=lambda item: item[1])


def write_deploy_note(out_dir, mpy_version_str):
    note = """YAO_AMS 部署包说明
====================

本目录是通过 tools/build_mpy.py 自动生成的，可以直接整体上传到 ESP32-C3。

构建信息
--------
  mpy-cross 版本 : {mpy_version}

目录内容
--------
  *.mpy           已交叉编译的模块（板子上按普通模块 import）
  boot.py         启动前脚本（保持源码，MicroPython 直接 exec）
  main.py         上电自动启动入口（保持源码）
  bambu/          MQTT 与拓竹指令相关模块
  index.html      Web 配置页面（运行时用 open() 读取，必须放文件系统）
  config.json     配置文件（首次可以不放，设备会自动生成）

上传方法（任选其一）
--------------------
  1) mpremote（推荐，pip install mpremote）
       mpremote connect /dev/ttyACM0 fs cp -r ./ :
       # Windows 下 COM 口形如 COM5，用
       #   mpremote connect COM5 fs cp -r . :

  2) Thonny
       打开 Thonny -> 工具 -> 文件，连上串口后把本目录的文件拖到设备根目录
       （bambu 子目录要手动新建并上传进去）

  3) ampy / rshell / 其他工具同理

注意事项
--------
  · .mpy 的字节码版本必须和板子上的 MicroPython 固件版本匹配，
    否则会报 ValueError: incompatible .mpy file。
    本包由 mpy-cross {mpy_version} 生成。
    如果版本不匹配，请改用对应版本重新构建：
        pip install mpy-cross==<固件版本号>
        python tools/build_mpy.py

  · 改过 hardware_config.py 里的引脚后，请重新执行本脚本再上传。

  · 上传完断电重启，设备会自动运行 main.py；
    连不上 WiFi 时会打开热点 AMS_WIFI（密码 A12345678）供初次配置。
""".format(mpy_version=mpy_version_str)
    path = os.path.join(out_dir, "部署说明.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(note)
    return path


def zip_dir(src_dir, base_name):
    """把目录内容打成 zip（顶层目录固定为 esp32c3-ams/）"""
    zip_path = shutil.make_archive(base_name, "zip", root_dir=src_dir)
    return zip_path


def main():
    parser = argparse.ArgumentParser(description="把 python_code/ 编译为 .mpy 部署包")
    parser.add_argument("-o", "--out", default=DEFAULT_OUT_DIR,
                        help="输出目录（默认 dist/）")
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

    out_dir = os.path.abspath(args.out)
    if os.path.exists(out_dir) and not args.keep:
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    files = collect_files()
    compiled, copied, total_bytes, errors = 0, 0, 0, []

    for full, rel in files:
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

    write_deploy_note(out_dir, version_str)

    log("")
    log("编译完成: %d 个 .mpy（共 %d B），%d 个原样拷贝" % (compiled, total_bytes, copied), Colors.OK)

    if errors:
        log("")
        for e in errors:
            log(e, Colors.ERR)
        return 1

    if not args.no_zip:
        base_name = os.path.join(os.path.dirname(out_dir), "esp32c3-ams-mpy")
        zip_path = zip_dir(out_dir, base_name)
        log("打包完成: %s" % zip_path, Colors.OK)

    log("输出目录: %s" % out_dir, Colors.OK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
