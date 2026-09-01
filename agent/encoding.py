"""编码与环境适配：全项目统一使用 UTF-8。
"""
from __future__ import annotations

import locale
import os
import sys
from pathlib import Path
from typing import Optional

UTF8 = "utf-8"


def setup_stdio() -> None:
    """把标准流重配置为 UTF-8（覆盖重定向场景），个别坏字节替换而不崩溃。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)  # pytest 等替身流可能没有
        if reconfigure is not None:
            reconfigure(encoding=UTF8, errors="replace")


def child_env() -> dict:
    """生成子进程环境：在原环境上注入 UTF-8 开关，让 Python 子进程同样用 UTF-8。"""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"             # 子进程整体进入 UTF-8 模式
    env["PYTHONIOENCODING"] = UTF8      # 其标准流按 UTF-8 编解码（含重定向）
    return env


def decode_bytes(data: bytes, fallback: Optional[str] = None) -> str:
    """统一解码入口：优先 UTF-8，失败回退到本地代码页（Windows 中文为 GBK）。

    用于解码外部世界的字节：shell 命令输出、来源不明的文件内容等。
    fallback 可显式指定兜底编码（主要供测试保持平台无关）。

    兜底编码显式优先尝试 GBK/cp936，再退回系统代码页：若只依赖
    locale.getpreferredencoding，在 Python UTF-8 模式（PYTHONUTF8=1 或
    -X utf8）下它会返回 "utf-8"，导致 GBK 字节被 utf-8+replace 解成乱码。
    """
    try:
        return data.decode(UTF8)
    except UnicodeDecodeError:
        pass

    candidates: list[str] = []
    if fallback:
        candidates.append(fallback)
    candidates.extend(["gbk", "cp936"])
    candidates.append(locale.getpreferredencoding(False))

    for enc in candidates:
        if not enc:
            continue
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode(UTF8, errors="replace")


def read_text(path: Path) -> str:
    """以统一编码读取文本文件：UTF-8 优先，本地代码页兜底。"""
    return decode_bytes(Path(path).read_bytes())
