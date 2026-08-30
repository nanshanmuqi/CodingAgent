"""文件工具：read_file / write_file / edit_file。

所有路径经 permissions.resolve_in_workspace 校验，限制在工作目录内。
"""
from __future__ import annotations

from ..permissions import resolve_existing, resolve_in_workspace, resolve_output
from .base import Tool, ToolResult

# 单次读取的最大行数与单行最大长度，避免超大文件撑爆上下文
MAX_READ_LINES = 400
MAX_LINE_LENGTH = 2000


def _read_file(path: str, offset: int = 1, limit: int = MAX_READ_LINES) -> ToolResult:
    target = resolve_existing(path)
    if not target.is_file():
        return ToolResult(ok=False, error=f"文件不存在：{path}")

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    start = max(offset - 1, 0)
    end = min(start + limit, total)
    if start >= total:
        return ToolResult(ok=False, error=f"起始行 {offset} 超出文件范围（共 {total} 行）")

    body = "\n".join(
        f"{i}\t{lines[i][:MAX_LINE_LENGTH]}" for i in range(start, end)
    )
    notice = ""
    if end < total:
        notice = f"\n\n[已截断：仅显示第 {start + 1}-{end} 行，文件共 {total} 行]"
    return ToolResult(ok=True, output=f"文件 {path}（共 {total} 行）：\n{body}{notice}")


def _write_file(path: str, content: str) -> ToolResult:
    target = resolve_output(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    actual = target.relative_to(resolve_in_workspace("."))
    return ToolResult(ok=True, output=f"已写入 {actual}（{len(content)} 字符）")


def _edit_file(path: str, old_str: str, new_str: str, replace_all: bool = False) -> ToolResult:
    target = resolve_existing(path)
    if not target.is_file():
        return ToolResult(ok=False, error=f"文件不存在：{path}")

    text = target.read_text(encoding="utf-8", errors="replace")
    count = text.count(old_str)
    if count == 0:
        return ToolResult(ok=False, error="未找到匹配的 old_str，请先用 read_file 核对原文后重试")
    if count > 1 and not replace_all:
        return ToolResult(
            ok=False,
            error=f"old_str 出现 {count} 处，不唯一。请补充更多上下文使其唯一，或设 replace_all=true",
        )

    new_text = text.replace(old_str, new_str) if replace_all else text.replace(old_str, new_str, 1)
    target.write_text(new_text, encoding="utf-8")
    return ToolResult(ok=True, output=f"已编辑 {path}（替换 {count if replace_all else 1} 处）")


read_file_tool = Tool(
    name="read_file",
    description="读取工作目录内文件的内容，返回带行号的文本。大文件请分段读取。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作目录的文件路径"},
            "offset": {"type": "integer", "description": "起始行号，从 1 开始，默认 1"},
            "limit": {"type": "integer", "description": f"读取行数，默认 {MAX_READ_LINES}"},
        },
        "required": ["path"],
    },
    handler=_read_file,
)

write_file_tool = Tool(
    name="write_file",
    description="创建或覆写文件，自动创建父目录。新生成的文件统一写入 out/ 目录（路径未带 out/ 前缀时会自动补全）。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作目录的文件路径"},
            "content": {"type": "string", "description": "要写入的完整内容"},
        },
        "required": ["path", "content"],
    },
    handler=_write_file,
)

edit_file_tool = Tool(
    name="edit_file",
    description="对文件做搜索替换式局部编辑：将 old_str 替换为 new_str。old_str 必须唯一匹配。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作目录的文件路径"},
            "old_str": {"type": "string", "description": "要被替换的原文（须与文件内容完全一致）"},
            "new_str": {"type": "string", "description": "替换后的新内容"},
            "replace_all": {"type": "boolean", "description": "是否替换全部匹配，默认 false"},
        },
        "required": ["path", "old_str", "new_str"],
    },
    handler=_edit_file,
)
