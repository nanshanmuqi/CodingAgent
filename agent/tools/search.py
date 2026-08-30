"""搜索工具：grep / glob，纯 Python 实现，跨平台，不依赖系统命令。"""
from __future__ import annotations

import fnmatch
import os
import re

from ..permissions import resolve_in_workspace
from .base import Tool, ToolResult

MAX_GREP_RESULTS = 50
MAX_GLOB_RESULTS = 200
# 跳过这些目录，避免无意义扫描
IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache"}


def _grep(pattern: str, path: str = ".", include: str = "", max_results: int = MAX_GREP_RESULTS) -> ToolResult:
    base = resolve_in_workspace(path)
    if not base.exists():
        return ToolResult(ok=False, error=f"路径不存在：{path}")
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return ToolResult(ok=False, error=f"正则表达式无效：{e}")

    matches: list[str] = []
    truncated = False

    def scan_file(file_path) -> None:
        nonlocal truncated
        try:
            with open(file_path, encoding="utf-8", errors="ignore") as f:
                for lineno, line in enumerate(f, 1):
                    if regex.search(line):
                        rel = os.path.relpath(file_path, resolve_in_workspace("."))
                        matches.append(f"{rel}:{lineno}: {line.rstrip()[:200]}")
                        if len(matches) >= max_results:
                            truncated = True
                            raise StopIteration
        except (OSError, UnicodeError):
            pass

    try:
        if base.is_file():
            scan_file(base)
        else:
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
                for name in files:
                    if include and not fnmatch.fnmatch(name, include):
                        continue
                    scan_file(os.path.join(root, name))
    except StopIteration:
        pass

    if not matches:
        return ToolResult(ok=True, output="未找到匹配项")
    output = "\n".join(matches)
    if truncated:
        output += f"\n[结果过多已截断，仅显示前 {max_results} 条]"
    return ToolResult(ok=True, output=output)


def _glob(pattern: str, path: str = ".") -> ToolResult:
    base = resolve_in_workspace(path)
    if not base.is_dir():
        return ToolResult(ok=False, error=f"目录不存在：{path}")

    results: list[str] = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for name in dirs + files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, base)
            # 同时匹配相对路径与纯文件名，兼容 "**/*.py" 与 "*.py" 两种习惯
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel.replace(os.sep, "/"), pattern) \
                    or ("**" not in pattern and fnmatch.fnmatch(name, pattern)):
                results.append(rel)
                if len(results) >= MAX_GLOB_RESULTS:
                    break

    if not results:
        return ToolResult(ok=True, output="未找到匹配的文件")
    return ToolResult(ok=True, output="\n".join(sorted(results)))


grep_tool = Tool(
    name="grep",
    description="在工作目录内按正则搜索文件内容，返回 文件:行号: 内容 的匹配列表。",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "搜索起点目录或文件，默认当前目录"},
            "include": {"type": "string", "description": "文件名过滤，如 *.py，默认全部文件"},
            "max_results": {"type": "integer", "description": f"结果条数上限，默认 {MAX_GREP_RESULTS}"},
        },
        "required": ["pattern"],
    },
    handler=_grep,
)

glob_tool = Tool(
    name="glob",
    description="按文件名模式在工作目录内查找文件，如 **/*.py、src/*.txt。",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "文件名模式，支持 * 与 **"},
            "path": {"type": "string", "description": "搜索起点目录，默认当前目录"},
        },
        "required": ["pattern"],
    },
    handler=_glob,
)
