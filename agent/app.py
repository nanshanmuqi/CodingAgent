"""Textual 全屏 TUI：Coding Agent 的交互界面（P0 脚手架）。

架构：
- 同步阻塞的 AgentLoop 在后台线程执行，回调只向线程安全队列投递事件；
- UI 主循环用 set_interval 定时 drain 队列，再更新 Widget。

这样 agent 核心（loop/client/tools/context/permissions）完全不动，
输出层从 append-only 的 console.print 换成可重绘的 Widget。
"""
from __future__ import annotations

import json
import os
import queue
import re
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from rich.markdown import Markdown
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static

from .client import LLMClient
from .config import Config
from .context import MessageHistory
from .loop import AgentLoop, RunResult, RunStatus
from .permissions import set_approval_handler
from .session import SessionStore, new_session_id
from .tools.base import ToolResult
from .trace import TraceLogger

# 展开详情时失败信息的单行截断长度
MAX_ERROR_LEN = 200


def format_tool_call(name: str, tool_call: dict) -> str:
    """生成一行可观测标题：工具名 + 关键参数（如 read_file a.txt）。"""
    try:
        args = json.loads(tool_call.get("function", {}).get("arguments", "") or "{}")
    except (json.JSONDecodeError, TypeError):
        return name
    key = {
        "read_file": "path", "write_file": "path", "edit_file": "path",
        "run_command": "command", "grep": "pattern", "glob": "pattern",
    }.get(name)
    value = args.get(key) if key else None
    if value is None:
        return name
    text = str(value)
    return f"{name} {text[:60]}" if len(text) <= 60 else f"{name} {text[:60]}…"


# 模型自带、由本程序统一渲染的建议小标题（正文渲染时跳过，避免与建议区重复）
_SUGGESTION_HEADERS = {"下一步建议", "后续建议"}


def _filter_body(text: str) -> str:
    """过滤建议行（[PROMPT]）与模型自带小标题，返回 Markdown 正文源文本。"""
    body_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if "[PROMPT]" in stripped or stripped.rstrip("：:").strip() in _SUGGESTION_HEADERS:
            continue
        body_lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(body_lines).strip())


def _extract_prompts(text: str) -> list[str]:
    """从最终回答中提取 [PROMPT] 建议项。"""
    prompts = []
    for line in text.splitlines():
        stripped = line.strip()
        if "[PROMPT]" in stripped:
            prompts.append(stripped.split("[PROMPT]", 1)[1].strip("：: "))
    return prompts


MAX_DIFF_LINES = 40


def _diff_stat(diff_text: str) -> str:
    """从 diff 文本统计 +N -M，用于折叠态摘要。"""
    adds = dels = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            adds += 1
        elif line.startswith("-"):
            dels += 1
    return f"+{adds} -{dels}" if (adds or dels) else ""


def _diff_lines(diff_text: str) -> list[Text]:
    """把 unified diff 逐行上色：+ 绿 / - 红 / 头信息 dim / hunk cyan。"""
    raw = diff_text.splitlines()
    colored = [_colorize_diff_line(line) for line in raw[:MAX_DIFF_LINES]]
    if len(raw) > MAX_DIFF_LINES:
        colored.append(Text(f"… 共 {len(raw)} 行，已截断", style="dim"))
    return colored


def _colorize_diff_line(line: str) -> Text:
    if line.startswith("+++") or line.startswith("---"):
        return Text(line, style="dim")
    if line.startswith("@@"):
        return Text(line, style="cyan")
    if line.startswith("+"):
        return Text(line, style="green")
    if line.startswith("-"):
        return Text(line, style="red")
    return Text(line)


# 斜杠命令注册表：(命令, 描述)
COMMANDS: list[tuple[str, str]] = [
    ("/help", "显示帮助"),
    ("/reset", "清空对话上下文"),
    ("/resume", "恢复历史会话"),
    ("/quit", "退出程序"),
]

# 工具类型 -> 圆点颜色（用于过程块折叠态/展开态的彩色圆点）
TOOL_COLORS = {
    "read_file": "cyan",
    "write_file": "green",
    "edit_file": "yellow",
    "run_command": "magenta",
    "grep": "blue",
    "glob": "white",
}

# 状态栏阶段 -> 颜色
PHASE_STYLES = {
    "空闲": "dim",
    "思考中": "yellow",
    "执行工具": "cyan",
    "完成": "green",
}


def _help_text() -> Text:
    """构建带颜色的帮助面板内容：命令名/快捷键着色，描述用 dim。"""
    t = Text("命令：\n", style="bold")
    for cmd, desc in COMMANDS:
        t.append(f"  {cmd:<8}", style="bold cyan")
        t.append(f"{desc}\n", style="dim")
    t.append("\n快捷键：\n", style="bold")
    for key, desc in (("Tab", "补全斜杠命令"), ("Ctrl+C", "中断任务"), ("Ctrl+Q", "退出窗口")):
        t.append(f"  {key:<8}", style="bold green")
        t.append(f"{desc}\n", style="dim")
    return t


class UserMessage(Static):
    """用户输入：`▍User` 前缀加粗青色，正文默认色。"""

    def __init__(self, text: str) -> None:
        super().__init__(
            Text.assemble(("▍User  ", "bold cyan"), (text, "")),
            classes="user-msg",
        )


class AgentMessage(Horizontal):
    """Agent 正文：流式累积，Markdown 渲染，带 `▍Agent` 角色前缀。"""

    def __init__(self) -> None:
        super().__init__(classes="agent-msg")
        self._buf = ""
        self._body = Static("", classes="agent-body")

    def compose(self) -> ComposeResult:
        yield Static(Text("▍Agent", style="bold green"), classes="agent-prefix")
        yield self._body

    def append(self, chunk: str) -> None:
        self._buf += chunk
        body = _filter_body(self._buf)
        self._body.update(Markdown(body) if body else Text("…", style="dim"))


class SystemMessage(Static):
    """系统元信息（错误/终止提示），独立于会话正文。"""

    def __init__(self, text, style: str = "red") -> None:
        content = text if isinstance(text, Text) else Text(str(text), style=style)
        super().__init__(content, classes="sys-msg")


class Divider(Static):
    """对话之间的分隔线。"""

    def __init__(self) -> None:
        super().__init__("", classes="divider")


class HelpPanel(Static):
    """帮助面板：命令与快捷键带颜色，独立于对话正文。"""

    def __init__(self) -> None:
        super().__init__(_help_text(), classes="help-panel")


@dataclass
class _ToolStep:
    """单个工具步骤的数据：标题 + diff 统计 + 详情行。"""
    title: str
    stat: str = ""
    lines: list[Text] = field(default_factory=list)


class TaskProcess(Static):
    """一个任务的工具过程：默认折叠成一行摘要，展开显示全部步骤。"""

    def __init__(self) -> None:
        super().__init__("", classes="task-process")
        self._steps: list[_ToolStep] = []
        self._counts: dict[str, int] = {}
        self.expanded = False
        self._refresh()

    def add_step(self, title: str) -> _ToolStep:
        step = _ToolStep(title)
        self._steps.append(step)
        name = title.split(" ", 1)[0]
        self._counts[name] = self._counts.get(name, 0) + 1
        self._refresh()
        return step

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self._refresh()

    def on_click(self) -> None:
        self.toggle()

    def _refresh(self) -> None:
        hint = "Ctrl+O 折叠" if self.expanded else "Ctrl+O 展开"
        header_parts: list = [Text("● 过程", style="dim")]
        if self._counts:
            counts = []
            for name, count in self._counts.items():
                color = TOOL_COLORS.get(name, "dim")
                counts.append(Text(f"● {name} ×{count}", style=color))
            header_parts.append(Text("  "))
            header_parts.append(Text(" · ", style="dim").join(counts))
        header_parts.append(Text(f"   {hint}", style="dim"))
        header = Text.assemble(*header_parts)
        if self.expanded and self._steps:
            parts: list = [header]
            for step in self._steps:
                title = step.title + (f"  {step.stat}" if step.stat else "")
                color = TOOL_COLORS.get(step.title.split(" ", 1)[0], "dim")
                parts.append(Text("\n  ● ", style=color))
                parts.append(Text(title, style="dim"))
                for line in step.lines:
                    parts.append(Text("\n    "))
                    parts.append(line)
            self.update(Text.assemble(*parts))
        else:
            self.update(header)


class ConversationLog(VerticalScroll):
    """主滚动区：容纳用户消息 / agent 正文 / 任务过程块。"""

    def __init__(self) -> None:
        super().__init__(id="log")
        self.processes: list[TaskProcess] = []

    def add_user(self, text: str) -> None:
        if self.children:
            self.mount(Divider())
        self.mount(UserMessage(text))
        self.scroll_end(animate=False)

    def add_system(self, text, style: str = "red") -> None:
        self.mount(SystemMessage(text, style))
        self.scroll_end(animate=False)

    def add_help(self) -> None:
        self.mount(HelpPanel())
        self.scroll_end(animate=False)

    def add_agent_stream(self) -> AgentMessage:
        msg = AgentMessage()
        self.mount(msg)
        self.scroll_end(animate=False)
        return msg

    def add_prompts(self, prompts: list[str]) -> None:
        """尾部建议区：正文已在流式阶段渲染，这里只渲染 Next steps。"""
        text = Text("Next steps:", style="bold yellow")
        for p in prompts:
            text.append("\n")
            text.append(f"- {p}", style="yellow")
        self.mount(Static(text, classes="prompts-msg"))
        self.scroll_end(animate=False)

    def add_process(self) -> TaskProcess:
        proc = TaskProcess()
        self.processes.append(proc)
        self.mount(proc)
        self.scroll_end(animate=False)
        return proc

    def toggle_last(self) -> None:
        """Ctrl+O：展开/折叠最近一个任务的过程块。"""
        if self.processes:
            self.processes[-1].toggle()

    def clear(self) -> None:
        """清空所有已显示的消息与任务过程块。"""
        for child in list(self.children):
            child.remove()
        self.processes.clear()


class StatusBar(Static):
    """常驻状态栏：模式 / 轮次 / 工具 / 上下文 / token 用量。"""

    def __init__(self) -> None:
        super().__init__("", id="statusbar")
        self.mode = "auto"
        self.round = 0
        self.phase = "空闲"
        self.tools = 0
        self.context_pct = 0
        self.tokens = 0
        self.budget = 0
        self.elapsed = 0.0
        self._refresh()

    def set_round(self, round_no: int, phase: str) -> None:
        self.round = round_no
        self.phase = phase
        self._refresh()

    def set_tools(self, count: int) -> None:
        self.tools = count
        self.phase = "执行工具"
        self._refresh()

    def set_usage(self, tokens: int, budget: int, context_pct: int) -> None:
        self.tokens = tokens
        self.budget = budget
        self.context_pct = context_pct
        self._refresh()

    def set_elapsed(self, seconds: float) -> None:
        self.elapsed = seconds
        self._refresh()

    def _refresh(self) -> None:
        phase_style = PHASE_STYLES.get(self.phase, "dim")
        parts = [
            Text(self.mode, style="dim"),
            Text(f"第{self.round}轮·{self.phase}" if self.round else self.phase, style=phase_style),
        ]
        if self.elapsed:
            parts.append(Text(f"{self.elapsed:.0f}s", style="dim"))
        if self.tools:
            parts.append(Text(f"工具{self.tools}", style="dim"))
        parts.append(Text(f"上下文{self.context_pct}%", style="dim"))
        parts.append(Text(f"累计{self.tokens}/{self.budget}", style="dim"))
        self.update(Text(" · ", style="dim").join(parts))


class ApprovalModal(ModalScreen[bool]):
    """危险命令确认弹窗：阻塞 worker 线程直到用户选择。"""

    BINDINGS = [
        Binding("y", "approve", "执行"),
        Binding("n", "reject", "拒绝"),
    ]

    def __init__(self, command: str, on_decision) -> None:
        super().__init__()
        self.command = command
        self.on_decision = on_decision

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-dialog"):
            yield Static("危险命令，需要确认：", classes="approval-title")
            yield Static(self.command, classes="approval-command")
            with Horizontal(id="approval-buttons"):
                yield Button("执行 \\[y]", variant="error", id="yes")
                yield Button("拒绝 \\[n]", variant="primary", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "yes":
            self._decide(True)
        elif event.button.id == "no":
            self._decide(False)

    def action_approve(self) -> None:
        self._decide(True)

    def action_reject(self) -> None:
        self._decide(False)

    def _decide(self, ok: bool) -> None:
        self.on_decision(ok)
        self.dismiss(ok)


class SessionPickerModal(ModalScreen[str | None]):
    """会话选择器：上下键选择，Enter 恢复，Esc 取消。"""

    def __init__(self, sessions: list[dict]) -> None:
        super().__init__()
        self._sessions = sessions

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Static(
                "选择要恢复的会话（↑/↓ 选择，Enter 恢复，Esc 取消）：",
                classes="picker-title",
            )
            yield OptionList(
                *[self._format(s) for s in self._sessions],
                id="picker-options",
            )

    def on_mount(self) -> None:
        self.query_one("#picker-options").focus()

    @staticmethod
    def _format(s: dict) -> str:
        sid = s.get("session_id", "")
        title = s.get("title", "") or "(无标题)"
        return f"{sid}  {title}"

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._sessions[event.option_index]["session_id"])


class CommandInput(Input):
    """带斜杠命令补全的输入框：Tab 补全到匹配命令。"""

    BINDINGS = [
        Binding("tab", "complete", "补全", show=False),
    ]

    def __init__(self, commands: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._commands = commands
        self._base = ""            # 补全基准：用户手动输入、尚未补全的 / 前缀
        self._matches: list[str] = []
        self._index = -1
        self._applying = False

    def watch_value(self, value: str) -> None:
        """用户手动改动输入时重算候选；程序补全写入时跳过。"""
        if self._applying:
            return
        base = value if (value.startswith("/") and " " not in value) else ""
        if base != self._base:
            self._base = base
            self._matches = [c for c in self._commands if c.startswith(base)] if base else []
            self._index = -1

    def _apply(self) -> None:
        if not self._matches:
            return
        self._applying = True
        try:
            self.value = self._matches[self._index]
            self.cursor_position = len(self.value)
        finally:
            self._applying = False

    def action_complete(self) -> None:
        if not self._matches:
            return
        if self._index == -1:
            self._index = 0
        else:
            self._index = (self._index + 1) % len(self._matches)
        self._apply()


class AgentApp(App):
    """Coding Agent 的 Textual 界面。"""

    TITLE = "Coding Agent"

    CSS = """
    Screen {
        layout: vertical;
    }
    #header {
        height: 3;
        padding: 0 1;
        background: $panel;
        content-align: left middle;
    }
    #header-sep {
        height: 1;
        background: $surface;
    }
    #log {
        height: 1fr;
        padding: 1 1;
    }
    #statusbar {
        height: 1;
        padding: 0 1;
        background: $panel;
    }
    #input {
        height: 3;
        padding: 0 1;
    }
    .user-msg {
        margin-top: 1;
        margin-bottom: 1;
        padding: 0 1;
    }
    .agent-msg {
        height: auto;
        margin-bottom: 1;
        padding: 0 1;
    }
    .agent-prefix {
        width: 7;
        color: green;
    }
    .agent-body {
        width: 1fr;
    }
    .task-process {
        margin: 1 0;
    }
    .sys-msg {
        margin-top: 1;
    }
    .divider {
        height: 1;
        margin: 1 0;
        background: $surface;
    }
    .help-panel {
        margin: 1 0;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    MarkdownFence {
        background: $surface;
        border: round $primary;
        padding: 0 1;
    }
    .prompts-msg {
        margin-bottom: 1;
        padding: 0 1;
        border-left: heavy yellow;
    }
    #approval-dialog {
        width: 72;
        height: auto;
        border: round $error;
        background: $surface;
        padding: 1 2;
    }
    #approval-dialog .approval-command {
        margin: 1 0;
        color: $error;
    }
    #approval-buttons {
        height: auto;
        margin-top: 1;
    }
    #picker-dialog {
        width: 72;
        height: auto;
        max-height: 22;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #picker-dialog .picker-title {
        margin-bottom: 1;
    }
    #picker-options {
        height: auto;
        max-height: 16;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_task", "中断任务", show=False, priority=True),
        Binding("ctrl+o", "toggle_step", "展开/折叠", show=False),
    ]

    def __init__(
        self,
        config: Config,
        trace: TraceLogger,
        session_id: str | None = None,
        messages: list[dict] | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.client = LLMClient(config)
        self.history = MessageHistory(
            max_context_tokens=config.max_context_tokens,
            model_name=config.model_name,
            messages=messages,
        )
        self.agent = AgentLoop(
            config,
            self.client,
            self.history,
            on_text=self._on_text,
            on_tool_call=self._on_tool_call,
            on_tool_result=self._on_tool_result,
            on_status=self._on_status,
            trace=trace,
        )
        set_approval_handler(self._make_approval_handler())
        self._install_sigint_handler()

        self._store = SessionStore()
        self._session_id = session_id or new_session_id()
        self._tool_details: dict[str, dict] = {}  # call_id -> {title, stat, diff/detail}

        self._events: queue.Queue = queue.Queue()
        self._pending_steps: deque[tuple[str, _ToolStep]] = deque()
        self._current_stream: Optional[AgentMessage] = None
        self._current_process: Optional[TaskProcess] = None
        self._tool_count = 0
        self._round_no = 0
        self._mode = "auto"
        self._task_running = False  # 是否有任务正在后台线程执行（避开 Textual App._running 命名冲突）
        self._started_at = 0.0      # 当前任务开始时间（time.monotonic），用于状态栏计时
        self._input: Optional[CommandInput] = None

    def compose(self) -> ComposeResult:
        yield Static(self._header_text(), id="header")
        yield Static("", id="header-sep")
        self.conversation = ConversationLog()
        yield self.conversation
        self.statusbar = StatusBar()
        yield self.statusbar
        self._input = CommandInput(
            commands=[name for name, _ in COMMANDS],
            placeholder="输入任务…（Tab 补全 / 命令 · Ctrl+Q 退出）",
            id="input",
        )
        yield self._input

    def on_mount(self) -> None:
        self.statusbar.set_usage(0, self.config.token_budget, self._context_pct())
        self.query_one("#input").focus()
        self.set_interval(0.05, self._drain)
        self.set_interval(1.0, self._tick_elapsed)
        self.conversation.add_system("直接输入任务开始 · Ctrl+C 中断 · Ctrl+Q 退出窗口", style="dim")

    # ---- 回调（worker 线程触发，只投递事件，不碰 Widget）----

    def _on_text(self, chunk: str) -> None:
        self._events.put(("text", chunk))

    def _on_tool_call(self, name: str, tool_call: dict) -> None:
        self._events.put(("tool_call", name, tool_call))

    def _on_tool_result(self, name: str, result: ToolResult) -> None:
        self._events.put(("tool_result", name, result))

    def _on_status(self, event: str, round_no: int) -> None:
        self._events.put(("status", event, round_no))

    def _run_agent(self, task: str) -> None:
        try:
            result = self.agent.run(task)
        except Exception as e:
            result = RunResult(RunStatus.ERROR, f"{type(e).__name__}: {e}")
        self._events.put(("done", result))

    # ---- 输入提交：启动后台线程执行 agent ----

    @on(Input.Submitted)
    def on_input_submitted(self, event: Input.Submitted) -> None:
        task = event.value.strip()
        if not task:
            return
        event.input.value = ""
        if task.startswith("/"):
            self._handle_command(task)
            return
        self.conversation.add_user(task)
        self._reset_task_state()
        self.statusbar.set_round(0, "思考中")
        self._set_running(True)
        threading.Thread(target=self._run_agent, args=(task,), daemon=True).start()

    def _handle_command(self, cmd: str) -> None:
        """处理斜杠命令：拦截 / 开头的输入，不交给模型。"""
        if cmd in ("/quit", "/exit"):
            self.exit()
        elif cmd == "/help":
            self.conversation.add_help()
        elif cmd == "/reset":
            self.history.reset()
            self.agent.total_tokens_used = 0
            self._reset_task_state()
            self._tool_details = {}
            self.conversation.clear()
            self.conversation.add_system("上下文已清空，token 用量已重置。", style="green")
            self.statusbar.set_usage(0, self.config.token_budget, self._context_pct())
        elif cmd == "/resume" or cmd.startswith("/resume "):
            arg = cmd[len("/resume"):].strip()
            if arg:
                data = self._store.load(arg)
                if data is None:
                    self.conversation.add_system(f"未找到会话：{arg}", style="red")
                else:
                    self._restore_session(arg, data)
            else:
                sessions = self._store.list_sessions()
                if not sessions:
                    self.conversation.add_system("没有可恢复的会话。", style="dim")
                else:
                    self._show_session_picker(sessions)
        else:
            self.conversation.add_system(f"未知命令：{cmd}，输入 /help 查看帮助", style="dim")

    def _show_session_picker(self, sessions: list[dict]) -> None:
        def on_selected(session_id: str | None) -> None:
            if session_id:
                data = self._store.load(session_id)
                if data:
                    self._restore_session(session_id, data)

        self.push_screen(SessionPickerModal(sessions), callback=on_selected)

    def _restore_session(self, session_id: str, data: dict) -> None:
        messages = data.get("messages") or []
        tool_details = data.get("tool_details") or {}
        self.history.restore(messages)
        self._session_id = session_id
        self._tool_details = dict(tool_details)
        self.conversation.clear()

        current_process: Optional[TaskProcess] = None
        for m in messages:
            role = m.get("role")
            if role == "user":
                self.conversation.add_user(str(m.get("content", "")))
                current_process = None  # 新任务：过程块重置
            elif role == "assistant":
                if m.get("content"):
                    self.conversation.add_agent_stream().append(str(m["content"]))
                for tc in m.get("tool_calls") or []:
                    if current_process is None:
                        current_process = self.conversation.add_process()
                    self._restore_step(current_process, tc, tool_details)

        title = data.get("title") or "(无标题)"
        self.conversation.add_system(f"已恢复会话 {session_id}：{title}", style="green")
        self._refresh_header()
        self._reset_task_state()
        self.statusbar.set_usage(
            self.agent.total_tokens_used, self.config.token_budget, self._context_pct()
        )

    def _restore_step(self, process: TaskProcess, tc: dict, tool_details: dict) -> None:
        call_id = tc.get("id", "")
        detail = tool_details.get(call_id) or {}
        name = tc.get("function", {}).get("name", "")
        title = detail.get("title") or format_tool_call(name, tc)
        step = process.add_step(title)
        stat = detail.get("stat", "")
        if stat:
            step.stat = stat
        diff = detail.get("diff", "")
        if diff:
            step.lines = _diff_lines(diff)
        else:
            step.lines = [Text(l) for l in (detail.get("detail") or "").splitlines()]
        process._refresh()

    def _refresh_header(self) -> None:
        self.query_one("#header").update(self._header_text())

    # ---- 审批：worker 线程阻塞等待 UI 弹窗结果 ----

    def _make_approval_handler(self):
        def handler(command: str) -> bool:
            result: dict = {}
            evt = threading.Event()
            self.call_from_thread(self._request_approval, command, result, evt)
            evt.wait()
            return bool(result.get("ok", False))

        return handler

    def _request_approval(self, command: str, result: dict, evt: threading.Event) -> None:
        def on_decision(ok: bool) -> None:
            result["ok"] = ok
            evt.set()

        self.push_screen(ApprovalModal(command, on_decision))

    # ---- 事件消费（UI 主循环）----

    def _drain(self) -> None:
        try:
            while True:
                self._handle(self._events.get_nowait())
        except queue.Empty:
            pass

    def _handle(self, event) -> None:
        kind = event[0]
        if kind == "text":
            if self._current_stream is None:
                self._current_stream = self.conversation.add_agent_stream()
            self._current_stream.append(event[1])
        elif kind == "status":
            _, event_name, round_no = event
            if event_name == "waiting_model":
                self._round_no = round_no
                self._current_stream = None  # 新一轮文本进入新的消息块
                self.statusbar.set_round(round_no, "思考中")
        elif kind == "tool_call":
            _, name, tool_call = event
            self._tool_count += 1
            if self._current_process is None:
                self._current_process = self.conversation.add_process()
            title = format_tool_call(name, tool_call)
            step = self._current_process.add_step(title)
            self._pending_steps.append((tool_call.get("id", ""), step))
            self.statusbar.set_tools(self._tool_count)
        elif kind == "tool_result":
            _, name, result = event
            if self._pending_steps:
                call_id, step = self._pending_steps.popleft()
                if result.diff:
                    step.stat = _diff_stat(result.diff)
                    step.lines = _diff_lines(result.diff)
                    self._tool_details[call_id] = {
                        "title": step.title, "stat": step.stat, "diff": result.diff,
                    }
                else:
                    detail = self._result_detail(result)
                    step.lines = [Text(l) for l in detail.splitlines()]
                    self._tool_details[call_id] = {"title": step.title, "detail": detail}
                if self._current_process is not None:
                    self._current_process._refresh()
        elif kind == "done":
            self._finish(event[1])

    @staticmethod
    def _result_detail(result: ToolResult) -> str:
        if result.ok:
            return result.summary or "完成"
        brief = (result.error or "执行失败").splitlines()[0][:MAX_ERROR_LEN]
        return f"× {brief}"

    def _finish(self, result: RunResult) -> None:
        self._set_running(False)
        if result.status == RunStatus.ERROR:
            self.conversation.add_system(f"[ERROR] {result.text}", "red")
        elif result.status == RunStatus.STOPPED:
            self.conversation.add_system(f"[INFO] {result.text}", "yellow")
        else:
            # 最终轮正文已在流式阶段渲染；若模型未返回文本则补一条占位
            if self._current_stream is None:
                self.conversation.add_agent_stream().append(result.text)
            prompts = _extract_prompts(result.text)
            if prompts:
                self.conversation.add_prompts(prompts)
        self.statusbar.set_usage(
            self.agent.total_tokens_used,
            self.config.token_budget,
            self._context_pct(),
        )
        self.statusbar.set_round(self._round_no, "完成")
        self._current_stream = None
        self.conversation.scroll_end(animate=False)
        # 每次任务结束后持久化会话，供 --continue/--resume 恢复
        self._store.save(
            self._session_id,
            self.history.messages,
            self.config.model_name,
            tool_details=self._tool_details,
        )

    # ---- 内部 ----

    def _set_running(self, running: bool) -> None:
        """切换运行态：运行中禁用输入并计时，结束后恢复并重新聚焦。"""
        self._task_running = running
        if running:
            self._started_at = time.monotonic()
            if self._input is not None:
                self._input.disabled = True
                self._input.placeholder = "运行中… Ctrl+C 中断"
        else:
            self._started_at = 0.0
            self.statusbar.set_elapsed(0.0)
            if self._input is not None:
                self._input.disabled = False
                self._input.placeholder = "输入任务…（Tab 补全 / 命令 · Ctrl+Q 退出）"
                self._input.focus()

    def _tick_elapsed(self) -> None:
        """每秒刷新状态栏耗时。"""
        if self._task_running:
            self.statusbar.set_elapsed(time.monotonic() - self._started_at)

    def action_cancel_task(self) -> None:
        """Ctrl+C：中断当前后台任务。"""
        if not self._task_running:
            return
        self.agent.cancel()
        self.conversation.add_system("正在中断当前任务…", style="yellow")

    def _install_sigint_handler(self) -> None:
        """兜底拦截 Ctrl+C 信号，转为请求中断任务，避免进程被直接终止。

        Textual 在 Windows 下会关闭控制台的 ENABLE_PROCESSED_INPUT，把 Ctrl+C
        转成按键交给 ctrl+c → cancel_task 绑定处理；但某些终端（如 ConPTY/Windows
        Terminal）在流式输出期间仍可能把 Ctrl+C 以 SIGINT 信号送达，导致进程被
        直接杀死、窗口关闭。这里再兜底一层，把信号改为置位中断标志。
        """
        def handler(signum, frame):
            self.agent.cancel()

        try:
            signal.signal(signal.SIGINT, handler)
        except (ValueError, OSError):
            pass  # 非主线程或平台不支持时忽略

    def _reset_task_state(self) -> None:
        self._pending_steps.clear()
        self._current_process = None
        self._current_stream = None
        self._tool_count = 0
        self._round_no = 0

    def _context_pct(self) -> int:
        total = self.history.total_tokens()
        return min(100, int(total * 100 / max(1, self.config.max_context_tokens)))

    def _header_text(self) -> Text:
        return Text.assemble(
            ("CodingAgent", "bold green"),
            (f"  ·  {self.config.model_name}", "dim"),
            (f"  ·  {self._mode}", "dim"),
            (f"  ·  {os.getcwd()}", "dim"),
            (f"  ·  会话 {self._session_id}", "dim"),
        )

    def action_toggle_step(self) -> None:
        self.conversation.toggle_last()
