"""
ota_update.py —— 应用层 OTA（在线更新）核心
============================================

背景：为什么不是"上传固件 BIN"
--------------------------------------------------------------------------
这块板子的分区表是「单个 factory 应用分区 + 2 MB littlefs 文件系统」：

    nvs       0x009000   24 KB
    phy_init  0x00f000    4 KB
    factory   0x010000  1984 KB   ← MicroPython 内核 + 本项目的 .py 全在这里
    vfs       0x200000  2048 KB   ← 文件系统

没有 ota_0 / ota_1 两个备份分区，也没有 otadata。所以**没法**像手机那样
"把新固件写进备用分区再切过去" —— 想那样必须先改分区表并用 USB 重刷一次，
而且写"自己正在运行的 app 分区"本身就极危险。

但本项目有一个很适合 OTA 的特点：**固件里装的是 .py 源码和 index.html 原文件**
（`make_firmware_bin.py` 直接把 python_code/ 下的文本打进文件系统，不做 .mpy）。
也就是说，"换料逻辑"和"网页界面"全都在文件系统里，随时可以整个替换掉。

于是本模块做的是**应用层 OTA**：

    网页上传一个 .ams 更新包 → 设备把里面的文件写进自己的文件系统 → 重启

能更新 AMS_WEB.py / index.html / 换料逻辑……所有业务代码和界面，
不需要插 USB，不动分区表。更新不了 MicroPython 内核本身（内核极少需要动，
真要动还是得插 USB）。

--------------------------------------------------------------------------
.ams 更新包格式（小端）
--------------------------------------------------------------------------
    偏移 0    8 字节   magic  b"AMSUPD1\\n"
    偏移 8    u16      文件个数 file_count
    偏移 10   u16      清单字节数 manifest_bytes
    偏移 12   u32      载荷总字节数 total_payload
    偏移 16   manifest（manifest_bytes 字节），每项：
                 u8   name_len
                 name name_len 字节（相对路径，如 AMS_WEB.py / bambu/bambu_mqtt.py）
                 u32  size
                 u32  crc32（标准 CRC-32，与 zlib.crc32 一致）
    之后      各文件载荷按清单顺序首尾相接

用 `tools/make_update_pack.py` 生成。

--------------------------------------------------------------------------
安全设计：绝不把板子写坏
--------------------------------------------------------------------------
1. **全部先写名字带 .new 的临时文件**，一个都不碰正式文件。
2. 每写完一个文件立刻核对 **长度和 CRC32**；任何一项对不上就整体中止。
3. 只有**所有文件全部校验通过**，才开始把 .new 依次改名成正式名字
   （改名是原子操作，中途掉电最多是"一部分新一部分旧"，不会出现半个文件）。
4. 任何失败路径都会删掉全部 .new，**绝不留下半截文件**，也不重启。
5. 拒绝绝对路径和 `..`，防止更新包写到不该写的地方。
"""

import os

MAGIC = b"AMSUPD1\n"
HEADER_LEN = 16
MANIFEST_MAX = 8192          # 清单上限，防止畸形包把内存吃光
NAME_MAX = 120

# 整机固件 BIN（ESP32 镜像）的第一个字节固定是 0xE9。
# 用户很可能直接把 esp32c3-ams-firmware.bin 拖进来 —— 要给出人话提示，
# 而不是让他对着"格式错误"发呆。
ESP_IMAGE_MAGIC = 0xE9

# 这些文件绝不接受更新包覆盖：里面是 WiFi 密码和打印机访问码，
# 被更新包覆盖一次就等于把设备配置清了。
PROTECTED = ("config.json", "wifi.dat", "boot_stat.json")


class OtaError(Exception):
    """更新包有问题 / 写入失败。message 直接给用户看，所以要写人话。"""


# ---------------------------------------------------------------------------
# CRC-32（标准算法，和 tools 侧的 zlib.crc32 完全一致）
# ---------------------------------------------------------------------------
def _build_table():
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ (0xEDB88320 if (c & 1) else 0)
        table.append(c)
    return table


_CRC_TABLE = _build_table()


def crc32(data, crc=0):
    """标准 CRC-32，可增量调用：crc32(next_chunk, crc32(prev_chunk))。

    与 CPython 的 zlib.crc32 结果一致（有测试钉住这一点），
    所以更新包在 PC 上用 zlib 算、在板子上用这份纯 Python 算，能对上。
    """
    value = (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF
    table = _CRC_TABLE
    for byte in data:
        value = (value >> 8) ^ table[(value ^ byte) & 0xFF]
    return (value ^ 0xFFFFFFFF) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _u16(buf, pos):
    return buf[pos] | (buf[pos + 1] << 8)


def _u32(buf, pos):
    return (buf[pos] | (buf[pos + 1] << 8)
            | (buf[pos + 2] << 16) | (buf[pos + 3] << 24))


def _safe_name(name):
    """校验清单里的文件名，返回规范化后的相对路径。

    更新包来自网络，必须当不可信输入处理：绝对路径和 `..` 一律拒绝，
    否则一个恶意/损坏的包就能覆盖文件系统里任意位置。
    """
    if not name:
        raise OtaError("更新包里有一个空文件名")
    if name.startswith("/") or name.startswith("\\"):
        raise OtaError("更新包包含绝对路径，已拒绝: %s" % name)
    if ":" in name:
        raise OtaError("更新包文件名里有冒号，已拒绝: %s" % name)
    parts = name.replace("\\", "/").split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise OtaError("更新包路径不合法，已拒绝: %s" % name)
    if any(part in PROTECTED for part in parts):
        raise OtaError("更新包试图覆盖受保护文件 %s（含设备配置，已拒绝）" % name)
    return "/".join(parts)


def looks_like_firmware_bin(head):
    """是不是"整机固件 BIN"？是的话要明确告诉用户该走 USB。"""
    return bool(head) and head[0] == ESP_IMAGE_MAGIC


# ---------------------------------------------------------------------------
# 更新包写入器
# ---------------------------------------------------------------------------
class OtaUpdate:
    """流式接收 .ams 更新包并写进文件系统。

    用法（在 async 请求处理里）::

        upd = OtaUpdate()
        while 还有数据:
            upd.feed(await read_some())     # 想喂多少喂多少
        upd.finish()                        # 校验 + 改名（失败会抛 OtaError）

    任何环节出错都调 abort()，它会清掉所有临时文件。
    """

    def __init__(self, total_bytes=0):
        self.total_bytes = total_bytes      # 客户端声明的总长度（仅用于进度）
        self.received = 0
        self.state = "header"               # header -> manifest -> payload -> done
        self.error = None

        self.file_count = 0
        self.manifest_bytes = 0
        self.total_payload = 0
        self.entries = []                   # [{name,size,crc,tmp,got,calc}]

        self._buf = b""
        self._index = -1
        self._fh = None
        self._written_payload = 0

    # ---------------- 进度 ----------------
    @property
    def progress(self):
        if self.total_bytes:
            return min(100, (self.received * 100) // self.total_bytes)
        return 0

    def head(self, n=8):
        """给调用方看开头几个字节，用来识别"整机 BIN"这种明显不对的文件。"""
        return self._buf[:n]

    # ---------------- 主循环：吃数据 ----------------
    def feed(self, chunk):
        """喂入一段原始字节。可以反复调用，直到 finish()。

        ★ 出错时**先自己清干净再抛**：调用方就算忘了调 abort()，
          也不会在文件系统里留下半截 .new 文件。
          （半截文件本身不致命，但"每失败一次就多几个垃圾文件"会慢慢把
          空间吃光，而且下次升级时会看到一堆莫名其妙的残留。）
        """
        if self.error:
            raise OtaError(self.error)
        if chunk:
            self._buf += chunk
            self.received += len(chunk)
        try:
            self._process()
        except OtaError as e:
            self.error = str(e)
            self.abort()
            raise

    def _process(self):
        # ---- 1) 包头 ----
        if self.state == "header":
            if len(self._buf) < HEADER_LEN:
                return
            if self._buf[:8] != MAGIC:
                if looks_like_firmware_bin(self._buf):
                    raise OtaError(
                        "这是整机固件 BIN，不是应用更新包。"
                        "网页 OTA 只能更新程序与界面（.ams 包）；"
                        "要整机升级请用 USB 刷写 "
                        "esp32c3-ams-firmware.bin（C3 板）或 "
                        "esp32s3-ams-firmware.bin（S3 板）")
                raise OtaError("文件格式不对：这不是 .ams 更新包")
            self.file_count = _u16(self._buf, 8)
            self.manifest_bytes = _u16(self._buf, 10)
            self.total_payload = _u32(self._buf, 12)
            if self.file_count <= 0:
                raise OtaError("更新包里没有任何文件")
            if self.manifest_bytes <= 0 or self.manifest_bytes > MANIFEST_MAX:
                raise OtaError("更新包清单长度异常（%d 字节）" % self.manifest_bytes)
            self._buf = self._buf[HEADER_LEN:]
            self.state = "manifest"

        # ---- 2) 文件清单 ----
        if self.state == "manifest":
            if len(self._buf) < self.manifest_bytes:
                return
            self._parse_manifest(self._buf[:self.manifest_bytes])
            self._buf = self._buf[self.manifest_bytes:]
            self.state = "payload"
            self._open_next()

        # ---- 3) 载荷 ----
        while self.state == "payload":
            entry = self.entries[self._index]
            need = entry["size"] - entry["got"]
            if need <= 0:
                self._close_current()
                if not self._open_next():
                    self.state = "done"
                continue
            if not self._buf:
                return
            take = self._buf[:need]
            self._buf = self._buf[need:]
            self._write(take, entry)

        if self.state == "done" and self._buf:
            # 载荷都收完了却还有多余字节 → 包被拼坏了
            raise OtaError("更新包尾部有多余数据，可能已损坏")

    def _parse_manifest(self, raw):
        pos = 0
        total = 0
        for i in range(self.file_count):
            if pos >= len(raw):
                raise OtaError("更新包清单被截断")
            name_len = raw[pos]
            pos += 1
            if name_len <= 0 or name_len > NAME_MAX or pos + name_len > len(raw):
                raise OtaError("更新包清单里的文件名长度异常")
            try:
                name = raw[pos:pos + name_len].decode("utf-8")
            except Exception:
                raise OtaError("更新包清单里的文件名不是合法 UTF-8")
            pos += name_len
            if pos + 8 > len(raw):
                raise OtaError("更新包清单被截断")
            size = _u32(raw, pos)
            crc = _u32(raw, pos + 4)
            pos += 8
            name = _safe_name(name)
            total += size
            self.entries.append({"name": name, "size": size, "crc": crc,
                                 "tmp": name + ".new", "got": 0, "calc": 0})

        if self.total_payload and total != self.total_payload:
            raise OtaError("更新包长度不一致（清单 %d 字节，包头声明 %d 字节）"
                           % (total, self.total_payload))

    # ---------------- 文件写入 ----------------
    def _open_next(self):
        self._index += 1
        if self._index >= len(self.entries):
            return False
        entry = self.entries[self._index]
        self._make_parents(entry["tmp"])
        try:
            self._fh = open(entry["tmp"], "wb")
        except OSError as e:
            raise OtaError("无法创建 %s: %s" % (entry["tmp"], e))
        entry["got"] = 0
        entry["calc"] = 0
        return True

    def _write(self, data, entry):
        try:
            self._fh.write(data)
        except OSError as e:
            raise OtaError("写入 %s 失败: %s" % (entry["tmp"], e))
        entry["got"] += len(data)
        entry["calc"] = crc32(data, entry["calc"])
        self._written_payload += len(data)

    def _close_current(self):
        if self._fh is None:
            return
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None
        entry = self.entries[self._index]
        if entry["got"] != entry["size"]:
            raise OtaError("%s 长度不对（收到 %d，应为 %d）"
                           % (entry["name"], entry["got"], entry["size"]))
        if entry["calc"] != entry["crc"]:
            raise OtaError("%s 校验失败（CRC32 不匹配），更新包可能已损坏"
                           % entry["name"])

    @staticmethod
    def _make_parents(path):
        """确保目录存在（更新包里有 bambu/、umqtt/ 这种子目录）。"""
        if "/" not in path:
            return
        parts = path.split("/")[:-1]
        cur = ""
        for part in parts:
            cur = part if not cur else (cur + "/" + part)
            try:
                os.mkdir(cur)
            except OSError:
                pass          # 已存在就跳过

    # ---------------- 收尾 ----------------
    def finish(self):
        """全部校验通过后把 .new 改名成正式文件；任何失败都会 abort 并抛错。"""
        if self.state != "done":
            self.abort()
            raise OtaError("更新包不完整（只收到 %d 字节）" % self.received)

        # 载荷阶段可能最后一个文件还没 close（feed 里只在 _process 循环里关）
        if self._fh is not None:
            self._close_current()

        renamed = []
        try:
            for entry in self.entries:
                self._replace(entry["tmp"], entry["name"])
                renamed.append(entry["name"])
        except Exception as e:
            # 改名阶段失败：已经改过去的不回滚（回滚反而更危险），
            # 但要清掉还没改的 .new，并如实报错。
            self.abort()
            raise OtaError("更新文件改名失败: %s" % e)

        return renamed

    @staticmethod
    def _replace(tmp, final):
        """把临时文件改名成正式文件（尽量原子）。"""
        try:
            os.rename(tmp, final)
            return
        except OSError:
            pass
        # 某些端口上 rename 不覆盖已存在的目标文件 → 先删再改名
        try:
            os.remove(final)
        except OSError:
            pass
        os.rename(tmp, final)

    def abort(self):
        """清理所有临时文件（失败路径必须调用，绝不留下半截文件）。"""
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        for entry in self.entries:
            try:
                os.remove(entry["tmp"])
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 桌面自测用：造一个更新包
# ---------------------------------------------------------------------------
def build_pack(files):
    """files: [(name, bytes), ...] → 完整的 .ams 包字节串。

    设备侧不用它（设备只解析），但桌面测试和 tools/make_update_pack.py
    都依赖这个格式，放在这里可以保证"造包"和"解包"永远是一份定义。
    """
    manifest = b""
    payload = b""
    for name, data in files:
        raw = name.encode("utf-8")
        manifest += bytes([len(raw)]) + raw
        manifest += bytes([len(data) & 0xFF, (len(data) >> 8) & 0xFF,
                           (len(data) >> 16) & 0xFF, (len(data) >> 24) & 0xFF])
        crc = crc32(data)
        manifest += bytes([crc & 0xFF, (crc >> 8) & 0xFF,
                           (crc >> 16) & 0xFF, (crc >> 24) & 0xFF])
        payload += data

    head = bytearray(MAGIC)
    head += bytes([len(files) & 0xFF, (len(files) >> 8) & 0xFF])
    head += bytes([len(manifest) & 0xFF, (len(manifest) >> 8) & 0xFF])
    total = len(payload)
    head += bytes([total & 0xFF, (total >> 8) & 0xFF,
                   (total >> 16) & 0xFF, (total >> 24) & 0xFF])
    return bytes(head) + manifest + payload
