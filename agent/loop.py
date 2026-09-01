"""Agent 主循环（自行实现）：
用户输入 → 调用模型 → 若有 tool_calls 本地并行执行并回填 → 再调用模型 →
直到模型返回纯文本回答。

终止条件：
1. 模型返回 stop 且无 tool_calls —— 正常结束
2. 单任务超过 max_rounds 轮 —— 强制停止
3. 累计 token 用量超过 token_budget —— 强制停止
4. 用户 Ctrl+C（KeyboardInterrupt 向上抛给 CLI 处理）
5. 同一工具以相同参数连续失败 3 次 —— 判定卡死，强制停止
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from .client import LLMClient
from .config import Config
from .context import MessageHistory
from .tools import ToolResult, execute_tool_call, tool_schemas
from .trace import TraceLogger

MAX_CONSECUTIVE_SAME_FAILURES = 3
# 单轮工具调用的并发上限：防止模型一次发起过多调用时线程数失控
MAX_PARALLEL_TOOL_CALLS = 8


class RunStatus(str, Enum):
    """一次任务执行的结束状态。"""
    OK = "ok"            # 正常结束，text 为最终回答
    STOPPED = "stopped"  # 命中终止条件（token/轮数/连续失败），text 为提示
    ERROR = "error"      # API 等错误，text 为错误信息


class StopReason(str, Enum):
    """STOPPED 的具体原因，供界面做差异化提示。"""
    TOKEN_BUDGET = "token_budget"
    MAX_ROUNDS = "max_rounds"
    CONSECUTIVE_FAILURES = "consecutive_failures"


@dataclass
class RunResult:
    """AgentLoop.run 的结构化返回：用枚举替代字符串前缀区分结果类型。"""
    status: RunStatus
    text: str
    reason: Optional[StopReason] = None  # 仅 status == STOPPED 时有值


class AgentLoop:
    def __init__(
        self,
        config: Config,
        client: LLMClient,
        history: MessageHistory,
        on_text: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[[str, dict], None]] = None,
        on_tool_result: Optional[Callable[[str, ToolResult], None]] = None,
        on_status: Optional[Callable[[str, int], None]] = None,
        trace: Optional[TraceLogger] = None,
    ):
        self._config = config
        self._client = client
        self._history = history
        # 回调：流式文本 / 工具调用开始 / 工具执行结束(含结果对象) / 状态变化（供 CLI 展示，可全部留空）
        self._on_text = on_text
        self._on_tool_call = on_tool_call
        self._on_tool_result = on_tool_result
        # on_status(event, round_no)：目前仅 "waiting_model"（每次请求模型前触发）
        self._on_status = on_status
        # 运行轨迹日志（可选）：传入时记录每轮工具调用/结果/用量与终止原因
        self._trace = trace

        self.total_tokens_used = 0  # 累计 token 用量（按 API 返回的 usage 统计）

    def run(self, user_input: str) -> RunResult:
        """执行一轮用户任务，返回结构化的 RunResult（status + 文本 + 停止原因）。"""
        self._history.add_user(user_input)
        if self._trace:
            self._trace.log_task(user_input)
        schemas = tool_schemas()
        last_failure_signature: Optional[str] = None
        consecutive_failures = 0

        for round_no in range(1, self._config.max_rounds + 1):
            # 终止条件 3：token 预算
            if self.total_tokens_used >= self._config.token_budget:
                self._log_termination("token 预算耗尽", round_no)
                return RunResult(
                    RunStatus.STOPPED,
                    f"累计 token 用量达到预算上限（{self._config.token_budget}）。",
                    StopReason.TOKEN_BUDGET,
                )

            self._history.prune()

            if self._on_status:
                self._on_status("waiting_model", round_no)

            try:
                response = self._client.chat(
                    self._history.messages, tools=schemas, on_text=self._on_text
                )
            except RuntimeError as e:
                self._log_termination("API 错误", round_no)
                return RunResult(RunStatus.ERROR, str(e))

            usage = response.get("usage")
            if usage:
                self.total_tokens_used += usage["total_tokens"]

            tool_calls = response.get("tool_calls")
            self._history.add_assistant(response.get("content"), tool_calls)

            # 终止条件 1：无工具调用，模型给出最终回答
            if not tool_calls:
                self._log_round(round_no, usage, None, None)
                self._log_termination("正常结束", round_no)
                return RunResult(RunStatus.OK, response.get("content") or "(模型未返回内容)")

            # 先按模型返回顺序通知 CLI 本轮将执行的工具（状态由真实回调驱动）
            for tool_call in tool_calls:
                name = tool_call["function"]["name"]
                if self._on_tool_call:
                    self._on_tool_call(name, tool_call)

            # 并行执行：同一轮内工具调用相互独立（无程序内数据依赖），并发执行
            # 可显著缩短多工具轮次耗时；结果仍按原顺序回填，保证 tool 消息与
            # tool_call id 一一对应。危险命令的审批在 permissions.ask_approval
            # 内用锁串行化，不会被并发绕过。
            results = self._execute_tool_calls(tool_calls)

            for tool_call, result in zip(tool_calls, results):
                name = tool_call["function"]["name"]
                if self._on_tool_result:
                    self._on_tool_result(name, result)

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
                        self._log_round(round_no, usage, tool_calls, results)
                        self._log_termination("连续失败卡死", round_no)
                        return RunResult(
                            RunStatus.STOPPED,
                            f"工具 {name} 以相同参数连续失败 "
                            f"{MAX_CONSECUTIVE_SAME_FAILURES} 次，模型疑似陷入循环。"
                            f"最后错误：{result.error}",
                            StopReason.CONSECUTIVE_FAILURES,
                        )

            # 本轮工具执行完毕，记录一轮轨迹
            self._log_round(round_no, usage, tool_calls, results)

        # 终止条件 2：轮数耗尽
        self._log_termination("最大轮数", self._config.max_rounds)
        return RunResult(
            RunStatus.STOPPED,
            f"单任务已达到最大轮数（{self._config.max_rounds}）。"
            "可通过 /reset 清空上下文后重试，或调大 MAX_ROUNDS。",
            StopReason.MAX_ROUNDS,
        )

    def _log_round(
        self,
        round_no: int,
        usage: Optional[dict],
        tool_calls: Optional[list[dict]],
        tool_results: Optional[list[ToolResult]],
    ) -> None:
        if self._trace:
            self._trace.log_round(round_no, usage, tool_calls, tool_results, self.total_tokens_used)

    def _log_termination(self, reason: str, round_no: int) -> None:
        if self._trace:
            self._trace.log_termination(reason, round_no, self.total_tokens_used)

    @staticmethod
    def _execute_tool_calls(tool_calls: list[dict]) -> list[ToolResult]:
        """并行执行一批工具调用，按输入顺序返回结果。

        单条工具调用直接串行执行，避免线程池开销；多条时用线程池并发，
        并发数受 MAX_PARALLEL_TOOL_CALLS 约束。execute_tool_call 内部已把
        所有异常转为结构化错误，这里再做一层兜底，确保线程异常不向上冒。
        """
        if len(tool_calls) == 1:
            return [execute_tool_call(tool_calls[0])]

        max_workers = min(MAX_PARALLEL_TOOL_CALLS, len(tool_calls))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(execute_tool_call, tc) for tc in tool_calls]
            results: list[ToolResult] = []
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as e:  # 兜底：正常情况下 execute_tool_call 不抛异常
                    results.append(ToolResult(ok=False, error=f"{type(e).__name__}: {e}"))
        return results

    @staticmethod
    def _normalize_arguments(tool_call: dict) -> str:
        """把工具参数序列化为稳定字符串，用于识别'相同参数'。"""
        raw = tool_call["function"].get("arguments", "")
        try:
            return json.dumps(json.loads(raw), sort_keys=True, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            return raw
