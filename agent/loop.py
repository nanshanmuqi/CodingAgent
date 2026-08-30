"""Agent 主循环（自行实现）：
用户输入 → 调用模型 → 若有 tool_calls 本地执行并回填 → 再调用模型 →
直到模型返回纯文本回答。

终止条件（全部显式实现）：
1. 模型返回 stop 且无 tool_calls —— 正常结束
2. 单任务超过 max_rounds 轮 —— 强制停止
3. 累计 token 用量超过 token_budget —— 强制停止
4. 用户 Ctrl+C（KeyboardInterrupt 向上抛给 CLI 处理）
5. 同一工具以相同参数连续失败 3 次 —— 判定卡死，强制停止
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from .client import LLMClient
from .config import Config
from .context import MessageHistory
from .tools import execute_tool_call, tool_schemas

MAX_CONSECUTIVE_SAME_FAILURES = 3


class AgentLoop:
    def __init__(
        self,
        config: Config,
        client: LLMClient,
        history: MessageHistory,
        on_text: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[[str, dict], None]] = None,
        on_tool_result: Optional[Callable[[str, bool], None]] = None,
        on_status: Optional[Callable[[str, int], None]] = None,
    ):
        self._config = config
        self._client = client
        self._history = history
        # 回调：流式文本 / 工具调用开始 / 工具执行结束 / 状态变化（供 CLI 展示，可全部留空）
        self._on_text = on_text
        self._on_tool_call = on_tool_call
        self._on_tool_result = on_tool_result
        # on_status(event, round_no)：目前仅 "waiting_model"（每次请求模型前触发）
        self._on_status = on_status

        self.total_tokens_used = 0  # 累计 token 用量（按 API 返回的 usage 统计）

    def run(self, user_input: str) -> str:
        """执行一轮用户任务，返回 agent 的最终文本回答。"""
        self._history.add_user(user_input)
        schemas = tool_schemas()
        last_failure_signature: Optional[str] = None
        consecutive_failures = 0

        for round_no in range(1, self._config.max_rounds + 1):
            # 终止条件 3：token 预算
            if self.total_tokens_used >= self._config.token_budget:
                return f"[已停止] 累计 token 用量达到预算上限（{self._config.token_budget}）。"

            self._history.prune()

            if self._on_status:
                self._on_status("waiting_model", round_no)

            try:
                response = self._client.chat(
                    self._history.messages, tools=schemas, on_text=self._on_text
                )
            except RuntimeError as e:
                return f"[API 错误] {e}"

            if response.get("usage"):
                self.total_tokens_used += response["usage"]["total_tokens"]

            tool_calls = response.get("tool_calls")
            self._history.add_assistant(response.get("content"), tool_calls)

            # 终止条件 1：无工具调用，模型给出最终回答
            if not tool_calls:
                return response.get("content") or "(模型未返回内容)"

            for tool_call in tool_calls:
                name = tool_call["function"]["name"]
                if self._on_tool_call:
                    self._on_tool_call(name, tool_call)

                result = execute_tool_call(tool_call)

                if self._on_tool_result:
                    self._on_tool_result(name, result.ok)

                self._history.add_tool_result(tool_call["id"], result.to_message_content())

                # 终止条件 5：同一工具同样参数连续失败，判定模型卡死
                if result.ok:
                    last_failure_signature = None
                    consecutive_failures = 0
                else:
                    signature = name + ":" + self._normalize_arguments(tool_call)
                    if signature == last_failure_signature:
                        consecutive_failures += 1
                    else:
                        last_failure_signature = signature
                        consecutive_failures = 1
                    if consecutive_failures >= MAX_CONSECUTIVE_SAME_FAILURES:
                        return (
                            f"[已停止] 工具 {name} 以相同参数连续失败 "
                            f"{MAX_CONSECUTIVE_SAME_FAILURES} 次，模型疑似陷入循环。"
                            f"最后错误：{result.error}"
                        )

        # 终止条件 2：轮数耗尽
        return (
            f"[已停止] 单任务已达到最大轮数（{self._config.max_rounds}）。"
            "可通过 /reset 清空上下文后重试，或调大 MAX_ROUNDS。"
        )

    @staticmethod
    def _normalize_arguments(tool_call: dict) -> str:
        """把工具参数序列化为稳定字符串，用于识别'相同参数'。"""
        raw = tool_call["function"].get("arguments", "")
        try:
            return json.dumps(json.loads(raw), sort_keys=True, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            return raw
