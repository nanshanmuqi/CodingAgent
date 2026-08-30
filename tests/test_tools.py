"""工具系统的单元测试：文件读写/编辑、grep/glob、路径防护、命令分级。"""
import pytest

from agent import permissions
from agent.permissions import classify_command, resolve_in_workspace
from agent.tools import execute_tool_call, tool_schemas
from agent.tools.file_tools import _edit_file, _read_file, _write_file, write_file_tool
from agent.tools.search import _glob, _grep
from agent.tools.shell import _run_command


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """把工具的工作目录隔离到临时目录，避免污染项目。"""
    monkeypatch.setattr(permissions, "WORKSPACE", tmp_path)
    return tmp_path


def make_tool_call(name: str, arguments: str) -> dict:
    return {"id": "call_1", "type": "function",
            "function": {"name": name, "arguments": arguments}}


# ---- 文件工具 ----

def test_write_and_read_file(workspace):
    result = _write_file("src/hello.py", "print('hi')\n")
    assert result.ok
    # 生成文件统一落到 out/ 目录，自动创建父目录
    assert (workspace / "out" / "src" / "hello.py").exists()

    result = _read_file("src/hello.py")
    assert result.ok and "print('hi')" in result.output


def test_write_file_keeps_out_prefix(workspace):
    _write_file("out/x.py", "x = 1\n")
    assert (workspace / "out" / "x.py").exists()
    assert not (workspace / "out" / "out" / "x.py").exists()


def test_read_file_truncation(workspace):
    (workspace / "big.txt").write_text("\n".join(f"line{i}" for i in range(1000)))
    result = _read_file("big.txt", limit=50)
    assert result.ok
    assert "已截断" in result.output and "共 1000 行" in result.output


def test_read_missing_file(workspace):
    assert not _read_file("nope.py").ok


def test_edit_file_unique(workspace):
    _write_file("a.py", "x = 1\ny = 2\n")
    result = _edit_file("a.py", "x = 1", "x = 10")
    assert result.ok
    assert (workspace / "out" / "a.py").read_text().startswith("x = 10")


def test_edit_file_not_found_and_ambiguous(workspace):
    _write_file("b.py", "foo\nfoo\n")
    assert not _edit_file("b.py", "bar", "baz").ok          # 找不到
    assert not _edit_file("b.py", "foo", "baz").ok          # 多处匹配不唯一
    assert _edit_file("b.py", "foo", "baz", replace_all=True).ok  # 显式全量替换


def test_path_escape_blocked(workspace):
    # 经 Tool.run 分发时，PermissionError 被转换为结构化错误结果（进程不崩溃）
    result = write_file_tool.run({"path": "../evil.txt", "content": "x"})
    assert not result.ok and "越出工作目录" in result.error
    assert not (workspace.parent / "evil.txt").exists()
    # 底层防护函数本身抛出 PermissionError
    with pytest.raises(PermissionError):
        resolve_in_workspace("../../outside")


# ---- 搜索工具 ----

def test_grep(workspace):
    _write_file("main.py", "def main():\n    pass\n")
    _write_file("note.txt", "main 函数说明\n")
    result = _grep(r"def main", include="*.py")
    assert result.ok and "main.py:1" in result.output
    assert "note.txt" not in result.output  # include 过滤生效


def test_grep_invalid_regex(workspace):
    assert not _grep("[").ok


def test_glob(workspace):
    _write_file("pkg/mod/a.py", "")
    _write_file("pkg/b.txt", "")
    result = _glob("**/*.py")
    assert result.ok and "a.py" in result.output and "b.txt" not in result.output


# ---- shell 工具 ----

def test_run_command_success(workspace):
    result = _run_command("echo hello-agent")
    assert result.ok and "hello-agent" in result.output and "退出码 0" in result.output


def test_run_command_failure_exit_code(workspace):
    result = _run_command("exit 1")
    assert not result.ok and "退出码 1" in result.error


def test_run_command_forbidden(workspace):
    result = _run_command("shutdown /s")
    assert not result.ok and "已拒绝" in result.error


def test_run_command_dangerous_requires_approval(workspace, monkeypatch):
    monkeypatch.setattr("agent.tools.shell.ask_approval", lambda cmd: False)
    result = _run_command("del something.txt")
    assert not result.ok and "拒绝" in result.error


def test_classify_command():
    assert classify_command("dir") == "safe"
    assert classify_command("python test.py") == "safe"
    assert classify_command("del a.txt") == "dangerous"
    assert classify_command("shutdown /s") == "forbidden"
    assert classify_command("rm -rf /") == "forbidden"


# ---- 注册表与 tool_call 解析 ----

def test_tool_schemas_cover_six_tools():
    names = {s["function"]["name"] for s in tool_schemas()}
    assert names == {"read_file", "write_file", "edit_file", "run_command", "grep", "glob"}


def test_execute_unknown_tool():
    result = execute_tool_call(make_tool_call("hack", "{}"))
    assert not result.ok and "未知工具" in result.error


def test_execute_invalid_json_arguments():
    result = execute_tool_call(make_tool_call("read_file", "{not json"))
    assert not result.ok and "JSON 解析失败" in result.error


def test_execute_tool_call_dispatch(workspace):
    _write_file("x.txt", "abc")
    result = execute_tool_call(make_tool_call("read_file", '{"path": "x.txt"}'))
    assert result.ok and "abc" in result.output
