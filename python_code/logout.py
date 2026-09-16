"""
logout.py —— 统一日志出口
==========================

除了照原样打印到串口，还会把最近若干行留在内存里，供网页右侧的
「设备日志」面板读取 —— 这样调试时不用一直插着 USB 看串口。

⚠️ 内存是有代价的：每一行最多 LOG_LINE_MAX 个字符，保留 LOG_MAX_LINES 行。
   当前设置最坏情况约占 24 × 100 ≈ 2.4 KB。板子空闲堆紧张时（页面都发不
   出去的那种），可以把这两个值调小；反过来想看得更多就调大，但别指望
   它能当文件系统用 —— 真正的历史日志请走串口。
"""

try:
    import time

    def _stamp():
        """给每一行加个 分:秒.毫秒 的时间戳（ticks_ms 从开机算起）"""
        try:
            ms = time.ticks_ms()
            total_s = ms // 1000
            return "%02d:%02d.%03d" % ((total_s // 60) % 100, total_s % 60, ms % 1000)
        except Exception:
            return "--:--.---"
except ImportError:                      # 理论上不会发生
    def _stamp():
        return "--:--.---"


# ---------------------------------------------------------------------------
# 环形缓冲
# ---------------------------------------------------------------------------
LOG_MAX_LINES = 24      # 保留多少行
LOG_LINE_MAX = 100      # 每行最多多少字符（超长截断，防止一条日志吃掉整块内存）

_log_lines = []         # 头部最旧、尾部最新


def logout(info, is_print=True, is_save=False, is_error=False, **kwargs):
    """写一条日志。

    ★ 参数顺序**必须**保持和上游一致：(info, is_print, is_save, is_error)。
      上游有个调用写成了 `logout("发布出错了", e)`，把异常当第二个位置参数传 ——
      只要顺序不变，它就跟以前一样能打印出来。
    """
    text = info if isinstance(info, str) else str(info)
    if len(text) > LOG_LINE_MAX:
        text = text[:LOG_LINE_MAX] + "…"

    line = "%s %s%s" % (_stamp(), "★ " if is_error else "", text)
    try:
        _log_lines.append(line)
        if len(_log_lines) > LOG_MAX_LINES:
            del _log_lines[0]
    except MemoryError:
        # 日志撑不下就先丢掉旧的，绝不能因为记日志把主流程搞挂
        try:
            del _log_lines[:len(_log_lines) // 2]
        except Exception:
            pass
    except Exception:
        pass

    if is_print:
        try:
            print(info, **kwargs)
        except Exception:
            pass


def recent(count=None):
    """取最近的日志行（旧的在前，新的在后），返回一个新列表。

    ★ 返回副本：网页那边序列化的时候不能拿到正在被 append 的同一个列表。
    """
    if count is None or count <= 0 or count >= len(_log_lines):
        return list(_log_lines)
    return list(_log_lines[-count:])


def count():
    """当前缓冲里有多少行"""
    return len(_log_lines)


def clear():
    """清空缓冲（只是内存里的，串口历史不受影响）"""
    del _log_lines[:]
    return 0
