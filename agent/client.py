"""LLM 调用封装：基于 openai 库的 OpenAI 兼容接口。

自行实现的部分：
- 指数退避重试（网络错误 / 限流 / 5xx）
- 流式输出的增量解析：文本逐 token 回调，tool_calls 分片累积拼装
"""
from __future__ import annotations

import time
from typing import Callable, Optional

import openai

from .config import Config

MAX_RETRIES = 3


class LLMClient:
    """对 chat.completions 的最小封装，返回统一的消息字典。

    返回结构：
        {
            "content": str | None,          # 文本回答
            "tool_calls": [ {...} ] | None, # OpenAI 格式的 tool_call 列表
            "finish_reason": str,
            "usage": {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int} | None,
        }
    """

    def __init__(self, config: Config):
        self._client = openai.OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=120,
        )
        self._model = config.model_name

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        on_text: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """发起一次对话请求。on_text 提供时启用流式输出并逐段回调。

        should_stop 提供时，流式接收过程中每到一个分片会询问一次；返回 True
        则提前结束本次请求（用于用户中断），此时返回部分内容。
        """
        kwargs: dict = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            # 让最后一个 chunk 携带 usage，便于统计 token 用量
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools

        last_error: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                stream = self._client.chat.completions.create(**kwargs)
                return self._consume_stream(stream, on_text, should_stop)
            except (openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError) as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)  # 指数退避：1s, 2s
            except openai.APIStatusError as e:
                # 4xx 属于请求本身问题，重试无意义
                raise RuntimeError(f"模型 API 返回错误（{e.status_code}）：{e.message}") from e
        raise RuntimeError(f"模型 API 请求失败，已重试 {MAX_RETRIES} 次：{last_error}") from last_error

    @staticmethod
    def _consume_stream(
        stream,
        on_text: Optional[Callable[[str], None]],
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """解析流式响应：文本增量回调；tool_calls 按 index 分片累积拼装。

        should_stop 提供且返回 True 时提前中断流式接收（用户中断）。
        """
        content_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        finish_reason: Optional[str] = None
        usage: Optional[dict] = None

        for chunk in stream:
            if should_stop is not None and should_stop():
                break
            if chunk.usage is not None:
                usage = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "total_tokens": chunk.usage.total_tokens,
                }
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta

            if delta.content:
                content_parts.append(delta.content)
                if on_text:
                    on_text(delta.content)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    slot = tool_calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] += tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments

        result_tool_calls = None
        if tool_calls:
            result_tool_calls = [
                {
                    "id": slot["id"],
                    "type": "function",
                    "function": {"name": slot["name"], "arguments": slot["arguments"]},
                }
                for _, slot in sorted(tool_calls.items())
            ]

        return {
            "content": "".join(content_parts) or None,
            "tool_calls": result_tool_calls,
            "finish_reason": finish_reason or "stop",
            "usage": usage,
        }
