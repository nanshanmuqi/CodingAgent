"""上下文管理的单元测试：消息维护、token 估算、裁剪压缩。"""
from agent.context import MessageHistory, estimate_tokens


def make_history(max_tokens: int = 1000) -> MessageHistory:
    return MessageHistory(max_context_tokens=max_tokens)


def test_system_prompt_present():
    history = make_history()
    assert history.messages[0]["role"] == "system"
    assert history.messages[0]["content"]


def test_system_prompt_includes_model_identity():
    """传入模型名时 system prompt 应包含身份说明，避免模型沿训练数据自称 Claude 等。"""
    history = MessageHistory(max_context_tokens=1000, model_name="deepseek-chat")
    content = history.messages[0]["content"]
    assert "deepseek-chat" in content and "身份说明" in content
    # 不传模型名时保持原样，不附带身份说明
    assert "身份说明" not in make_history().messages[0]["content"]


def test_add_messages_and_reset():
    history = make_history()
    history.add_user("任务一")
    history.add_assistant("回答一")
    history.add_tool_result("call_1", "工具输出")
    assert [m["role"] for m in history.messages] == ["system", "user", "assistant", "tool"]

    history.reset()
    assert len(history.messages) == 1  # 只剩 system


def test_estimate_tokens_positive():
    assert estimate_tokens("hello world, 你好") > 0


def test_no_prune_under_threshold():
    history = make_history(max_tokens=10**9)
    for i in range(30):
        history.add_user(f"u{i}")
        history.add_tool_result(f"c{i}", "x" * 2000)
    snapshot = [m.get("content") for m in history.messages]
    history.prune()
    assert [m.get("content") for m in history.messages] == snapshot  # 未做任何改动


def test_prune_compresses_old_tool_outputs():
    history = make_history(max_tokens=1000)
    # 构造较早的超长工具输出
    for i in range(10):
        history.add_user(f"u{i}")
        history.add_assistant(None, [{"id": f"c{i}", "type": "function",
                                      "function": {"name": "read_file", "arguments": "{}"}}])
        history.add_tool_result(f"c{i}", f"文件 f{i}.py 内容\n" + "y" * 2000)
    # 再加最近几条，使前面的工具输出落在"最近窗口"之外
    for i in range(12):
        history.add_user(f"recent{i}")

    history.prune()

    contents = [str(m.get("content", "")) for m in history.messages]
    # 较早的超长输出被压缩为摘要
    assert any("已省略" in c for c in contents)
    # system 始终保留
    assert history.messages[0]["role"] == "system"


def test_prune_drops_oldest_units_without_orphan_tool():
    history = make_history(max_tokens=500)
    for i in range(40):
        history.add_user(f"u{i} " + "z" * 100)
        history.add_assistant("a" * 100,
                              [{"id": f"c{i}", "type": "function",
                                "function": {"name": "grep", "arguments": "{}"}}])
        history.add_tool_result(f"c{i}", "r" * 100)

    before = history.total_tokens()
    history.prune()

    assert history.messages[0]["role"] == "system"
    # 裁剪显著降低了总量（设计上有意保留最近 KEEP_RECENT_MESSAGES 条，不保证严格低于阈值）
    assert history.total_tokens() < before
    assert len(history.messages) <= 1 + 10  # system + 最近窗口
    # 不得出现孤立的 tool 消息（它前面的 assistant 被删了）
    for i, message in enumerate(history.messages):
        if message["role"] == "tool":
            assert any(m.get("tool_calls") for m in history.messages[:i])


def test_prune_keeps_recent_messages():
    history = make_history(max_tokens=300)
    for i in range(20):
        history.add_user(f"u{i} " + "q" * 80)
    history.prune()
    # 最近的用户消息仍在
    contents = [str(m.get("content", "")) for m in history.messages]
    assert any("u19" in c for c in contents)
