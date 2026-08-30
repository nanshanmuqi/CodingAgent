"""配置加载：环境变量优先，本地 .env 文件兜底。凭据一律不入库。"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass
class Config:
    api_key: str
    base_url: str
    model_name: str
    max_rounds: int = 40
    token_budget: int = 200_000
    command_timeout: int = 60
    # 发送给模型的上下文 token 阈值，超过则触发裁剪（deepseek-chat 上下文为 64K）
    max_context_tokens: int = 60_000


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_config() -> Config:
    """从环境变量/.env 加载配置，缺少必填项时直接退出并提示。"""
    load_dotenv()

    missing = [k for k in ("API_KEY", "BASE_URL", "MODEL_NAME") if not os.getenv(k, "").strip()]
    if missing:
        raise SystemExit(
            f"缺少必要配置：{', '.join(missing)}\n"
            "请通过环境变量或项目根目录 .env 文件提供（格式参考 .env.example）"
        )

    return Config(
        api_key=os.environ["API_KEY"].strip(),
        base_url=os.environ["BASE_URL"].strip(),
        model_name=os.environ["MODEL_NAME"].strip(),
        max_rounds=_get_int("MAX_ROUNDS", 40),
        token_budget=_get_int("TOKEN_BUDGET", 200_000),
        command_timeout=_get_int("COMMAND_TIMEOUT", 60),
        max_context_tokens=_get_int("MAX_CONTEXT_TOKENS", 60_000),
    )
