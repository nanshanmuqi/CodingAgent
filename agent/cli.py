"""CLI 入口：REPL 循环、三明治布局输出、工作状态显示、斜杠命令、危险命令审批交互。

启动方式：
  python -m agent.cli                在当前终端交互
  python -m agent.cli --new-window   唤起一个独立的新终端窗口进行交互
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.status import Status
from rich.text import Text

from .client import LLMClient
from .config import load_config
from .context import MessageHistory
from .encoding import child_env, setup_stdio
from .loop import AgentLoop
from .permissions import set_approval_handler
from .tools.base import ToolResult
from .trace import TraceLogger

console = Console()

HELP_TEXT = """\
/help     显示本帮助
/verbose  切换完整过程输出（默认只显示折叠摘要）
/reset    清空对话上下文
/tokens   查看累计 token 用量
/quit     退出
Ctrl+C    中断当前任务"""

# ---- 元信息标签（英文；过程用 √/×、建议用 "Next steps:"，标签只在区首出现一次，避免重复） ----
TAG_INFO = "[INFO]"     # 帮助/用量/重置/退出/终止等系统提示
TAG_WARN = "[WARN]"     # 危险命令审批
TAG_ERROR = "[ERROR]"   # 未预期异常与 API 错误

# ---- 颜色语义：结构标记与建议区分开，避免 dark_cyan 撞色；结构提亮增强对比度 ----
C_STRUCT = "bold cyan"   # 结构标记：Stage 分隔、User > 提示
C_SUGGEST = "yellow"     # 建议区条目：黄色在深色终端上高对比度，易读
C_AGENT = "bright_magenta"  # Agent 回复前缀与面板边框：暗色终端下醒目，且不与 cyan/green/yellow/red 撞色

MAX_TITLE_LEN = 40    # 阶段标题（模型该轮一句话说明）最大长度
MAX_ERROR_LEN = 120   # 失败行附带的错误摘要最大长度

# 模型自带、由本程序统一渲染的建议小标题（结论区渲染时跳过，避免与建议区重复）
_SUGGESTION_HEADERS = {"下一步建议", "后续建议"}


def _print_tagged(tag: str, text: str, style: str = "") -> None:
    """逐行打印元信息并加全角标签；空行原样输出（不加标签），仅作视觉分隔。
    markup=False 保证内容中的 [ ] 安全。"""
    for line in text.splitlines():
        if line:
            console.print(f"{tag} {line}", style=style, markup=False, highlight=False)
        else:
            console.print()


# 工作状态 spinner：完全由主循环的真实回调驱动（等待模型 / 流式输出 / 执行工具），不凭空显示
_status: Optional[Status] = None
_round_no: int = 0            # 当前轮次（on_status 每轮更新）
_round_text: str = ""         # 当前轮模型文本缓冲：工具轮用作阶段标题，最终轮即尾部结论
_stage_printed: bool = False  # 当前轮的阶段分隔是否已打印（有工具调用的轮才打印）
_verbose: bool = False               # /verbose 开关：True 时输出完整工具流水
_tool_counts: dict[str, int] = {}    # 本轮各工具调用次数（折叠摘要用）
_tool_failures: int = 0              # 本轮工具失败次数


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


def _print_stage_header() -> None:
    """阶段分隔：══ Stage N：名称 ══；名称取模型该轮的一句话说明，无则退化。"""
    if _round_no > 1:
        console.print()  # 阶段之间空行，增大间距
    title = next((l.strip() for l in _round_text.splitlines() if l.strip()), "")
    title = title[:MAX_TITLE_LEN] or "工具执行"
    console.print(f"══ Stage {_round_no}：{title} ══", style=C_STRUCT, markup=False, highlight=False)


def _print_tail(answer: str) -> None:
    """尾部：结论与建议合并渲染于一个线框面板中，"Agent >" 作为面板首行。

    面板内上下分层：首行 "Agent >" → 结论正文（Markdown）→ 分隔线 →
    "Next steps:" 建议区（用 ASCII "-" 作项目符号，避免中文终端框线错位）。
    模型可能把建议写成 "- [PROMPT] …" 等列表形式，按"包含 [PROMPT]"提取；
    模型自带的小标题（如"下一步建议："）跳过，由本函数统一渲染建议区。
    """
    body_lines, prompts = [], []
    for line in answer.splitlines():
        stripped = line.strip()
        if "[PROMPT]" in stripped:
            prompts.append(stripped.split("[PROMPT]", 1)[1].strip("：: "))
        elif stripped.rstrip("：:").strip() in _SUGGESTION_HEADERS:
            continue
        else:
            body_lines.append(line)

    # 折叠连续空行，避免模型输出过多空行把面板撑得过高
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(body_lines).strip())

    parts: list = [Text("Agent >", style=f"bold {C_AGENT}")]
    if body:
        parts.append(Markdown(body))
    if prompts:
        parts.append(Rule(style="dim"))  # 结论与建议之间的分隔线，划分更明确
        parts.append(Text("Next steps:", style="bold yellow"))
        for text in prompts:
            # 用 ASCII "-" 做项目符号：East Asian Ambiguous 的 "•" 在中文终端易被渲染成 2 列，导致框线错位
            parts.append(Text(f"  - {text}", style=C_SUGGEST))

    console.print(Panel(Group(*parts), border_style=C_AGENT, expand=False))


def _on_status(event: str, round_no: int) -> None:
    global _round_no, _round_text, _stage_printed, _tool_counts, _tool_failures
    # 每次请求模型前主循环触发：新一轮开始，重置本轮文本缓冲与阶段标记
    if event == "waiting_model":
        if round_no == 1:  # 每个任务第一轮重置折叠摘要统计
            _tool_counts, _tool_failures = {}, 0
        _round_no, _round_text, _stage_printed = round_no, "", False
        _status_start(f"[dark_cyan]思考中（第 {round_no} 轮），等待模型响应…[/dark_cyan]")


def _on_text(text: str) -> None:
    global _round_text
    if not _round_text:
        # 首个流式 token 到达：切换 spinner 文案（_status_start 内部会先停旧状态）
        _status_start("[dark_cyan]模型输出中…[/dark_cyan]")
    _round_text += text  # 只缓冲不打印：工具轮文本将成为阶段标题，最终轮文本由尾部面板统一展示
    if _status is not None:
        # 长答案生成期间用 spinner 文案给出进度，避免用户干等
        _status.update(status=f"[dark_cyan]模型输出中…（已生成 {len(_round_text)} 字符）[/dark_cyan]")


def _on_tool_call(name: str, tool_call: dict) -> None:
    global _stage_printed
    _status_stop()  # 模型已返回（工具调用），思考结束
    _tool_counts[name] = _tool_counts.get(name, 0) + 1
    if _verbose and not _stage_printed:
        # 此刻该轮文本已完整缓冲（流式响应先于工具执行结束），可取出阶段名称
        _print_stage_header()
        _stage_printed = True
    _status_start(f"[cyan]执行 {escape(name)}…[/cyan]")


def _on_tool_result(name: str, result: ToolResult) -> None:
    global _tool_failures
    _status_stop()  # 工具执行结束
    if not result.ok:
        _tool_failures += 1
    if not _verbose:
        return  # 非 verbose：过程折叠，只累积摘要统计，不落永久行
    if result.ok:
        # √/× 均在 GBK 字符集内：GBK 终端下也不会编码崩溃（✓/✗ 不在 GBK 内，勿用）
        summary = result.summary or name
        console.print(f"  √ {summary}", style="green", markup=False, highlight=False)
    else:
        brief = result.error.splitlines()[0][:MAX_ERROR_LEN] if result.error else "执行失败"
        console.print(f"  × {name}：{brief}", style="red", markup=False, highlight=False)


def _print_run_summary() -> None:
    """非 verbose：把本轮工具调用折叠为一行摘要；无工具调用则不输出。"""
    if not _tool_counts:
        return
    parts = [f"{name} ×{n}" for name, n in _tool_counts.items()]
    summary = f"共 {_round_no} 轮 · 调用工具 {sum(_tool_counts.values())} 次（{'、'.join(parts)}）"
    if _tool_failures:
        summary += f"，失败 {_tool_failures} 次"
    console.print(summary, style="dim", markup=False, highlight=False)


def _make_approval_handler():
    def handler(command: str) -> bool:
        _status_stop()  # 停掉 spinner，避免与审批输入提示交错
        _print_tagged(TAG_WARN, f"\n危险命令，需要确认：{command}", style="bold yellow")
        # input 的 prompt 默认按 markup 解析，[WARN]/[y/N] 需转义避免被误当样式标记
        prompt = f"{escape(TAG_WARN)} 确认执行？{escape('[y/N]')} "
        answer = console.input(prompt).strip().lower()
        return answer in ("y", "yes")

    return handler


def _relaunch_in_new_window() -> None:
    """以独立控制台窗口重新启动本 CLI（Windows 的 CREATE_NEW_CONSOLE）。"""
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)  # 非 Windows 平台退化为普通子进程
    subprocess.Popen(
        [sys.executable, "-m", "agent.cli"],
        cwd=os.getcwd(),  # 继承当前工作目录，新窗口内 agent 的工作目录不变
        env=child_env(),  # 新窗口内的 Python 同样使用 UTF-8，避免中文乱码
        creationflags=creationflags,
    )


def main() -> None:
    global _verbose
    setup_stdio()  # 统一终端编码为 UTF-8：重定向到管道/文件时中文与符号不乱码
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
        f"输出目录：out/ · Token 预算：{config.token_budget} · 最大轮数：{config.max_rounds}",
        title="[bold green]Coding Agent[/bold green]",
        border_style="green",
    ))
    console.print(
        "直接输入任务开始 · /help 命令 · /verbose 完整过程 · /reset 清空 · /quit 退出",
        style="dim", markup=False, highlight=False,
    )
    console.print()

    # 运行轨迹日志：记录每轮工具调用/结果/用量与终止原因，写入 logs/*.jsonl 供回看审计
    trace = TraceLogger(Path("logs"))
    console.print(f"轨迹日志：{trace.path}", style="dim", markup=False, highlight=False)

    # 流式文本在每个 agent.run 内通过回调实时打印
    agent = AgentLoop(
        config,
        client,
        history,
        on_text=_on_text,
        on_tool_call=_on_tool_call,
        on_tool_result=_on_tool_result,
        on_status=_on_status,
        trace=trace,
    )

    while True:
        try:
            user_input = console.input("\n[bold cyan]User > [/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            _print_tagged(TAG_INFO, "\n已退出。")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            if user_input == "/quit":
                break
            elif user_input == "/help":
                console.print(Panel.fit(
                    HELP_TEXT,
                    title="[bold cyan]命令[/bold cyan]",
                    border_style="cyan",
                    subtitle="其他输入作为编程任务交给 agent 自主完成",
                ))
            elif user_input == "/verbose":
                _verbose = not _verbose
                _print_tagged(TAG_INFO, f"完整过程输出已{'开启' if _verbose else '关闭'}。", style="green")
            elif user_input == "/reset":
                history.reset()
                _print_tagged(TAG_INFO, "上下文已清空。", style="green")
            elif user_input == "/tokens":
                _print_tagged(
                    TAG_INFO,
                    f"累计 token 用量：{agent.total_tokens_used} / 预算 {config.token_budget}；"
                    f"当前上下文约 {history.total_tokens()} tokens",
                )
            else:
                _print_tagged(TAG_INFO, f"未知命令：{user_input}，输入 /help 查看帮助", style="yellow")
            continue

        # ---- 三明治布局：User > 输入 → Agent 主体（折叠摘要或 /verbose 完整流水）→ 结论面板 ----
        console.print()  # User > 与主体之间留空
        start_time = time.monotonic()              # 本次任务计时起点
        tokens_before = agent.total_tokens_used    # 本次任务 token 增量起点
        try:
            answer = agent.run(user_input)
            _status_stop()  # 渲染尾部前停掉 spinner，避免与结论块交错
            console.print()  # 主体与尾部之间空行分隔
            # 终止/错误提示（[...] 开头）不属于结论，按元信息展示；正常回答渲染尾部
            if answer.startswith("[API 错误]"):
                _print_tagged(TAG_ERROR, answer, style="red")
            elif answer.startswith("["):
                _print_tagged(TAG_INFO, answer, style="yellow")
            else:
                if not _verbose and _tool_counts:
                    _print_run_summary()
                    console.print()
                _print_tail(answer)
        except KeyboardInterrupt:
            # 终止条件 4：用户中断当前任务，回到输入态
            _print_tagged(TAG_INFO, "\n已中断当前任务。", style="yellow")
        except Exception as e:
            _print_tagged(TAG_ERROR, f"\n发生未预期错误：{type(e).__name__}: {e}", style="red")
        finally:
            _status_stop()  # 任何退出路径都确保 spinner 不残留

        # 本次任务元信息脚注：耗时 + token 用量（dim 弱化，与结论区分）
        elapsed = time.monotonic() - start_time
        tokens_this_task = agent.total_tokens_used - tokens_before
        console.print(
            "\n",
            f"耗时 {elapsed:.1f}s · 本次 {tokens_this_task} tokens · "
            f"上下文 {history.total_tokens()} tokens · "
            f"累计 {agent.total_tokens_used}/{config.token_budget}",
            style="dim", markup=False, highlight=False,
        )
        # 任务边界：弱分隔线区分连续任务，避免输出黏连（斜杠命令已 continue，不会走到这里）
        console.print("─" * console.width, style="dim")

    trace.close()


if __name__ == "__main__":
    main()
