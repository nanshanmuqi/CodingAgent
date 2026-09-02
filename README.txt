编程智能体（Coding Agent）
==========================

一个独立设计实现的编程智能体：通过与 LLM 交互，自主读写文件、执行命令，
完成用户交付的编程任务。未使用任何 agent 框架/SDK，对话历史管理、工具
定义与本地执行、模型输出解析、循环终止条件、错误处理均为自行实现。

运行环境
--------
- Python 3.10+
- Windows

安装与配置
----------
1. 安装依赖：
       pip install -r requirements.txt

2. 配置模型凭据（不会入库）：
   复制 .env.example 为 .env，填写以下三项：
       API_KEY=你的模型API密钥
       BASE_URL=https://api.deepseek.com
       MODEL_NAME=deepseek-chat
   可选项：MAX_ROUNDS（默认 40）、TOKEN_BUDGET（默认 200000）

启动
----
在项目根目录执行：
    python -m agent.cli            唤起独立的新终端窗口进行交互（默认）
    python -m agent.cli --inline   在当前终端交互
    python -m agent.cli --continue           恢复最近一次会话
    python -m agent.cli --resume <会话id>     恢复指定会话

会话会自动保存到 sessions/ 目录，标题栏显示当前会话 id。

建议用 Windows Terminal 运行（全屏 TUI 在传统 cmd 控制台下渲染可能不稳）。

启动后在底部输入框直接描述编程任务，例如：
    读取当前目录的 main.py，解释它的功能
    写一个快速排序到 sort.py，并写测试跑通它

快捷键
------
    Ctrl+O    展开/折叠最近一个工具步骤
              点击某个工具步骤行，也可直接展开/折叠该步骤
    Ctrl+C    中断当前任务
    Ctrl+Q    退出窗口
    Enter     提交输入框中的任务

界面布局
--------
全屏三区结构：
    顶部  标题栏：模型 / 权限模式 / 工作目录
    中部  会话流：用户输入（> 前缀）、agent 正文（Markdown 渲染）、
          工具步骤（默认折叠成一行，含 diff 统计如 +14 -0）
    底部  状态栏（模式/轮次/工具数/上下文占用/token）+ 输入框

工具步骤展开后：命令类工具显示执行结果，文件写/改显示红绿高亮的 diff。
正文末尾的 [PROMPT] 建议会被提取出来，单独渲染为 Next steps 区块。

工具集（本地执行）
-----------------
read_file / write_file / edit_file / run_command / grep / glob

安全机制
--------
- 文件操作限制在启动时的工作目录内，阻止 ../ 路径逃逸
- run_command 分级审批：常规命令自动执行；危险命令（del/rm 等）需用户
  确认；极端危险命令（shutdown/format 等）直接拒绝
- API key 仅通过环境变量或 .env 提供，.env 已在 .gitignore 中排除

项目结构
--------
agent/
    cli.py          CLI 入口：加载配置、启动 TUI、新窗口重起
    app.py          Textual 全屏 TUI：界面组件、线程桥接、折叠/展开、diff 渲染
    config.py       环境变量/配置加载与校验
    client.py       LLM 调用封装：重试、流式解析、tool_calls 拼装
    loop.py         Agent 主循环与全部终止条件
    context.py      消息历史、token 估算、上下文裁剪压缩
    encoding.py     编码与环境适配：全项目统一 UTF-8（终端/子进程/文件/命令输出）
    permissions.py  路径防护、命令分级审批、out/ 输出目录解析
    trace.py        运行轨迹日志（JSONL）：每轮工具调用/结果/用量/终止原因
    tools/          6 个工具的 schema 定义与本地实现、注册表
tests/              单元测试（pytest）

运行测试
--------
    python -m pytest tests/ -q

设计说明
--------
主循环：用户输入 -> 调用模型 -> 若返回 tool_calls 则本地执行并把结果回填 -> 再次调用模型 -> 直到模型返回纯文本回答。

终止条件：正常回答 / 达到最大轮数 / token 超预算 / 同一工具同参数连续失败 3 次（防卡死）。

上下文管理：超出阈值时先将较早的超长工具输出压缩为摘要，仍超限则成组丢弃最旧对话（保证不留下孤立的 tool 消息），system 与最近消息始终保留。

编码适配：全项目统一 UTF-8。标准流启动时重配置为 UTF-8；子进程注入PYTHONUTF8/PYTHONIOENCODING；命令输出与文件读取按 UTF-8 优先、系统代码页（GBK）兜底解码，Windows 中文环境下终端、工具输出均不乱码。

运行轨迹日志：每次会话把每轮的工具调用、工具结果、token 用量与终止原因按 JSONL 逐行写入 logs/agent-trace-<时间戳>.jsonl，供回看与审计；logs/ 已加入 .gitignore，不污染仓库。
