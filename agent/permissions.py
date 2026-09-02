"""安全与权限：工作目录路径防护、shell 命令分级审批。

审批交互通过可注入的 handler 完成，便于 CLI 与测试复用。
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Callable, Optional

# 工作目录：文件类工具只允许在此目录内读写
WORKSPACE = Path.cwd()

# 生成文件的统一输出目录（工作目录下的 out/）
OUTPUT_DIR_NAME = "out"

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
    r"(?<!-)>\s*[^\s]",  # 重定向覆盖写文件（(?<!-) 排除箭头 -> 等文本误判）
]

# 间接脚本执行：命令文本本身不含危险关键字，但会通过解释器/脚本宿主运行任意
# 脚本或内联代码，实际行为对分级器不可见，可能绕过上面的危险命令识别
# （例如先写一个删除脚本，再 python/脚本宿主运行它）。这类命令同样需用户确认。
SCRIPT_EXECUTION_PATTERNS = [
    # Python：运行 .py 脚本或 -c 内联代码（-m 模块调用如 pytest 不在此列）
    r"\bpy(?:thon)?3?(?:\.exe)?\s+\S+\.py\b",
    r"\bpy(?:thon)?3?(?:\.exe)?\s+-c\b",
    # Node.js：运行 .js/.mjs/.cjs 脚本或 -e 内联代码
    r"\bnode(?:\.exe)?\s+\S+\.(?:js|mjs|cjs)\b",
    r"\bnode(?:\.exe)?\s+-e\b",
    # PowerShell / pwsh：完整 shell + .NET，可执行任意代码，一律需确认
    r"\b(?:powershell|pwsh)(?:\.exe)?\b",
    # 批处理脚本：直接运行 .bat/.cmd
    r"(?:^|[\s;&|])\S+\.(?:bat|cmd)\b",
    # Windows 脚本宿主：运行 VBS/JS 脚本
    r"\b(?:wscript|cscript)(?:\.exe)?\b",
    # bash / sh：运行 .sh 脚本或 -c 内联代码
    r"\b(?:bash|sh)\s+\S+\.sh\b",
    r"\b(?:bash|sh)\s+-c\b",
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


def resolve_output(path: str) -> Path:
    """解析生成文件的写入路径：未带 out/ 前缀时自动落到 out/ 目录下。"""
    target = resolve_in_workspace(path)
    out_dir = resolve_in_workspace(OUTPUT_DIR_NAME)
    if target != out_dir and out_dir not in target.parents:
        target = (out_dir / path).resolve()
    return target


def resolve_existing(path: str) -> Path:
    """读取/编辑用：优先按原路径解析，不存在时回退到 out/ 目录下查找。"""
    target = resolve_in_workspace(path)
    if not target.exists():
        fallback = resolve_output(path)
        if fallback.exists():
            return fallback
    return target


def classify_command(command: str) -> str:
    """对 shell 命令分级：safe / dangerous / forbidden。"""
    lowered = command.lower()
    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, lowered):
            return FORBIDDEN
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, lowered):
            return DANGEROUS
    for pattern in SCRIPT_EXECUTION_PATTERNS:
        if re.search(pattern, lowered):
            return DANGEROUS
    return SAFE


# --- 审批 handler：CLI 注入，默认为终端 y/n 提问 ---

ApprovalHandler = Callable[[str], bool]


def _default_approval_handler(command: str) -> bool:
    answer = input(f"该命令具有风险，确认执行？\n  {command}\n[y/N] ").strip().lower()
    return answer in ("y", "yes")


_handler: Optional[ApprovalHandler] = None

# 审批交互必须串行：并发执行工具时，多个危险命令若同时请求确认，
# 终端提示会交错、输入也易错乱；用一把锁保证一次只处理一个审批。
_approval_lock = threading.Lock()


def set_approval_handler(handler: Optional[ApprovalHandler]) -> None:
    global _handler
    _handler = handler


def ask_approval(command: str) -> bool:
    handler = _handler or _default_approval_handler
    with _approval_lock:
        try:
            return bool(handler(command))
        except (EOFError, KeyboardInterrupt):
            return False
