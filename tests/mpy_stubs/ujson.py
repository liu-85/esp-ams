"""
tests/mpy_stubs/ujson.py
========================
`ujson` 模块的桌面端桩实现，直接转发给标准库 json。
"""

from json import load, loads, dump, dumps  # noqa: F401

__all__ = ["load", "loads", "dump", "dumps"]
