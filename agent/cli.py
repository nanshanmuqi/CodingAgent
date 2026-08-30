"""CLI 入口：REPL 循环、流式输出、工作状态显示、斜杠命令、危险命令审批交互。

界面基于 rich 的行内渲染：不接管终端屏幕、保留原有滚动历史，
避免全屏应用带来的渲染残留与布局跳动。所有输出经 escape/markup=False
处理，模型文本中的方括号不会被误解析为样式标记。

启动方式：
  python -m agent.cli                在当前终端交互
  python -m agent.cli --new-window   唤起一个独立的新终端窗口进行交互
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.status import Status

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

# 工作状态 spinner：完全由主循环的真实回调驱动（等待模型 / 流式输出 / 执行工具），不凭空显示
_status: Optional[Status] = None
_answer_started: bool = False  # 本轮是否已打印 "Agent >" 前缀（spinner 会清行，须惰性打印）


def _status_start(text: str) -> None:
    global _status
    _status_stop()
    _status = console.status(text, spinner="dots")
    _status.start()


def _status_stop() -> None:
    global _status
    if _status is not None:
        _status.stop()
        _status = None


def _on_status(event: str, round_no: int) -> None:
    # 每次请求模型前主循环触发：模型尚未返回任何内容，显示"思考中"
    if event == "waiting_model":
        _status_start(f"[bold magenta]正在思考（第 {round_no} 轮），等待模型响应…[/bold magenta]")


def _on_tool_call(name: str, tool_call: dict) -> None:
    _status_stop()  # 模型已返回（工具调用），思考结束
    # 工具参数来自模型输出，必须 escape 防止 [ ] 被当作 rich 样式标记
    console.print(f"  [cyan]▶ {escape(format_tool_call_brief(tool_call))}[/cyan]")
    _status_start(f"[cyan]正在执行工具 {escape(name)}…[/cyan]")


def _on_tool_result(name: str, ok: bool) -> None:
    _status_stop()  # 工具执行结束
    if ok:
        console.print(f"  [green]✓ {escape(name)}[/green]")
    else:
        console.print(f"  [red]✗ {escape(name)} 执行失败[/red]")


def _on_text(text: str) -> None:
    global _answer_started
    _status_stop()  # 首个流式 token 到达，模型开始输出，状态转为可见的文本流
    if not _answer_started:
        # spinner 会清除当前行，"Agent >" 前缀须在流式输出开始前惰性打印
        console.print("\n[bold magenta]Agent > [/bold magenta]", end="")
        _answer_started = True
    # markup=False：模型文本按纯文本输出，方括号不产生渲染残留
    console.print(text, end="", markup=False, highlight=False)


def _make_approval_handler():
    def handler(command: str) -> bool:
        console.print(f"\n[bold yellow]⚠ 危险命令，需要确认：[/bold yellow] {escape(command)}")
        answer = console.input("确认执行？[y/N] ").strip().lower()
        return answer in ("y", "yes")

    return handler


def _relaunch_in_new_window() -> None:
    """以独立控制台窗口重新启动本 CLI（Windows 的 CREATE_NEW_CONSOLE）。"""
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)  # 非 Windows 平台退化为普通子进程
    subprocess.Popen(
        [sys.executable, "-m", "agent.cli"],
        cwd=os.getcwd(),  # 继承当前工作目录，新窗口内 agent 的工作目录不变
        creationflags=creationflags,
    )


def main() -> None:
    global _answer_started
    if "--new-window" in sys.argv:
        _relaunch_in_new_window()
        return
    config = load_config()
    client = LLMClient(config)
    history = MessageHistory(max_context_tokens=config.max_context_tokens,
                             model_name=config.model_name)
    set_approval_handler(_make_approval_handler())

    console.print(Panel.fit(
        f"模型：[bold]{escape(config.model_name)}[/bold]\n"
        f"工作目录：{escape(os.getcwd())}\n"
        f"生成文件输出目录：out/\n"
        "输入 /help 查看命令",
        title="[bold green]Coding Agent[/bold green]",
        border_style="green",
    ))

    # 流式文本在每个 agent.run 内通过回调实时打印
    agent = AgentLoop(
        config,
        client,
        history,
        on_text=_on_text,
        on_tool_call=_on_tool_call,
        on_tool_result=_on_tool_result,
        on_status=_on_status,
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
                console.print(f"[yellow]未知命令：{escape(user_input)}[/yellow]，输入 /help 查看帮助")
            continue

        _answer_started = False
        try:
            answer = agent.run(user_input)
            console.print()  # 流式输出后的收尾换行
            # 正常回答已由流式回调实时打印；终止/错误提示（[...] 开头）未曾打印，在此补打
            if answer.startswith("["):
                console.print(f"[yellow]{escape(answer)}[/yellow]")
        except KeyboardInterrupt:
            # 终止条件 4：用户中断当前任务，回到输入态
            console.print("\n[yellow]已中断当前任务。[/yellow]")
        except Exception as e:
            console.print(f"\n[red]发生未预期错误：{type(e).__name__}: {escape(str(e))}[/red]")
        finally:
            _status_stop()  # 任何退出路径都确保 spinner 不残留


if __name__ == "__main__":
    main()
