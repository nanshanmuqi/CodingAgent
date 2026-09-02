"""会话持久化：把对话历史序列化到 sessions/ 目录，支持 --continue/--resume 恢复。

会话文件为 sessions/<id>.json，内容为 JSON：
    { "session_id", "model", "updated", "messages": [...] }
messages 直接复用 MessageHistory.messages 的结构（system/user/assistant/tool）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

SESSIONS_DIR = Path("sessions")


def new_session_id() -> str:
    """生成按时间排序的新会话 id（同时可作人类可读的时间戳）。"""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


class SessionStore:
    def __init__(self, directory: Path = SESSIONS_DIR) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.json"

    def save(
        self,
        session_id: str,
        messages: list[dict],
        model_name: str,
        tool_details: dict | None = None,
    ) -> None:
        title = ""
        for m in messages:
            if m.get("role") == "user":
                title = str(m.get("content", "")).splitlines()[0][:60]
                break
        data = {
            "session_id": session_id,
            "model": model_name,
            "title": title,
            "updated": datetime.now().isoformat(timespec="seconds"),
            "messages": messages,
            "tool_details": tool_details or {},
        }
        self._path(session_id).write_text(
            json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8"
        )

    def load(self, session_id: str) -> Optional[dict]:
        path = self._path(session_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def latest_id(self) -> Optional[str]:
        """最近一次更新的会话 id（供 --continue 使用）。"""
        files = list(self._dir.glob("*.json"))
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime).stem

    def list_sessions(self) -> list[dict]:
        """返回会话摘要列表（不含 messages），按更新时间倒序。"""
        sessions = []
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            sessions.append((
                path.stat().st_mtime,
                {
                    "session_id": data.get("session_id", path.stem),
                    "title": data.get("title", ""),
                    "updated": data.get("updated", ""),
                },
            ))
        sessions.sort(key=lambda item: item[0], reverse=True)
        return [summary for _, summary in sessions]
