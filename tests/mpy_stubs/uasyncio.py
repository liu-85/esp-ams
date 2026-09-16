"""
tests/mpy_stubs/uasyncio.py
===========================
`uasyncio` 模块的桌面端桩实现，转发给标准库 asyncio。

MicroPython 的 uasyncio 与标准 asyncio 在常用 API 上基本一致
（sleep / sleep_ms / create_task / gather / run / Lock / Event / wait_for），
这里做一层薄封装即可满足导入与结构检查。
"""

import asyncio as _asyncio

# ---- 直接对照 ----
sleep = _asyncio.sleep
wait_for = _asyncio.wait_for
gather = _asyncio.gather
Lock = _asyncio.Lock
Event = _asyncio.Event
CancelledError = _asyncio.CancelledError
TimeoutError = getattr(_asyncio, "TimeoutError", Exception)
ThreadSafeFlag = _asyncio.Event


def sleep_ms(ms):
    """MicroPython: await asyncio.sleep_ms(200)"""
    return _asyncio.sleep(ms / 1000.0)


def sleep_us(us):
    return _asyncio.sleep(us / 1000000.0)


async def wait_for_ms(awaitable, timeout_ms):
    return await _asyncio.wait_for(awaitable, timeout_ms / 1000.0)


def create_task(coro, name=None):
    return _asyncio.ensure_future(coro)


def run(coro):
    return _asyncio.run(coro)


def get_event_loop():
    return _asyncio.get_event_loop()


async def start_server(*args, **kwargs):
    raise NotImplementedError("桌面桩未实现 start_server")


__all__ = [
    "sleep", "sleep_ms", "sleep_us", "wait_for", "wait_for_ms",
    "gather", "create_task", "run", "get_event_loop",
    "Lock", "Event", "CancelledError", "TimeoutError", "ThreadSafeFlag",
]
