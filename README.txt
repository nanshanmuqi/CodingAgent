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
    python -m agent.cli                在当前终端交互
    python -m agent.cli --new-window   唤起独立的新终端窗口进行交互

启动后在"User >"提示符下直接描述编程任务，例如：
    读取当前目录的 main.py，解释它的功能
    写一个快速排序到 sort.py，并写测试跑通它

交互命令
--------
    /help    显示帮助
    /reset   清空对话上下文
    /tokens  查看累计 token 用量
    /quit    退出
    Ctrl+C   中断当前任务

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
    cli.py          CLI 入口：REPL、流式输出、工作状态显示、斜杠命令（rich 行内渲染）
    config.py       环境变量/配置加载与校验
    client.py       LLM 调用封装：重试、流式解析、tool_calls 拼装
    loop.py         Agent 主循环与全部终止条件
    context.py      消息历史、token 估算、上下文裁剪压缩
    permissions.py  路径防护、命令分级审批、out/ 输出目录解析
    tools/          6 个工具的 schema 定义与本地实现、注册表
tests/              单元测试（pytest，35 个用例）

运行测试
--------
    python -m pytest tests/ -q

设计说明
--------
主循环：用户输入 -> 调用模型 -> 若返回 tool_calls 则本地执行并把结果回填
-> 再次调用模型 -> 直到模型返回纯文本回答。
终止条件：正常回答 / 达到最大轮数 / token 超预算 / 用户 Ctrl+C /
同一工具同参数连续失败 3 次（防卡死）。
上下文管理：超出阈值时先将较早的超长工具输出压缩为摘要，仍超限则成组
丢弃最旧对话（保证不留下孤立的 tool 消息），system 与最近消息始终保留。
