"""CLI 入口：加载配置并启动 Textual 全屏 TUI（agent.app.AgentApp）。

启动方式：
  python -m agent.cli                      默认唤起独立新窗口运行
  python -m agent.cli --inline             在当前终端运行
  python -m agent.cli --continue           恢复最近一次会话
  python -m agent.cli --resume <会话id>     恢复指定会话
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .app import AgentApp
from .config import load_config
from .encoding import child_env, setup_stdio
from .session import SessionStore
from .trace import TraceLogger


def _relaunch_in_new_window() -> None:
    """以独立控制台窗口重新启动本 CLI（Windows 的 CREATE_NEW_CONSOLE）。"""
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)  # 非 Windows 平台退化为普通子进程
    # 透传 --continue/--resume 等参数，仅去掉 --inline 避免重复
    passthrough = [a for a in sys.argv[1:] if a != "--inline"]
    subprocess.Popen(
        [sys.executable, "-m", "agent.cli", "--inline", *passthrough],
        cwd=os.getcwd(),  # 继承当前工作目录，新窗口内 agent 的工作目录不变
        env=child_env(),  # 新窗口内的 Python 同样使用 UTF-8，避免中文乱码
        creationflags=creationflags,
    )


def main() -> None:
    setup_stdio()  # 统一终端编码为 UTF-8：重定向到管道/文件时中文与符号不乱码
    # 默认在独立新窗口运行（Textual 需要真实控制台渲染）；--inline 则在当前终端运行。
    # 非 Windows 平台没有 CREATE_NEW_CONSOLE，直接在当前终端运行。
    if "--inline" not in sys.argv and getattr(subprocess, "CREATE_NEW_CONSOLE", 0):
        _relaunch_in_new_window()
        return

    config = load_config()
    store = SessionStore()

    # 解析 --continue/--resume：恢复已有会话
    resume_id = None
    if "--resume" in sys.argv:
        i = sys.argv.index("--resume")
        if i + 1 < len(sys.argv):
            resume_id = sys.argv[i + 1]
    elif "--continue" in sys.argv:
        resume_id = store.latest_id()

    session_id = None
    messages = None
    if resume_id:
        data = store.load(resume_id)
        if data:
            session_id = resume_id
            messages = data.get("messages")

    trace = TraceLogger(Path("logs"))
    try:
        AgentApp(config, trace, session_id=session_id, messages=messages).run()
    finally:
        trace.close()


if __name__ == "__main__":
    main()
