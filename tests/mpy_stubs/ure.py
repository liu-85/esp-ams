"""
tests/mpy_stubs/ure.py
======================
`ure` 模块的桌面端桩实现，直接转发给标准库 re。
"""

import re  # noqa: F401

search = re.search
match = re.match
fullmatch = re.fullmatch
compile = re.compile
sub = re.sub
subn = re.subn
split = re.split
findall = re.findall
finditer = re.finditer
escape = re.escape

DEBUG = re.DEBUG
IGNORECASE = re.IGNORECASE
MULTILINE = re.MULTILINE
DOTALL = re.DOTALL
VERBOSE = re.VERBOSE

__all__ = [
    "search", "match", "fullmatch", "compile", "sub", "subn",
    "split", "findall", "finditer", "escape",
    "DEBUG", "IGNORECASE", "MULTILINE", "DOTALL", "VERBOSE",
]
