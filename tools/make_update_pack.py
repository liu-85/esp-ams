"""
tools/make_update_pack.py —— 生成网页 OTA 用的 .ams 应用更新包
================================================================

这个脚本把 `python_code/` 下的源码和页面打成一个 `.ams` 包，
在设备的「系统升级」页面上传它，设备会把文件写进自己的文件系统然后重启。

--------------------------------------------------------------------------
.ams 包是什么 / 为什么不用固件 BIN
--------------------------------------------------------------------------
本项目的固件里装的是 **.py 源码 + index.html 原文件**（make_firmware_bin.py
直接把文本打进文件系统，不做 .mpy）。而板子的分区表只有单个 factory 分区，
没有 ota_0/ota_1 备用分区 —— 所以没法"把新固件写到备用分区再切过去"。

但业务逻辑和界面全都在文件系统里，随时可以整体替换，于是：
    网页上传 .ams 包 → 设备写文件系统 → 重启
这一条路能更新所有程序与界面，不需要插 USB。

包的格式定义在 `python_code/ota_update.py` 里（设备侧解析用的同一份代码），
这里 import 过来复用，保证"造包"和"解包"永远不会走偏。

用法::

    python tools/make_update_pack.py
    python tools/make_update_pack.py --src python_code --out dist/esp32c3-ams-update.ams
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "python_code"))

from ota_update import build_pack          # noqa: E402  必须在改 sys.path 之后

DEFAULT_SRC = os.path.join(ROOT, "python_code")
DEFAULT_OUT = os.path.join(ROOT, "dist", "esp32c3-ams-update.ams")

# 和设备端 make_firmware_bin.py 保持一致的排除规则：
# 编译产物不进包，设备配置（含密码）**绝不能**进包 ——
# 否则一次升级就把别人的 WiFi 密码和打印机访问码覆盖过去了。
SKIP_DIRS = {"__pycache__", ".idea", ".git", ".vscode", ".mypy_cache", ".pytest_cache"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".mpy", ".swp", ".swo", ".tmp", ".log", ".bak")
SKIP_FILES = {"config.json", "wifi.dat", "boot_stat.json", ".DS_Store", "Thumbs.db", ".gitignore"}


def collect(src):
    """按固定顺序收集要打包的文件，返回 [(相对路径, 内容)]。

    排序是为了让同样的源码产出**逐字节相同**的包，方便对比和校验。
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name in SKIP_FILES:
                continue
            if name.lower().endswith(SKIP_SUFFIXES):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, src).replace(os.sep, "/")
            with open(full, "rb") as handle:
                found.append((rel, handle.read()))
    found.sort(key=lambda item: item[0])
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成网页 OTA 用的 .ams 更新包")
    parser.add_argument("--src", default=DEFAULT_SRC, help="源码目录（默认 python_code）")
    parser.add_argument("--out", default=DEFAULT_OUT, help="输出 .ams 路径")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.src):
        print("[错误] 源码目录不存在: %s" % args.src)
        return 1

    files = collect(args.src)
    if not files:
        print("[错误] %s 下没有可打包的文件" % args.src)
        return 1

    # 包里必须有 index.html 和 main.py，否则刷完就开不了机 / 开不了网页
    names = [name for name, _ in files]
    for required in ("main.py", "index.html", "boot.py"):
        if required not in names:
            print("[错误] 包里缺少 %s —— 打进去会开不了机" % required)
            return 1

    blob = build_pack(files)

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    with open(args.out, "wb") as handle:
        handle.write(blob)

    print("[更新包] 已生成 %s" % args.out)
    print("[更新包] 共 %d 个文件，%d 字节" % (len(files), len(blob)))
    for name, data in files:
        print("    %-32s %7d 字节" % (name, len(data)))
    print("")
    print("用法：设备联网后打开配置页面 → 左侧「系统升级」→ 选择这个 .ams 文件 → 上传。")
    print("     设备写完文件会自动重启，重启后新版本即刻生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
