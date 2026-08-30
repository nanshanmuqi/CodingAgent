"""主循环的单元测试：用 FakeClient 模拟模型响应，不依赖真实 API。"""
import json

import pytest

from agent import permissions
from agent.config import Config
from agent.context import MessageHistory
from agent.loop import AgentLoop


class FakeClient:
    """按脚本依次返回响应的伪 LLM 客户端。"""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    def chat(self, messages, tools=None, on_text=None):
        self.calls += 1
        self.seen_messages.append([dict(m) for m in messages])
        assert tools, "主循环应始终携带工具 schema"
        response = self._responses.pop(0)
        if on_text and response.get("content"):
            on_text(response["content"])
        return response


def text_response(content: str, tokens: int = 10) -> dict:
    return {"content": content, "tool_calls": None, "finish_reason": "stop",
            "usage": {"prompt_tokens": tokens, "completion_tokens": 0, "total_tokens": tokens}}


def tool_call_response(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "content": None,
        "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
        "finish_reason": "tool_calls",
        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
    }


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions, "WORKSPACE", tmp_path)
    return Config(api_key="k", base_url="http://x", model_name="m",
                  max_rounds=5, token_budget=1000)


def make_agent(config, responses):
    history = MessageHistory(max_context_tokens=10**9)
    return AgentLoop(config, FakeClient(responses), history), history


def test_simple_text_answer(config):
    agent, history = make_agent(config, [text_response("这是回答")])
    assert agent.run("你好") == "这是回答"
    assert agent.total_tokens_used == 10
    assert [m["role"] for m in history.messages] == ["system", "user", "assistant"]


def test_tool_call_then_answer(config):
    """第一轮模型要求调工具，第二轮给出最终回答。"""
    agent, history = make_agent(config, [
        tool_call_response("write_file", {"path": "a.txt", "content": "hi"}),
        text_response("已创建 a.txt"),
    ])
    assert agent.run("创建文件") == "已创建 a.txt"
    assert agent._client.calls == 2

    roles = [m["role"] for m in history.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    # 第二次调用模型时，历史里应包含工具执行结果
    second_call_roles = [m["role"] for m in agent._client.seen_messages[1]]
    assert "tool" in second_call_roles


def test_status_callback_reports_real_state(config):
    """on_status 在每次请求模型前触发一次，事件与轮次均来自主循环实际状态。"""
    events: list[tuple[str, int]] = []
    agent, _ = make_agent(config, [
        tool_call_response("write_file", {"path": "a.txt", "content": "hi"}),
        text_response("完成"),
    ])
    agent._on_status = lambda event, round_no: events.append((event, round_no))
    assert agent.run("任务") == "完成"
    # 两次调用模型 → 两次 waiting_model，轮次递增
    assert events == [("waiting_model", 1), ("waiting_model", 2)]


def test_max_rounds_termination(config):
    responses = [tool_call_response("grep", {"pattern": "x"}, call_id=f"c{i}") for i in range(10)]
    agent, _ = make_agent(config, responses)
    answer = agent.run("永远找下去")
    assert "最大轮数" in answer
    assert agent._client.calls == config.max_rounds


def test_token_budget_termination(config):
    config.token_budget = 10  # 第一轮即消耗 10 tokens，第二轮开始前触发停止
    agent, _ = make_agent(config, [tool_call_response("grep", {"pattern": "x"}),
                                   text_response("不会到达")])
    answer = agent.run("任务")
    assert "token" in answer and "预算" in answer
    assert agent._client.calls == 1  # 第二次调用未发生


def test_stuck_loop_termination(config):
    """同一工具同样参数连续失败 3 次 → 判定卡死。"""
    responses = [
        tool_call_response("read_file", {"path": "不存在.txt"}, call_id=f"c{i}")
        for i in range(5)
    ]
    agent, _ = make_agent(config, responses)
    answer = agent.run("读不存在的文件")
    assert "连续失败" in answer


def test_failure_counter_resets_after_success(config):
    """失败后成功一次，计数器应清零，不会误判卡死。"""
    responses = [
        tool_call_response("read_file", {"path": "a.txt"}, call_id="c1"),  # 失败
        tool_call_response("write_file", {"path": "a.txt", "content": "x"}, call_id="c2"),  # 成功
        tool_call_response("read_file", {"path": "b.txt"}, call_id="c3"),  # 失败
        text_response("完成"),
    ]
    agent, _ = make_agent(config, responses)
    assert agent.run("任务") == "完成"
    assert agent._client.calls == 4


def test_api_error_reported(config):
    class BrokenClient:
        def chat(self, messages, tools=None, on_text=None):
            raise RuntimeError("模型 API 请求失败")

    history = MessageHistory()
    agent = AgentLoop(config, BrokenClient(), history)
    assert "API 错误" in agent.run("任务")
