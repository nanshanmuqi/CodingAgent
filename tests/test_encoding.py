"""编码与环境适配的单元测试：统一解码、子进程环境、标准流配置。"""
import os

from agent.encoding import child_env, decode_bytes, read_text, setup_stdio


def test_decode_utf8_priority():
    assert decode_bytes("中文 UTF-8".encode("utf-8")) == "中文 UTF-8"


def test_decode_fallback_to_local_codepage():
    # GBK 字节不是合法 UTF-8，应回退到指定兜底编码正确解码
    assert decode_bytes("中文 GBK".encode("gbk"), fallback="gbk") == "中文 GBK"


def test_decode_never_raises():
    # 任意坏字节序列都不应抛异常，坏段以替换字符呈现
    assert isinstance(decode_bytes(b"\xff\xfe\x00invalid\x81"), str)


def test_read_text_roundtrip_and_fallback(tmp_path):
    utf8_file = tmp_path / "a.txt"
    utf8_file.write_bytes("你好 UTF-8".encode("utf-8"))
    assert read_text(utf8_file) == "你好 UTF-8"

    gbk_file = tmp_path / "b.txt"
    gbk_file.write_bytes("你好 GBK".encode("gbk"))
    assert "你好" in read_text(gbk_file)  # 本地代码页兜底（Windows 中文环境为 GBK）


def test_child_env_injects_utf8_switches():
    env = child_env()
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert os.environ.get("PYTHONUTF8") != "1" or env is not os.environ  # 不污染当前进程


def test_setup_stdio_idempotent():
    setup_stdio()
    setup_stdio()  # 重复调用安全（pytest 替身流无 reconfigure 时静默跳过）
