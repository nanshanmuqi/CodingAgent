"""工具注册表：汇总所有工具的 schema，并负责解析、分发模型发起的 tool_call。"""
from __future__ import annotations

import json

from .base import Tool, ToolResult
from .file_tools import edit_file_tool, read_file_tool, write_file_tool
from .search import glob_tool, grep_tool
from .shell import run_command_tool

TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in (
        read_file_tool,
        write_file_tool,
        edit_file_tool,
        run_command_tool,
        grep_tool,
        glob_tool,
    )
}


def tool_schemas() -> list[dict]:
    """生成 OpenAI tool calling 所需的 tools 参数。"""
    return [tool.to_openai_schema() for tool in TOOLS.values()]


def execute_tool_call(tool_call: dict) -> ToolResult:
    """解析并执行一个模型返回的 tool_call，任何异常都转为结构化错误。"""
    name = tool_call.get("function", {}).get("name", "")
    raw_arguments = tool_call.get("function", {}).get("arguments", "") or "{}"

    tool = TOOLS.get(name)
    if tool is None:
        return ToolResult(ok=False, error=f"未知工具：{name}，可用工具：{', '.join(TOOLS)}")

    try:
        arguments = json.loads(raw_arguments)
        if not isinstance(arguments, dict):
            raise ValueError("arguments 必须是 JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        return ToolResult(ok=False, error=f"工具参数 JSON 解析失败：{e}，请修正后重新发起调用")

    return tool.run(arguments)
