"""CLI 三明治布局的单元测试：折叠摘要、分阶段过程日志、尾部结论块。"""
import pytest

import agent.cli as cli
from agent.tools.base import ToolResult


@pytest.fixture
def renderer():
    """新建渲染器并默认开启完整过程输出（等价旧 _reset() 的 verbose=True）。"""
    r = cli.UIRenderer()
    r.toggle_verbose()
    return r


def make_tool_call(name: str, arguments: str) -> dict:
    return {"function": {"name": name, "arguments": arguments}}


def test_stage_header_printed_once_with_model_title(renderer, capsys):
    """阶段分隔每轮只打印一次，名称取模型该轮的一句话说明。"""
    renderer.on_status("waiting_model", 2)
    renderer.on_text("先读取数据文件。")
    renderer.on_tool_call("read_file", make_tool_call("read_file", '{"path": "a.txt"}'))
    renderer.on_tool_result("read_file", ToolResult(ok=True, summary="读取 a.txt（共 3 行）"))
    renderer.on_tool_call("grep", make_tool_call("grep", '{"pattern": "x"}'))  # 同轮第二个工具
    out = capsys.readouterr().out
    assert out.count("══ Stage 2：先读取数据文件。 ══") == 1
    assert "  √ 读取 a.txt（共 3 行）" in out


def test_stage_header_fallback_title(renderer, capsys):
    """模型该轮未输出文字时，阶段名称退化为"工具执行"。"""
    renderer.on_status("waiting_model", 1)
    renderer.on_tool_call("glob", make_tool_call("glob", '{"pattern": "*.py"}'))
    assert "══ Stage 1：工具执行 ══" in capsys.readouterr().out


def test_tool_result_failure_shows_error_brief(renderer, capsys):
    """失败行附错误摘要（仅首行，截断）。"""
    renderer.on_tool_result("run_command", ToolResult(ok=False, error="命令执行超过 60 秒\n详细堆栈…"))
    out = capsys.readouterr().out
    assert "  × run_command：命令执行超过 60 秒" in out
    assert "详细堆栈" not in out


def test_verbose_off_shows_tool_log_but_folds_result_detail(renderer, capsys):
    """默认模式：打印可观测日志（Stage + 工具名参数），但折叠 √/× 结果明细。"""
    renderer.toggle_verbose()  # 关闭完整过程
    renderer.on_status("waiting_model", 1)
    renderer.on_tool_call("read_file", make_tool_call("read_file", '{"path": "a.txt"}'))
    renderer.on_tool_result("read_file", ToolResult(ok=True, summary="读取 a.txt（共 3 行）"))
    renderer.status_stop()
    out = capsys.readouterr().out
    assert "> read_file a.txt" in out       # 工具调用可观测日志
    assert "══ Stage" in out                 # 阶段标题默认可见
    assert "√" not in out                    # 结果明细仍折叠
    assert "读取 a.txt（共 3 行）" not in out  # summary 不落行
    assert renderer.tool_counts == {"read_file": 1}
    assert renderer.tool_failures == 0


def test_run_summary_collapses_tool_counts(renderer, capsys):
    """折叠摘要：一行汇总轮次、工具调用次数，失败会标注。"""
    renderer._round_no = 3
    renderer._tool_counts = {"read_file": 2, "run_command": 1}
    renderer._tool_failures = 1
    renderer.print_run_summary()
    out = capsys.readouterr().out
    assert "共 3 轮" in out
    assert "read_file ×2" in out
    assert "run_command ×1" in out
    assert "失败 1 次" in out


def test_tail_renders_body_and_prompts(renderer, capsys):
    """尾部：结论以 Markdown 面板渲染，建议以 Next steps: 单独列出。"""
    answer = "距离矩阵如下：\n\n| 从 | 到 | 距离 |\n|--|--|--|\n| A | B | 5 |\n\n[PROMPT] 把矩阵可视化\n[PROMPT] 增加负权边重算"
    renderer.print_tail(answer)
    out = capsys.readouterr().out
    assert "Agent >" in out                  # Agent 前缀
    assert "距离矩阵如下" in out               # 正文进入面板
    assert "从" in out and "到" in out        # 表格内容被渲染
    assert "Next steps:" in out               # 建议区标题只出现一次
    assert "把矩阵可视化" in out
    assert "增加负权边重算" in out
    assert ">>>" not in out                    # 不再使用逐行 >>> 前缀


def test_tail_extracts_bullet_prompts_and_strips_header(renderer, capsys):
    """[PROMPT] 以列表形式（- [PROMPT]）出现也能提取，模型自带小标题被跳过。"""
    answer = "结果如下：\n\n下一步建议：\n- [PROMPT] 把矩阵可视化\n- [PROMPT] 增加负权边重算"
    renderer.print_tail(answer)
    out = capsys.readouterr().out
    assert "结果如下" in out
    assert "Next steps:" in out
    assert "把矩阵可视化" in out
    assert "增加负权边重算" in out
    assert "下一步建议：" not in out            # 小标题不残留


def test_print_tagged_skips_blank_lines(capsys):
    """元信息：空行只作视觉分隔不加标签，内容行逐行带英文标签。"""
    cli._print_tagged(cli.TAG_INFO, "\n第一\n\n第二", style="yellow")
    assert capsys.readouterr().out.splitlines() == ["", "[INFO] 第一", "", "[INFO] 第二"]
