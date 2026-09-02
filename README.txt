# Coding Agent

一个自主编程智能体：通过与 LLM 交互，自主读写文件、执行命令，完成编程任务。

## 仓库地址

- HTTPS：https://github.com/nanshanmuqi/CodingAgent
- SSH：git@github.com:nanshanmuqi/CodingAgent.git

## 特色功能

### 核心循环
- 自研主循环：用户输入 → 调用模型 → 本地执行工具 → 回填结果 → 再次调用，直到模型返回纯文本回答
- 多终止条件：正常回答 / 达到最大轮数 / token 超预算 / 同一工具同参数连续失败 3 次（防卡死）/ Ctrl+C 手动中断

### 工具执行
- 六种本地工具：read_file / write_file / edit_file / run_command / grep / glob
- 单轮多个工具调用并发执行（上限 8 个），结果按原顺序回填
- run_command 分级审批：常规命令自动执行，危险命令（del/rm 等）需确认，极端危险命令（shutdown/format 等）直接拒绝

### 流式与渲染
- 流式响应逐 token 回调，正文 Markdown 实时渲染
- tool_calls 按 index 分片累积拼装，兼容流式分片返回
- 文件改动以 diff 展示，增删行红绿高亮，可展开/折叠

### 上下文管理
- 超阈值时先把较早的超长工具输出压缩为摘要，仍超限则成组丢弃最旧对话
- 保证不留下孤立的 tool 消息，system 与最近消息始终保留

### 会话与日志
- 会话自动持久化到 sessions/，支持 --continue / --resume 恢复
- 每轮工具调用、结果、token 用量、终止原因按 JSONL 写入日志，供回看审计

### 编码适配
- 全项目统一 UTF-8：标准流重配置、子进程注入环境变量、命令输出与文件读取按 UTF-8 优先、GBK 兜底，Windows 中文环境不乱码

## 如何运行

1. 安装依赖：

   pip install -r requirements.txt

2. 配置凭据：复制 .env.example 为 .env，填写 API_KEY、BASE_URL、MODEL_NAME。

3. 启动：

   python -m agent.cli                # 默认独立新窗口
   python -m agent.cli --inline       # 当前终端
   python -m agent.cli --continue     # 恢复最近会话
   python -m agent.cli --resume <id>  # 恢复指定会话

## 快捷键

- Ctrl+C 中断任务 · Ctrl+Q 退出窗口 · Ctrl+O 展开/折叠工具步骤 · Enter 提交

## 其它说明

- 环境：Python 3.10+ / Windows，建议使用 Windows Terminal
- 测试：python -m pytest tests/ -q
- 运行轨迹日志写入 logs/，供回看与审计
