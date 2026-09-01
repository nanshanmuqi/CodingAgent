"""运行轨迹日志（TraceLogger）的单元与集成测试。"""
import json

from agent import permissions
from agent.config import Config
from agent.context import MessageHistory
from agent.loop import AgentLoop
from agent.trace import TraceLogger


class FakeClient:
    """按脚本依次返回响应的伪 LLM 客户端。"""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)

    def chat(self, messages, tools=None, on_text=None):
        response = self._responses.pop(0)
        if on_text and response.get("content"):
            on_text(response["content"])
        return response


def text_response(content: str, tokens: int = 10) -> dict:
    return {"content": content, "tool_calls": None, "finish_reason": "stop",
            "usage": {"prompt_tokens": tokens, "completion_tokens": 0, "total_tokens": tokens}}


def tool_call_response(name: str, arguments: dict, call_id: str = "c1") -> dict:
    return {
        "content": None,
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)}}],
        "finish_reason": "tool_calls",
        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
    }


def _make_config(tmp_path, monkeypatch) -> Config:
    monkeypatch.setattr(permissions, "WORKSPACE", tmp_path)
    return Config(api_key="k", base_url="http://x", model_name="m",
                  max_rounds=5, token_budget=1000)


def test_trace_logger_writes_jsonl(tmp_path):
    trace = TraceLogger(tmp_path)
    trace.log_task("创建文件")
    trace.log_round(1, {"total_tokens": 10}, None, None, 10)
    trace.log_termination("正常结束", 1, 10)
    trace.close()

    records = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in records] == ["task", "round", "termination"]
    assert records[0]["input"] == "创建文件"
    assert records[1]["round"] == 1 and records[1]["usage"] == {"total_tokens": 10}
    assert records[2]["reason"] == "正常结束" and records[2]["total_tokens_used"] == 10


def test_loop_writes_round_and_termination(tmp_path, monkeypatch):
    trace = TraceLogger(tmp_path)
    history = MessageHistory(max_context_tokens=10**9)
    agent = AgentLoop(
        _make_config(tmp_path, monkeypatch),
        FakeClient([
            tool_call_response("write_file", {"path": "a.txt", "content": "hi"}),
            text_response("完成"),
        ]),
        history,
        trace=trace,
    )

    assert agent.run("创建文件") == "完成"

    records = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in records] == ["task", "round", "round", "termination"]

    round1 = records[1]
    assert round1["tool_calls"][0]["name"] == "write_file"
    assert round1["tool_results"][0]["name"] == "write_file"
    assert round1["tool_results"][0]["ok"] is True

    round2 = records[2]
    assert round2["tool_calls"] == [] and round2["tool_results"] == []

    assert records[3]["reason"] == "正常结束"
    trace.close()
