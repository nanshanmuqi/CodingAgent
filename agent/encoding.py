"""编码与环境适配：全项目统一使用 UTF-8。

Windows 中文环境的乱码根源与本模块的对策：

1. 终端重定向后按系统代码页（GBK）编码
   → setup_stdio() 把 stdin/stdout/stderr 重配置为 UTF-8
2. 新窗口 / 子进程中的 Python 仍按 GBK 工作
   → child_env() 注入 PYTHONUTF8 / PYTHONIOENCODING
3. 外部字节来源编码不一（cmd 输出是 OEM 代码页、老文件可能是 GBK）
   → decode_bytes() 先按 UTF-8 解码，失败回退系统代码页
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
    """统一解码入口：优先 UTF-8，失败回退系统代码页（Windows 中文环境为 GBK）。

    用于解码外部世界的字节：shell 命令输出、来源不明的文件内容等。
    fallback 可显式指定兜底编码（主要供测试保持平台无关）。
    """
    try:
        return data.decode(UTF8)
    except UnicodeDecodeError:
        return data.decode(fallback or locale.getpreferredencoding(False), errors="replace")


def read_text(path: Path) -> str:
    """以统一编码读取文本文件：UTF-8 优先，本地代码页兜底。"""
    return decode_bytes(Path(path).read_bytes())
