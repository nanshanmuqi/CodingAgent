"""工具抽象基类与统一执行结果类型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolResult:
    """工具执行的统一结果。失败时 error 非空，供回填模型自我修正。

    summary 为面向 CLI 的一行摘要（过程弱化展示用），与回填模型的完整 output 解耦。
    """
    ok: bool
    output: str = ""
    error: str = ""
    summary: str = ""
    diff: str = ""

    def to_message_content(self) -> str:
        if self.ok:
            return self.output or "(执行成功，无输出)"
        return f"工具执行失败：{self.error}"


@dataclass
class Tool:
    """一个工具 = JSON Schema 定义 + 本地执行函数。"""
    name: str
    description: str
    parameters: dict  # JSON Schema (object)
    handler: Callable[..., ToolResult]
    # handler 不接受的额外注入参数名（如 timeout 由 shell 工具自己处理）
    extra: dict = field(default_factory=dict)

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            return self.handler(**arguments)
        except TypeError as e:
            return ToolResult(ok=False, error=f"参数错误：{e}")
        except PermissionError as e:
            return ToolResult(ok=False, error=str(e))
        except Exception as e:  # 工具内任何异常都不应使进程崩溃
            return ToolResult(ok=False, error=f"{type(e).__name__}: {e}")
