"""运行轨迹日志（JSONL）：记录每轮工具调用、结果、token 用量与终止原因，用于回看/审计。

一个日志文件对应一次会话，按时间顺序写入三类记录：
- task        任务开始（含用户输入）
- round       每一轮：本轮 API 用量、模型发起的工具调用、工具执行结果、累计 token
- termination 任务结束及终止原因

每行一个 JSON 对象（JSONL），可直接用文本工具或 `jq`/脚本按行解析回放。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .tools import ToolResult


class TraceLogger:
    """把 agent 运行轨迹写成 JSONL，一行一条记录，便于回看与审计。"""

    def __init__(self, log_dir: Path):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._path = self._log_dir / f"agent-trace-{stamp}.jsonl"
        self._file = self._path.open("a", encoding="utf-8")

    @property
    def path(self) -> Path:
        return self._path

    def log_task(self, user_input: str) -> None:
        self._write({"ts": self._now(), "event": "task", "input": user_input})

    def log_round(
        self,
        round_no: int,
        usage: Optional[dict],
        tool_calls: Optional[list[dict]],
        tool_results: Optional[list[ToolResult]],
        total_tokens_used: int,
    ) -> None:
        calls = tool_calls or []
        results = tool_results or []
        serialized_calls: list[dict] = []
        serialized_results: list[dict] = []
        for i, tc in enumerate(calls):
            fn = tc.get("function") or {}
            name = fn.get("name")
            call_id = tc.get("id")
            serialized_calls.append({
                "id": call_id,
                "name": name,
                "arguments": self._parse_arguments(fn.get("arguments")),
            })
            if i < len(results):
                r = results[i]
                serialized_results.append({
                    "id": call_id,
                    "name": name,
                    "ok": r.ok,
                    "output": r.output,
                    "error": r.error,
                })
        self._write({
            "ts": self._now(),
            "event": "round",
            "round": round_no,
            "usage": usage,
            "total_tokens_used": total_tokens_used,
            "tool_calls": serialized_calls,
            "tool_results": serialized_results,
        })

    def log_termination(self, reason: str, round_no: int, total_tokens_used: int) -> None:
        self._write({
            "ts": self._now(),
            "event": "termination",
            "reason": reason,
            "round": round_no,
            "total_tokens_used": total_tokens_used,
        })

    def close(self) -> None:
        self._file.close()

    # ---- 内部 ----

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _parse_arguments(raw: Any) -> Any:
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return raw
        return raw

    def _write(self, record: dict[str, Any]) -> None:
        self._file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._file.flush()
