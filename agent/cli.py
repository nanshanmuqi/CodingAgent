"""CLI 入口：REPL 循环、流式输出、斜杠命令、危险命令审批交互。

启动方式：python -m agent.cli
"""
from __future__ import annotations

import os

from rich.console import Console

from .client import LLMClient, format_tool_call_brief
from .config import load_config
from .context import MessageHistory
from .loop import AgentLoop
from .permissions import set_approval_handler

console = Console()

HELP_TEXT = """\
可用命令：
  /help    显示本帮助
  /reset   清空对话上下文
  /tokens  查看累计 token 用量
  /quit    退出
其他输入将作为编程任务交给 agent 自主完成。
"""


def _make_approval_handler():
    def handler(command: str) -> bool:
        console.print(f"\n[bold yellow]⚠ 危险命令，需要确认：[/bold yellow] {command}")
        answer = console.input("确认执行？[y/N] ").strip().lower()
        return answer in ("y", "yes")

    return handler


def _on_tool_call(name: str, tool_call: dict) -> None:
    console.print(f"\n[cyan]> {format_tool_call_brief(tool_call)}[/cyan]")


def _on_tool_result(name: str, ok: bool) -> None:
    if not ok:
        console.print(f"[red]  {name} 执行失败[/red]")


def _on_text(text: str) -> None:
    console.print(text, end="", highlight=False)


def main() -> None:
    config = load_config()
    client = LLMClient(config)
    history = MessageHistory(max_context_tokens=config.max_context_tokens)
    set_approval_handler(_make_approval_handler())

    console.print(
        f"[bold green]Coding Agent[/bold green] 已启动"
        f"（模型：{config.model_name}，工作目录：{os.getcwd()}）"
    )
    console.print("输入 /help 查看命令。\n")

    # 流式文本在每个 agent.run 内通过回调实时打印
    agent = AgentLoop(
        config,
        client,
        history,
        on_text=_on_text,
        on_tool_call=_on_tool_call,
        on_tool_result=_on_tool_result,
    )

    while True:
        try:
            user_input = console.input("\n[bold blue]User > [/bold blue]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n已退出。")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            if user_input == "/quit":
                break
            elif user_input == "/help":
                console.print(HELP_TEXT)
            elif user_input == "/reset":
                history.reset()
                console.print("[green]上下文已清空。[/green]")
            elif user_input == "/tokens":
                console.print(
                    f"累计 token 用量：{agent.total_tokens_used} / 预算 {config.token_budget}；"
                    f"当前上下文约 {history.total_tokens()} tokens"
                )
            else:
                console.print(f"[yellow]未知命令：{user_input}[/yellow]，输入 /help 查看帮助")
            continue

        console.print("\n[bold magenta]Agent > [/bold magenta]", end="")
        try:
            answer = agent.run(user_input)
            console.print()  # 流式输出后的收尾换行
            # 正常回答已由流式回调实时打印；终止/错误提示（[...] 开头）未曾打印，在此补打
            if answer.startswith("["):
                console.print(f"[yellow]{answer}[/yellow]")
        except KeyboardInterrupt:
            # 终止条件 4：用户中断当前任务，回到输入态
            console.print("\n[yellow]已中断当前任务。[/yellow]")
        except Exception as e:
            console.print(f"\n[red]发生未预期错误：{type(e).__name__}: {e}[/red]")


if __name__ == "__main__":
    main()
