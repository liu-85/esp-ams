#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建「单文件烧录」固件 —— 把 MicroPython 官方固件和本项目的代码合成一个 BIN。

为什么需要它
------------
常规做法是「先烧固件、再用 mpremote/Thonny 把 .py 一个个传上去」，两步而且容易漏文件。
这个脚本产出的 BIN 从地址 0x0 开始、覆盖整片 4MB Flash，里面已经包含：

    0x000000  ESP32-C3 引导程序
    0x008000  分区表
    0x010000  MicroPython 应用（官方发布的固件，未做任何修改）
    0x200000  文件系统（littlefs v2），放着本项目全部 .py / index.html / umqtt

也就是说：一条命令烧一个文件，板子插上电就直接跑起来，不需要再传任何文件。

用了哪些手段保证「烧进去一定能启动」
------------------------------------
1. 文件系统镜像由 littlefs 2.8（上游源码）生成，版本与 MicroPython v1.23 内置的
   lib/littlefs 完全一致；脚本还会联网核对两边版本号，不一致直接报错退出。
   这一点很关键：ESP32 端 _boot.py 挂载失败时会走 inisetup.check_bootsec()，
   若首扇区不是 0xFF 就判定「文件系统损坏」并进入死循环，必须一次做对。
2. 所有参数（block_size=4096 / block_count / name_max / file_max / attr_max /
   block_cycles / read_size / prog_size / lookahead）都与设备端 VfsLfs2.mkfs 对齐。
3. 生成后立刻用同一份 littlefs 代码回挂载、逐文件逐字节比对（lfs_mkfs verify）。
4. 再用另一套独立实现（littlefs-python，若已安装）读取最终 BIN 里的文件系统交叉验证。
5. 分区表是从官方固件里解析出来的，不写死偏移，官方换布局也不会错。

用法
----
    python tools/make_firmware_bin.py \
        --firmware-url https://micropython.org/resources/firmware/ESP32_GENERIC_C3-20240602-v1.23.0.bin \
        --firmware-sha256 8058b7d6eb55f8124fbdcc797e2e8b39ae947a18df635567e02c8786874c04fd \
        --lfs-tool tools/build/lfs_mkfs \
        --out dist/esp32c3-ams-firmware.bin

烧录（整片覆盖，正常情况不需要先擦除；升级/异常时可用 erase_flash 救援）：

    esptool.py --chip esp32c3 --port COM3 write_flash -z 0x0 dist/esp32c3-ams-firmware.bin
"""

import argparse
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
PARTITION_TABLE_OFFSET = 0x8000       # ESP-IDF 分区表固定位置
PARTITION_ENTRY_SIZE = 32
PARTITION_MAGIC = b"\xaa\x50"
FLASH_SECTOR = 4096                   # ESP32-C3 闪存擦除粒度，也是文件系统的 block_size

DEFAULT_SOURCE = "python_code"
DEFAULT_OUT = os.path.join("dist", "esp32c3-ams-firmware.bin")
DEFAULT_MICROPYTHON_VERSION = "1.23.0"

# 不进文件系统的目录 / 文件
SKIP_DIRS = {"__pycache__", ".idea", ".git", ".vscode", ".mypy_cache", ".pytest_cache"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".mpy", ".swp", ".swo", ".tmp", ".log", ".bak")
# ⚠️ config.json / wifi.dat 含 WiFi 密码和打印机访问码，绝不能打进固件
SKIP_FILES = {"config.json", "wifi.dat", "boot_stat.json", ".DS_Store", "Thumbs.db", ".gitignore"}


def log(msg):
    print("[固件构建] %s" % msg)


def warn(msg):
    print("[固件构建][警告] %s" % msg)


def die(msg):
    print("[固件构建][错误] %s" % msg, file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# 下载 / 校验
# ---------------------------------------------------------------------------
def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def download(url, dest, expected_sha256=None, refresh=False):
    """下载官方固件，带缓存；已存在且校验通过则直接复用"""
    if os.path.exists(dest) and not refresh:
        size = os.path.getsize(dest)
        if size > 100 * 1024:
            if expected_sha256:
                got = sha256_of(dest)
                if got == expected_sha256.lower():
                    log("复用已缓存固件: %s（校验通过）" % dest)
                    return dest
                warn("缓存固件校验不匹配，重新下载")
            else:
                log("复用已缓存固件: %s" % dest)
                return dest

    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    log("下载官方固件: %s" % url)
    req = urllib.request.Request(url, headers={"User-Agent": "esp-ams-firmware-builder"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
            shutil.copyfileobj(resp, out)
    except urllib.error.HTTPError as exc:
        die("固件下载失败 HTTP %s: %s\n"
            "    地址可能已失效，请到 https://micropython.org/download/ESP32_GENERIC_C3/ "
            "复制最新固件链接（含构建日期的那串）后重试" % (exc.code, url))
    except Exception as exc:  # noqa: BLE001
        die("固件下载失败: %s" % exc)

    size = os.path.getsize(dest)
    log("下载完成: %d 字节 (%.2f MB)" % (size, size / 1048576.0))
    if size < 100 * 1024:
        die("下载到的文件太小（%d 字节），可能不是固件" % size)

    if expected_sha256:
        got = sha256_of(dest)
        if got != expected_sha256.lower():
            die("固件 SHA256 校验失败\n    期望: %s\n    实际: %s\n"
                "    若官方重新发布了同版本固件，请更新工作流里的 MICROPYTHON_FIRMWARE_SHA256"
                % (expected_sha256.lower(), got))
        log("SHA256 校验通过: %s" % got)
    return dest


# ---------------------------------------------------------------------------
# 分区表
# ---------------------------------------------------------------------------
def parse_partition_table(data):
    """解析 ESP-IDF 分区表，返回 [{name, type, subtype, offset, size}, ...]"""
    parts = []
    pos = PARTITION_TABLE_OFFSET
    while pos + PARTITION_ENTRY_SIZE <= len(data):
        entry = data[pos:pos + PARTITION_ENTRY_SIZE]
        if entry[0:2] != PARTITION_MAGIC:
            break
        ptype = entry[2]
        subtype = entry[3]
        offset, size = struct.unpack("<II", entry[4:12])
        name = entry[12:28].split(b"\x00")[0].decode("utf-8", "replace")
        parts.append({
            "name": name,
            "type": ptype,
            "subtype": subtype,
            "offset": offset,
            "size": size,
        })
        pos += PARTITION_ENTRY_SIZE
    return parts


def find_partition(parts, name):
    for p in parts:
        if p["name"] == name:
            return p
    return None


# ---------------------------------------------------------------------------
# 文件清单
# ---------------------------------------------------------------------------
def collect_files(source_dir):
    """遍历源码目录，返回 (目录列表, 文件列表)，路径统一成镜像内的绝对路径"""
    if not os.path.isdir(source_dir):
        die("源码目录不存在: %s" % source_dir)

    dirs = []
    files = []
    for root, subdirs, filenames in os.walk(source_dir):
        subdirs[:] = sorted(d for d in subdirs if d not in SKIP_DIRS and not d.startswith("."))
        rel_root = os.path.relpath(root, source_dir)
        for name in sorted(subdirs):
            rel = name if rel_root == "." else os.path.join(rel_root, name)
            dirs.append("/" + rel.replace(os.sep, "/"))
        for name in sorted(filenames):
            if name in SKIP_FILES or name.startswith("."):
                continue
            if name.lower().endswith(SKIP_SUFFIXES):
                continue
            rel = name if rel_root == "." else os.path.join(rel_root, name)
            files.append((os.path.abspath(os.path.join(root, name)),
                          "/" + rel.replace(os.sep, "/")))
    return dirs, files


def write_manifest(path, dirs, files):
    lines = ["# lfs_mkfs 清单：D=目录 F=源文件<制表符>镜像内路径"]
    for d in dirs:
        lines.append("D\t%s" % d)
    for src, dst in files:
        lines.append("F\t%s\t%s" % (src, dst))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# 调用 littlefs 工具
# ---------------------------------------------------------------------------
def run_tool(tool, args):
    cmd = [tool] + args
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = proc.stdout.decode("utf-8", "replace")
    if out.strip():
        for line in out.strip().splitlines():
            print("    %s" % line)
    if proc.returncode != 0:
        die("命令执行失败（退出码 %d）: %s" % (proc.returncode, " ".join(cmd)))
    return out


def resolve_tool(explicit):
    """找 lfs_mkfs 可执行文件；找不到就给出编译方法"""
    candidates = []
    if explicit:
        candidates.append(explicit)
    else:
        exe = "lfs_mkfs.exe" if os.name == "nt" else "lfs_mkfs"
        candidates.append(os.path.join("tools", "build", exe))
        candidates.append(os.path.join("tools", "build", "lfs_mkfs"))
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    die("找不到 littlefs 镜像工具（lfs_mkfs）。\n"
        "    它由 tools/lfs_mkfs.c 配合 littlefs 源码编译而来，编译方法：\n"
        "      git clone --depth 1 -b v2.8.0 https://github.com/littlefs-project/littlefs\n"
        "      gcc -O2 -o tools/build/lfs_mkfs tools/lfs_mkfs.c \\\n"
        "          /path/to/littlefs/lfs.c /path/to/littlefs/lfs_util.c -I/path/to/littlefs\n"
        "    （GitHub Actions 里这一步是自动的，平时直接用 CI 产物即可）")


def check_lfs_version(tool, micropython_version, strict):
    """核对镜像工具用的 littlefs 版本 == 该版本 MicroPython 内置的 littlefs 版本"""
    out = run_tool(tool, ["version"])
    version_hex = None
    for line in out.splitlines():
        if line.startswith("LFS_VERSION="):
            version_hex = line.split("=", 1)[1].strip()
    if version_hex is None:
        warn("无法从镜像工具读取版本号，跳过一致性检查")
        return

    url = ("https://raw.githubusercontent.com/micropython/micropython/v%s/"
           "lib/littlefs/lfs2.h" % micropython_version)
    log("核对 littlefs 版本: 镜像工具 %s / MicroPython v%s 内置" % (version_hex, micropython_version))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "esp-ams-firmware-builder"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            header = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        msg = "无法联网核对 littlefs 版本（%s）" % exc
        if strict:
            die(msg)
        warn(msg + "，已跳过该检查")
        return

    import re
    m = re.search(r"#define LFS2_VERSION\s+(0x[0-9a-fA-F]+)", header)
    if not m:
        warn("未能从 MicroPython 源码解析出 LFS2_VERSION，跳过该检查")
        return
    expected = int(m.group(1), 16)
    actual = int(version_hex, 16)
    if expected != actual:
        die("littlefs 版本不一致！\n"
            "    镜像工具用的 littlefs: %s\n"
            "    MicroPython v%s 内置:   0x%08x\n"
            "    两者不同会导致设备挂载文件系统失败，必须把 littlefs 源码换成与 MicroPython "
            "相同的小版本（工作流里的 LITTLEFS_VERSION）" % (version_hex, micropython_version, expected))
    log("littlefs 版本一致: 0x%08x" % actual)


# ---------------------------------------------------------------------------
# 交叉验证（可选，用另一套独立实现读一遍最终镜像）
# ---------------------------------------------------------------------------
def cross_check(vfs_bytes, block_size, block_count, files):
    try:
        from littlefs import LittleFS, UserContext
    except ImportError:
        warn("未安装 littlefs-python，跳过独立实现交叉验证"
             "（可执行 pip install littlefs-python 启用）")
        return True

    log("用独立实现（littlefs-python）交叉验证镜像...")
    ctx = UserContext(buffer=bytearray(vfs_bytes))
    fs = LittleFS(context=ctx, block_size=block_size, block_count=block_count, mount=True)

    found = {}

    def walk(path):
        try:
            names = fs.listdir(path)
        except Exception:  # noqa: BLE001
            return
        for name in names:
            full = (path.rstrip("/") + "/" + name) if path != "/" else "/" + name
            try:
                st = fs.stat(full)
                is_dir = str(getattr(st, "type", "")).lower().find("dir") >= 0 or \
                    getattr(st, "type", None) == 2
            except Exception:  # noqa: BLE001
                is_dir = False
            if is_dir:
                walk(full)
            else:
                with fs.open(full, "rb") as fh:
                    found[full] = fh.read()

    walk("/")

    expected = {}
    for src, dst in files:
        with open(src, "rb") as f:
            expected[dst] = f.read()

    missing = sorted(set(expected) - set(found))
    extra = sorted(set(found) - set(expected))
    diff = sorted(p for p in set(expected) & set(found) if expected[p] != found[p])
    if missing or extra or diff:
        die("交叉验证失败：缺失 %s / 多余 %s / 内容不符 %s" % (missing, extra, diff))
    log("交叉验证通过：%d 个文件全部可读且内容一致" % len(found))
    return True


def verify_bin(path, vfs):
    """产物自检：确认写出来的 BIN 确实是我们以为的东西"""
    with open(path, "rb") as f:
        data = f.read()
    if not data or data[0] != 0xE9:
        die("产物文件开头不是 0xE9（ESP 镜像魔数），说明拼接有误")
    parts = parse_partition_table(data)
    got = find_partition(parts, "vfs")
    if got is None or got["offset"] != vfs["offset"] or got["size"] != vfs["size"]:
        die("产物里的分区表与预期不一致（vfs 位置/大小变了）")

    # 关键检查：设备端 _boot.py 执行的是 vfs.mount(bdev, "/")，它靠块 0/1 里
    # 偏移 8 处的 "littlefs" 魔数来自动识别文件系统（见 MicroPython
    # extmod/vfs.c 的 mp_vfs_autodetect）。魔数不对就会挂载失败，进而走到
    # inisetup.check_bootsec()，判定「文件系统损坏」后死循环。
    for block in (0, 1):
        base = vfs["offset"] + block * FLASH_SECTOR
        magic = bytes(data[base + 8:base + 16])
        if magic != b"littlefs":
            die("文件系统魔数校验失败：块 %d 偏移 8 处是 %r，应为 b'littlefs'。\n"
                "    设备将无法自动挂载（表现为反复打印文件系统损坏）。"
                % (block, magic))

    if len(data) < vfs["offset"] + vfs["size"]:
        warn("产物在文件系统分区结尾之前就结束了，属于 --trim 的正常情况")
    return data


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        description="把 MicroPython 官方固件和本项目代码合成一个可直接烧录的 BIN",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_argument_group("来源")
    src.add_argument("--source", default=DEFAULT_SOURCE,
                     help="要打进固件的源码目录（默认 %(default)s）")
    src.add_argument("--firmware-url", default=None, help="MicroPython 官方固件下载地址")
    src.add_argument("--firmware-file", default=None, help="本地已有的官方固件（离线构建用）")
    src.add_argument("--firmware-sha256", default=None, help="官方固件 SHA256，填了就强校验")
    src.add_argument("--micropython-version", default=DEFAULT_MICROPYTHON_VERSION,
                     help="对照检查 littlefs 版本的 MicroPython 版本（默认 %(default)s）")

    out = p.add_argument_group("产物")
    out.add_argument("--out", default=DEFAULT_OUT, help="输出的 BIN 路径（默认 %(default)s）")
    out.add_argument("--work-dir", default=os.path.join("dist", "_work"),
                     help="中间文件目录（清单、文件系统镜像）")
    out.add_argument("--cache-dir", default=os.path.join("dist", "_cache"),
                     help="官方固件缓存目录")
    out.add_argument("--trim", action="store_true",
                     help="裁掉结尾全 0xFF 的区域让文件更小。默认不裁：完整覆盖整片 Flash，"
                          "这样即使不先擦除也能把旧文件系统清干净")

    tool = p.add_argument_group("工具")
    tool.add_argument("--lfs-tool", default=None, help="lfs_mkfs 可执行文件路径")
    tool.add_argument("--strict-version-check", action="store_true",
                      help="联网核对 littlefs 版本失败时直接报错（CI 用）")
    tool.add_argument("--no-cross-check", action="store_true", help="跳过独立实现交叉验证")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    root = os.path.abspath(os.getcwd())

    tool = resolve_tool(args.lfs_tool)
    log("镜像工具: %s" % tool)
    check_lfs_version(tool, args.micropython_version, args.strict_version_check)

    # 1. 拿到官方固件
    if args.firmware_file:
        firmware = args.firmware_file
        if not os.path.isfile(firmware):
            die("本地固件不存在: %s" % firmware)
        if args.firmware_sha256:
            got = sha256_of(firmware)
            if got != args.firmware_sha256.lower():
                die("固件 SHA256 校验失败\n    期望: %s\n    实际: %s"
                    % (args.firmware_sha256.lower(), got))
            log("SHA256 校验通过")
    elif args.firmware_url:
        cached = os.path.join(args.cache_dir, os.path.basename(args.firmware_url))
        firmware = download(args.firmware_url, cached, args.firmware_sha256)
    else:
        die("必须提供 --firmware-url 或 --firmware-file")

    with open(firmware, "rb") as f:
        fw = f.read()
    log("官方固件: %s (%d 字节)" % (firmware, len(fw)))

    # 2. 读分区表，定位文件系统分区
    parts = parse_partition_table(fw)
    if not parts:
        die("在 0x%x 处没解析到分区表，这个文件可能不是完整固件（MicroPython 的 C3 固件是从 0x0 开始的整片镜像）"
            % PARTITION_TABLE_OFFSET)
    log("分区表:")
    for p in parts:
        print("    %-10s type=0x%02x subtype=0x%02x offset=0x%06x size=0x%06x (%d KB)"
              % (p["name"], p["type"], p["subtype"], p["offset"], p["size"], p["size"] // 1024))

    vfs = find_partition(parts, "vfs")
    if vfs is None:
        die("分区表里没有 vfs 分区，无法放置文件系统")
    if vfs["size"] % FLASH_SECTOR != 0:
        die("vfs 分区大小 %d 不是 %d 的整数倍，无法用作 littlefs" % (vfs["size"], FLASH_SECTOR))
    block_size = FLASH_SECTOR
    block_count = vfs["size"] // FLASH_SECTOR
    log("文件系统分区: offset=0x%06x size=%d KB → block_size=%d block_count=%d"
        % (vfs["offset"], vfs["size"] // 1024, block_size, block_count))

    if len(fw) > vfs["offset"]:
        die("固件(%d 字节)已经越过 vfs 分区起点(0x%06x)，说明官方固件布局变了，需要更新脚本"
            % (len(fw), vfs["offset"]))

    # 3. 生成文件清单
    os.makedirs(args.work_dir, exist_ok=True)
    dirs, files = collect_files(args.source)
    manifest = write_manifest(os.path.join(args.work_dir, "manifest.txt"), dirs, files)
    total_bytes = sum(os.path.getsize(src) for src, _ in files)
    log("待写入文件 %d 个 / 目录 %d 个，共 %d 字节" % (len(files), len(dirs), total_bytes))
    for _, dst in files:
        print("    %s" % dst)
    if total_bytes > vfs["size"]:
        die("文件总量 %d 字节超过分区容量 %d 字节" % (total_bytes, vfs["size"]))

    # 4. 生成 littlefs 镜像
    lfs_image = os.path.join(args.work_dir, "vfs.bin")
    if os.path.exists(lfs_image):
        os.remove(lfs_image)
    log("生成 littlefs 镜像...")
    run_tool(tool, ["create", "--out", lfs_image,
                    "--block-size", str(block_size), "--block-count", str(block_count),
                    "--manifest", manifest])
    actual_size = os.path.getsize(lfs_image)
    if actual_size != vfs["size"]:
        die("镜像大小 %d 与分区大小 %d 不一致" % (actual_size, vfs["size"]))

    # 5. 用同一份 littlefs 代码回读校验
    log("回读校验...")
    run_tool(tool, ["verify", "--img", lfs_image,
                    "--block-size", str(block_size), "--block-count", str(block_count),
                    "--manifest", manifest])

    # 6. 拼成一个 BIN
    total = vfs["offset"] + vfs["size"]
    image = bytearray(b"\xff" * total)
    image[0:len(fw)] = fw
    with open(lfs_image, "rb") as f:
        image[vfs["offset"]:vfs["offset"] + vfs["size"]] = f.read()

    if args.trim:
        end = len(image)
        while end > 0 and image[end - 1] == 0xFF:
            end -= 1
        # 对齐到扇区，免得相邻数据被截断
        end = min(len(image), ((end + FLASH_SECTOR - 1) // FLASH_SECTOR) * FLASH_SECTOR)
        log("裁剪结尾空白: %d 字节 → %d 字节" % (len(image), end))
        image = image[:end]

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "wb") as f:
        f.write(image)

    log("产物自检（重新读回解析镜像头与分区表）...")
    verify_bin(args.out, vfs)

    digest = hashlib.sha256(bytes(image)).hexdigest()
    with open(args.out + ".sha256", "w", encoding="utf-8") as f:
        f.write("%s  %s\n" % (digest, os.path.basename(args.out)))

    # 7. 交叉验证（直接用最终 BIN 里的那一块文件系统区域，而不是中间文件）
    if not args.no_cross_check:
        vfs_slice = bytes(image[vfs["offset"]:vfs["offset"] + vfs["size"]])
        if len(vfs_slice) < vfs["size"]:
            vfs_slice = vfs_slice + b"\xff" * (vfs["size"] - len(vfs_slice))
        log("对最终 BIN 内的文件系统做独立实现交叉验证...")
        cross_check(vfs_slice, block_size, block_count, files)

    # 8. 汇总
    print("")
    print("=" * 72)
    print("构建完成")
    print("=" * 72)
    print("  产物      : %s" % os.path.abspath(args.out))
    print("  大小      : %d 字节 (%.2f MB)" % (len(image), len(image) / 1048576.0))
    print("  SHA256    : %s" % digest)
    print("  内容      : 引导程序 + 分区表 + MicroPython v%s + 文件系统(%d 个文件)"
          % (args.micropython_version, len(files)))
    print("")
    print("  烧录（一条命令，整片覆盖，正常不用先擦除）:")
    print("    esptool.py --chip esp32c3 --port COM3 write_flash -z 0x0 %s"
          % os.path.basename(args.out))
    print("  如果板子出现异常，先彻底擦除再烧:")
    print("    esptool.py --chip esp32c3 --port COM3 erase_flash")
    if not args.trim:
        print("  提示: 本文件覆盖整片 4MB Flash，所以旧的文件系统会被一并清除。")
        print("        想得到体积更小的文件可加 --trim（但那样就建议先 erase_flash）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
