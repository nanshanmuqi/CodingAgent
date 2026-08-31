"""CLI 三明治布局的单元测试：折叠摘要、分阶段过程日志、尾部结论块。"""
import agent.cli as cli
from agent.tools.base import ToolResult


def _reset():
    """重置 CLI 输出层状态，避免测试间互相污染。"""
    cli._status_stop()
    cli._round_no = 0
    cli._round_text = ""
    cli._stage_printed = False
    cli._verbose = True
    cli._tool_counts = {}
    cli._tool_failures = 0


def make_tool_call(name: str, arguments: str) -> dict:
    return {"function": {"name": name, "arguments": arguments}}


def test_stage_header_printed_once_with_model_title(capsys):
    """阶段分隔每轮只打印一次，名称取模型该轮的一句话说明。"""
    _reset()
    cli._on_status("waiting_model", 2)
    cli._on_text("先读取数据文件。")
    cli._on_tool_call("read_file", make_tool_call("read_file", '{"path": "a.txt"}'))
    cli._on_tool_result("read_file", ToolResult(ok=True, summary="读取 a.txt（共 3 行）"))
    cli._on_tool_call("grep", make_tool_call("grep", '{"pattern": "x"}'))  # 同轮第二个工具
    out = capsys.readouterr().out
    assert out.count("══ 阶段 2：先读取数据文件。 ══") == 1
    assert "  [RESULT] √ 读取 a.txt（共 3 行）" in out


def test_stage_header_fallback_title(capsys):
    """模型该轮未输出文字时，阶段名称退化为"工具执行"。"""
    _reset()
    cli._on_status("waiting_model", 1)
    cli._on_tool_call("glob", make_tool_call("glob", '{"pattern": "*.py"}'))
    assert "══ 阶段 1：工具执行 ══" in capsys.readouterr().out


def test_tool_result_failure_shows_error_brief(capsys):
    """[RESULT] 失败行附错误摘要（仅首行，截断）。"""
    _reset()
    cli._on_tool_result("run_command", ToolResult(ok=False, error="命令执行超过 60 秒\n详细堆栈…"))
    out = capsys.readouterr().out
    assert "  [RESULT] × run_command：命令执行超过 60 秒" in out
    assert "详细堆栈" not in out


def test_verbose_off_suppresses_log_and_counts(capsys):
    """非 verbose：工具过程不落永久行，仅累积折叠摘要统计。"""
    _reset()
    cli._verbose = False
    cli._on_status("waiting_model", 1)
    cli._on_tool_call("read_file", make_tool_call("read_file", '{"path": "a.txt"}'))
    cli._on_tool_result("read_file", ToolResult(ok=True, summary="读取 a.txt（共 3 行）"))
    cli._status_stop()
    out = capsys.readouterr().out
    assert "[RESULT]" not in out
    assert "══ 阶段" not in out
    assert cli._tool_counts == {"read_file": 1}
    assert cli._tool_failures == 0


def test_run_summary_collapses_tool_counts(capsys):
    """折叠摘要：一行汇总轮次、工具调用次数，失败会标注。"""
    _reset()
    cli._round_no = 3
    cli._tool_counts = {"read_file": 2, "run_command": 1}
    cli._tool_failures = 1
    cli._print_run_summary()
    out = capsys.readouterr().out
    assert "共 3 轮" in out
    assert "read_file ×2" in out
    assert "run_command ×1" in out
    assert "失败 1 次" in out


def test_tail_renders_body_and_prompts(capsys):
    """尾部：结论以 Markdown 面板渲染，[PROMPT] 行单独列出。"""
    _reset()
    answer = "距离矩阵如下：\n\n| 从 | 到 | 距离 |\n|--|--|--|\n| A | B | 5 |\n\n[PROMPT] 把矩阵可视化\n[PROMPT] 增加负权边重算"
    cli._print_tail(answer)
    out = capsys.readouterr().out
    assert "结论" in out                      # 面板标题
    assert "距离矩阵如下" in out               # 正文进入面板
    assert "从" in out and "到" in out        # 表格内容被渲染
    assert "[PROMPT] 把矩阵可视化" in out      # 建议单独列出
    assert "[PROMPT] 增加负权边重算" in out
    assert ">>>" not in out                    # 不再使用逐行 >>> 前缀


def test_tail_extracts_bullet_prompts_and_strips_header(capsys):
    """[PROMPT] 以列表形式（- [PROMPT]）出现也能提取，模型自带小标题被跳过。"""
    _reset()
    answer = "结果如下：\n\n下一步建议：\n- [PROMPT] 把矩阵可视化\n- [PROMPT] 增加负权边重算"
    cli._print_tail(answer)
    out = capsys.readouterr().out
    assert "结果如下" in out
    assert "[PROMPT] 把矩阵可视化" in out
    assert "[PROMPT] 增加负权边重算" in out
    assert "下一步建议：" not in out            # 小标题不残留


def test_print_tagged_skips_blank_lines(capsys):
    """元信息：空行只作视觉分隔不加标签，内容行逐行带全角标签。"""
    _reset()
    cli._print_tagged(cli.TAG_SYS, "\n第一\n\n第二", style="yellow")
    assert capsys.readouterr().out.splitlines() == ["", "【系统】 第一", "", "【系统】 第二"]
