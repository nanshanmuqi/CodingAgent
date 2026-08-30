"""shell 工具：run_command。

安全设计：
- 命令经 permissions 分级：safe 直接执行 / dangerous 需用户确认 / forbidden 直接拒绝
- 超时强制终止进程
- stdout/stderr 合并捕获，超长截断
"""
from __future__ import annotations

import subprocess

from ..permissions import DANGEROUS, FORBIDDEN, ask_approval, classify_command
from .base import Tool, ToolResult

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300
MAX_OUTPUT_CHARS = 20_000


def _run_command(command: str, timeout: int = DEFAULT_TIMEOUT) -> ToolResult:
    level = classify_command(command)
    if level == FORBIDDEN:
        return ToolResult(ok=False, error=f"命令命中禁用规则，已拒绝执行：{command}")
    if level == DANGEROUS and not ask_approval(command):
        return ToolResult(ok=False, error="用户拒绝了该命令的执行")

    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        return ToolResult(ok=False, error=f"命令启动失败：{e}")

    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return ToolResult(ok=False, error=f"命令执行超过 {timeout} 秒，已强制终止")

    output = output or ""
    truncated = len(output) > MAX_OUTPUT_CHARS
    if truncated:
        output = output[:MAX_OUTPUT_CHARS]

    parts = []
    if output.strip():
        parts.append(output.rstrip())
    if truncated:
        parts.append(f"[输出过长已截断，仅保留前 {MAX_OUTPUT_CHARS} 字符]")
    parts.append(f"[退出码 {proc.returncode}]")

    # 非零退出码视为失败，把输出一并返回供模型诊断
    return ToolResult(
        ok=proc.returncode == 0,
        output="\n".join(parts),
        error="" if proc.returncode == 0 else "\n".join(parts),
    )


run_command_tool = Tool(
    name="run_command",
    description=(
        "在 Windows shell 中执行命令并返回输出与退出码。"
        "用于运行脚本、安装依赖、执行测试等。危险命令会被拒绝或要求用户确认。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的完整命令"},
            "timeout": {"type": "integer", "description": f"超时秒数，默认 {DEFAULT_TIMEOUT}，上限 {MAX_TIMEOUT}"},
        },
        "required": ["command"],
    },
    handler=_run_command,
)
