# Coding Agent

一个自主编程智能体：通过与 LLM 交互，自主读写文件、执行命令，完成编程任务。

## 仓库地址

- HTTPS：https://github.com/nanshanmuqi/CodingAgent
- SSH：git@github.com:nanshanmuqi/CodingAgent.git

## 特色功能

### 核心循环
- 自研主循环（agent/loop.py）：用户输入 → 调用模型 → 本地执行工具 → 回填结果 → 再次调用，直到模型返回纯文本回答
- 五种终止条件：
  - 正常结束：模型返回 `stop` 且无 tool_calls
  - 达到最大轮数（默认 40 轮，`MAX_ROUNDS` 可调）
  - 累计 token 超预算（默认 512K，`TOKEN_BUDGET` 可调）
  - 同一工具以相同参数连续失败 3 次（判定模型卡死，防止无限循环）
  - Ctrl+C 手动中断（线程安全的中断标志，循环在检查点停止）
- 单轮内多个工具调用相互独立，采用线程池并发执行，显著缩短多工具轮次耗时

### 工具执行
- 六种本地工具：read_file / write_file / edit_file / run_command / grep / glob
  - read_file：带行号读取，支持 offset/limit 分段读取大文件
  - write_file：创建或覆写文件，自动创建父目录，统一写入 out/ 目录
  - edit_file：搜索替换式局部编辑，old_str 须唯一匹配（或 replace_all 全量替换），行尾自动归一化
  - run_command：执行 Windows cmd 命令，超时强制终止（默认 60s，上限 300s），输出超长截断
  - grep：按正则搜索文件内容，返回「文件:行号: 内容」，跳过 .git/node_modules 等目录
  - glob：按文件名模式查找文件，支持 `**/*.py` 与 `*.py` 两种习惯
- 单轮多个工具调用并发执行（上限 8 个），结果按原顺序回填，保证 tool 消息与 tool_call id 一一对应
- run_command 三级命令分级：
  - safe：常规命令自动执行
  - dangerous：危险命令（del/rm/move/rename 等）需用户确认
  - forbidden：极端危险命令（format/shutdown/rm -rf/diskpart 等）直接拒绝
  - 额外识别间接脚本执行（python xx.py、powershell、.bat 等），防止绕过危险命令识别
- 路径防护：所有文件路径解析到工作目录内，`../` 逃逸直接拒绝；新生成文件自动落到 out/ 目录

### 流式与渲染
- 流式响应逐 token 回调，正文 Markdown 实时渲染（基于 Textual 全屏 TUI）
- tool_calls 按 index 分片累积拼装，兼容流式分片返回
- 文件改动以 diff 展示，增删行红绿高亮，可展开/折叠（Ctrl+O）
- agent 核心在后台线程执行，UI 主循环定时消费事件队列，两者解耦，界面在长任务中保持流畅

### 上下文管理
- token 用量估算：优先 tiktoken（cl100k_base），不可用时退化为字符数/4
- 两阶段裁剪策略：
  - 先压缩：把较早（非最近 10 条内）的超长工具输出替换为摘要
  - 再丢弃：仍超限则成组丢弃最旧对话（一个单元 = 一条非 tool 消息 + 其后连续的 tool 消息）
- 保证不留下孤立的 tool 消息，system 提示与最近消息始终保留

### 会话与日志
- 会话自动持久化到 sessions/<id>.json，支持 `--continue` / `--resume` 恢复
- 会话文件保存标题、模型、消息历史与工具详情（供 UI 重建过程块）
- 每轮工具调用、结果、token 用量、终止原因按 JSONL 写入 logs/，供回看审计（task / round / termination 三类记录）

### 编码适配
- 全项目统一 UTF-8：标准流重配置、子进程注入 `PYTHONUTF8=1` 环境变量、命令输出与文件读取按 UTF-8 优先、GBK 兜底
- 覆盖 Windows 中文环境（cmd 的 GBK 输出、GBK 文件读取等），确保不乱码

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
