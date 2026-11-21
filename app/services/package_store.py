"""素材包会话落盘：同一 session_id 反复编辑、导出，进程重启不丢。

设计：
- 每会话一个 JSON 文件（data/packages/{session_id}.json），原子写（临时文件 + replace）。
- 会话自带 expires_at，过期即视为不存在（load 返回 None 并删文件）。
- 只做存取，不理解包内语义；所有编排在 app/workflow/package.py。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class PackageStore:
    """素材包会话存储。"""

    def __init__(self, base_dir: str | Path = "data/packages", ttl_s: int = 3600) -> None:
        self._dir = Path(base_dir)
        self._ttl = int(ttl_s)

    @property
    def dir(self) -> Path:
        return self._dir

    @property
    def ttl(self) -> int:
        return self._ttl

    def _path(self, session_id: str) -> Path:
        safe = "".join(ch for ch in str(session_id) if ch.isalnum() or ch in "-_")[:64]
        if not safe:
            raise ValueError("session_id 非法")
        return self._dir / f"{safe}.json"

    def save(self, state: dict[str, Any]) -> Path:
        session_id = str(state.get("session_id") or "")
        path = self._path(session_id)
        self._dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        payload = dict(state)
        payload["saved_at"] = now
        payload["expires_at"] = now + int(state.get("ttl_s") or self._ttl)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def load(self, session_id: str) -> dict[str, Any] | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        expires_at = float(state.get("expires_at") or 0)
        if expires_at and expires_at < time.time():
            self.delete(session_id)
            return None
        return state

    def exists(self, session_id: str) -> bool:
        return self.load(session_id) is not None

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        try:
            path.unlink()
            return True
        except OSError:
            return False

    def purge_expired(self) -> int:
        """清理过期会话，返回删除数量。"""
        if not self._dir.exists():
            return 0
        removed = 0
        now = time.time()
        for path in self._dir.glob("*.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if float(state.get("expires_at") or 0) < now:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed
