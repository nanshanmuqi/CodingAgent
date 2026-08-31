"""CLI 入口：REPL 循环、三明治布局输出、工作状态显示、斜杠命令、危险命令审批交互。

界面基于 rich 的行内渲染：不接管终端屏幕、保留原有滚动历史，
避免全屏应用带来的渲染残留与布局跳动。模型文本一律 markup=False
输出，方括号不会被误解析为样式标记。

每个任务的输出为"三明治"布局（过程与结论分区，结论一眼可达）：
  头部  User > 前缀标记用户输入，Agent > 标记 agent 回合开始
  主体  默认折叠：过程只在 spinner 中闪过，结束后输出一行摘要（共 N 轮 · 调用工具 M 次）
        /verbose 切换为完整流水（══ 阶段 N ══ + [RESULT] 执行结果摘要）
  尾部  结论以 Markdown 渲染于带标题面板中，随后 [PROMPT] 下一步建议

标记分层（参考成熟 CLI 产品：元信息与任务内容分离、过程弱化、结论强化）：
  结构标记  User > / Agent > / 折叠摘要 / ══ 阶段 / 结论面板
  内容标记  [RESULT] / [PROMPT]（半角，任务过程与建议）
  元信息    【系统】/【警告】/【错误】（全角，框架提示，与任务内容区分）
所有符号均在 GBK 字符集内，Windows 中文终端不会显示为豆腐块。

启动方式：
  python -m agent.cli                在当前终端交互
  python -m agent.cli --new-window   唤起一个独立的新终端窗口进行交互
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.status import Status

from .client import LLMClient
from .config import load_config
from .context import MessageHistory
from .encoding import child_env, setup_stdio
from .loop import AgentLoop
from .permissions import set_approval_handler
from .tools.base import ToolResult

console = Console()

HELP_TEXT = """\
可用命令：
  /help     显示本帮助
  /verbose  切换完整过程输出（默认只显示折叠摘要）
  /reset    清空对话上下文
  /tokens   查看累计 token 用量
  /quit     退出
其他输入将作为编程任务交给 agent 自主完成。
"""

# ---- 元信息标签（全角，框架提示层；任务内容层标记 [RESULT]/[PROMPT] 直接内联） ----
TAG_SYS = "【系统】"     # 帮助/用量/重置/退出/终止等系统提示
TAG_WARN = "【警告】"    # 危险命令审批
TAG_ERROR = "【错误】"   # 未预期异常与 API 错误

MAX_TITLE_LEN = 40    # 阶段标题（模型该轮一句话说明）最大长度
MAX_ERROR_LEN = 120   # [RESULT] 失败行附带的错误摘要最大长度

# 模型自带、由本程序统一渲染的建议小标题（结论区渲染时跳过，避免与 [PROMPT] 区重复）
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
    """阶段分隔：══ 阶段 N：名称 ══；名称取模型该轮的一句话说明，无则退化。"""
    if _round_no > 1:
        console.print()  # 阶段之间空行，增大间距
    title = next((l.strip() for l in _round_text.splitlines() if l.strip()), "")
    title = title[:MAX_TITLE_LEN] or "工具执行"
    console.print(f"══ 阶段 {_round_no}：{title} ══", style="bold blue", markup=False, highlight=False)


def _print_tail(answer: str) -> None:
    """尾部：结论以 Markdown 渲染于带标题面板中，随后 [PROMPT] 下一步建议。

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
    if body:
        console.print(Panel(Markdown(body), title="结论", border_style="green", expand=False))
    if prompts:
        console.print()
        for text in prompts:
            console.print(f"  [PROMPT] {text}", style="magenta", markup=False, highlight=False)


def _on_status(event: str, round_no: int) -> None:
    global _round_no, _round_text, _stage_printed, _tool_counts, _tool_failures
    # 每次请求模型前主循环触发：新一轮开始，重置本轮文本缓冲与阶段标记
    if event == "waiting_model":
        if round_no == 1:  # 每个任务第一轮重置折叠摘要统计
            _tool_counts, _tool_failures = {}, 0
        _round_no, _round_text, _stage_printed = round_no, "", False
        _status_start(f"[magenta]思考中（第 {round_no} 轮），等待模型响应…[/magenta]")


def _on_text(text: str) -> None:
    global _round_text
    if not _round_text:
        # 首个流式 token 到达：切换 spinner 文案（_status_start 内部会先停旧状态）
        _status_start("[magenta]模型输出中…[/magenta]")
    _round_text += text  # 只缓冲不打印：工具轮文本将成为阶段标题，最终轮文本由尾部统一展示


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
        console.print(f"  [RESULT] √ {summary}", style="green", markup=False, highlight=False)
    else:
        brief = result.error.splitlines()[0][:MAX_ERROR_LEN] if result.error else "执行失败"
        console.print(f"  [RESULT] × {name}：{brief}", style="red", markup=False, highlight=False)


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
        answer = console.input(f"{TAG_WARN} 确认执行？[y/N] ").strip().lower()
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
            user_input = console.input("\n[bold cyan]User > [/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            _print_tagged(TAG_SYS, "\n已退出。")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            if user_input == "/quit":
                break
            elif user_input == "/help":
                _print_tagged(TAG_SYS, HELP_TEXT)
            elif user_input == "/verbose":
                _verbose = not _verbose
                _print_tagged(TAG_SYS, f"完整过程输出已{'开启' if _verbose else '关闭'}。", style="green")
            elif user_input == "/reset":
                history.reset()
                _print_tagged(TAG_SYS, "上下文已清空。", style="green")
            elif user_input == "/tokens":
                _print_tagged(
                    TAG_SYS,
                    f"累计 token 用量：{agent.total_tokens_used} / 预算 {config.token_budget}；"
                    f"当前上下文约 {history.total_tokens()} tokens",
                )
            else:
                _print_tagged(TAG_SYS, f"未知命令：{user_input}，输入 /help 查看帮助", style="yellow")
            continue

        # ---- 三明治布局：User > 输入 → Agent > 主体（折叠摘要或 /verbose 完整流水）→ 结论面板 ----
        console.print("Agent >", style="bold green", highlight=False)
        try:
            answer = agent.run(user_input)
            _status_stop()  # 渲染尾部前停掉 spinner，避免与结论块交错
            console.print()  # 主体与尾部之间空行分隔
            # 终止/错误提示（[...] 开头）不属于结论，按元信息展示；正常回答渲染尾部
            if answer.startswith("[API 错误]"):
                _print_tagged(TAG_ERROR, answer, style="red")
            elif answer.startswith("["):
                _print_tagged(TAG_SYS, answer, style="yellow")
            else:
                if not _verbose and _tool_counts:
                    _print_run_summary()
                    console.print()
                _print_tail(answer)
        except KeyboardInterrupt:
            # 终止条件 4：用户中断当前任务，回到输入态
            _print_tagged(TAG_SYS, "\n已中断当前任务。", style="yellow")
        except Exception as e:
            _print_tagged(TAG_ERROR, f"\n发生未预期错误：{type(e).__name__}: {e}", style="red")
        finally:
            _status_stop()  # 任何退出路径都确保 spinner 不残留


if __name__ == "__main__":
    main()
