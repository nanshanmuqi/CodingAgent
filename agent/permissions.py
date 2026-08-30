"""安全与权限：工作目录路径防护、shell 命令分级审批。

审批交互通过可注入的 handler 完成，便于 CLI 与测试复用。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

# 工作目录：文件类工具只允许在此目录内读写
WORKSPACE = Path.cwd()

# 极端危险命令：直接拒绝执行
FORBIDDEN_PATTERNS = [
    r"\bformat\b",
    r"\bmkfs\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bdel\b.*\s/[fq]\b",        # del /f /q 强制删除
    r"\brm\b\s+-[a-z]*r[a-z]*f",  # rm -rf 类
    r"\breg\s+delete\b",
    r"\bdiskpart\b",
]

# 危险命令：需要用户确认后才执行
DANGEROUS_PATTERNS = [
    r"\bdel\b",
    r"\berase\b",
    r"\brm\b",
    r"\brmdir\b",
    r"\brd\b",
    r"\bremove-item\b",
    r"\bri\b",
    r"\bmove\b",
    r"\bren\b",
    r"\brename\b",
    r"\btakeown\b",
    r"\bicacls\b",
    r"\battrib\b",
    r">\s*[^\s]",  # 重定向覆盖写文件
]

SAFE = "safe"
DANGEROUS = "dangerous"
FORBIDDEN = "forbidden"


def resolve_in_workspace(path: str) -> Path:
    """将相对路径解析到工作目录内；越界（../ 逃逸）则抛 PermissionError。"""
    resolved = (WORKSPACE / path).resolve()
    if resolved != WORKSPACE and WORKSPACE not in resolved.parents:
        raise PermissionError(f"路径越出工作目录，已拒绝：{path}")
    return resolved


def classify_command(command: str) -> str:
    """对 shell 命令分级：safe / dangerous / forbidden。"""
    lowered = command.lower()
    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, lowered):
            return FORBIDDEN
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, lowered):
            return DANGEROUS
    return SAFE


# --- 审批 handler：CLI 注入，默认为终端 y/n 提问 ---

ApprovalHandler = Callable[[str], bool]


def _default_approval_handler(command: str) -> bool:
    answer = input(f"该命令具有风险，确认执行？\n  {command}\n[y/N] ").strip().lower()
    return answer in ("y", "yes")


_handler: Optional[ApprovalHandler] = None


def set_approval_handler(handler: Optional[ApprovalHandler]) -> None:
    global _handler
    _handler = handler


def ask_approval(command: str) -> bool:
    handler = _handler or _default_approval_handler
    try:
        return bool(handler(command))
    except (EOFError, KeyboardInterrupt):
        return False
