"""对话历史与上下文管理（自行实现）：
- 维护 system / user / assistant / tool 四种角色的消息列表
- token 用量估算（优先 tiktoken，退化为字符数/4）
- 超阈值裁剪：保留 system 与最近对话；较早的工具输出压缩为摘要，仍超限则成组丢弃最旧对话
"""
from __future__ import annotations

import json

try:
    import tiktoken

    _encoding = tiktoken.get_encoding("cl100k_base")
except Exception:  # tiktoken 不可用（如无网络下载词表）时退化
    _encoding = None

# 工具输出超过该长度才压缩为摘要
TOOL_OUTPUT_COMPRESS_THRESHOLD = 400
# 裁剪时保留最近的消息条数
KEEP_RECENT_MESSAGES = 10

SYSTEM_PROMPT = """你是一个编程智能体（coding agent），通过调用工具自主完成用户的编程任务。

工作方式：
1. 理解任务后，自主拆解步骤，交替调用工具：读文件理解现状 → 写/改代码 → 执行命令验证 → 根据结果修正。
2. 文件修改优先使用 edit_file 做局部编辑；新建或整体重写才用 write_file。
3. 执行命令后必须检查退出码与输出，出错时分析原因并重试。
4. 所有文件操作限制在当前工作目录内，路径一律使用相对路径。
5. 任务完成后，用简洁的中文总结你做了什么、验证结果如何。

当前工作目录即用户项目根目录。可用工具：read_file、write_file、edit_file、run_command、grep、glob。"""


def estimate_tokens(text: str) -> int:
    if _encoding is not None:
        try:
            return len(_encoding.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def _message_tokens(message: dict) -> int:
    total = 4  # 每条消息的结构开销
    if message.get("content"):
        total += estimate_tokens(str(message["content"]))
    if message.get("tool_calls"):
        total += estimate_tokens(json.dumps(message["tool_calls"], ensure_ascii=False))
    return total


class MessageHistory:
    def __init__(self, max_context_tokens: int = 60_000):
        self.max_context_tokens = max_context_tokens
        self._messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    @property
    def messages(self) -> list[dict]:
        return self._messages

    def reset(self) -> None:
        self._messages = self._messages[:1]

    def add_user(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str | None, tool_calls: list[dict] | None = None) -> None:
        message: dict = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        self._messages.append(message)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self._messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})

    def total_tokens(self) -> int:
        return sum(_message_tokens(m) for m in self._messages)

    # ---- 裁剪逻辑 ----

    def prune(self) -> None:
        """超过阈值时裁剪上下文，两轮策略：先压缩旧工具输出，再成组丢弃最旧对话。"""
        if self.total_tokens() <= self.max_context_tokens:
            return
        self._compress_old_tool_outputs()
        while self.total_tokens() > self.max_context_tokens and len(self._messages) > KEEP_RECENT_MESSAGES + 1:
            self._drop_oldest_unit()

    def _compress_old_tool_outputs(self) -> None:
        """将较早（不在最近 KEEP_RECENT_MESSAGES 条内）的超长工具输出替换为摘要。"""
        boundary = len(self._messages) - KEEP_RECENT_MESSAGES
        for i in range(1, max(boundary, 1)):
            message = self._messages[i]
            if message.get("role") == "tool":
                content = str(message.get("content", ""))
                if len(content) > TOOL_OUTPUT_COMPRESS_THRESHOLD:
                    first_line = content.splitlines()[0][:120] if content else ""
                    message["content"] = (
                        f"[较早的工具输出已省略，原约 {len(content)} 字符。首行：{first_line}]"
                    )

    def _drop_oldest_unit(self) -> None:
        """丢弃 system 之后最旧的一组消息，保证不留下孤立的 tool 消息。"""
        i = 1
        # 跳过已不允许丢弃的边界
        if i >= len(self._messages):
            return
        # 一个"单元"= 一条非 tool 消息 + 其后所有连续的 tool 消息
        j = i + 1
        while j < len(self._messages) and self._messages[j].get("role") == "tool":
            j += 1
        del self._messages[i:j]
