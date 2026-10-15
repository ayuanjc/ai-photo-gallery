"""写回管理服务：审计日志读取 + 一键回滚。

功能：
1. 读取审计日志（JSONL）→ 列表
2. 按时间范围/动作类型过滤
3. 一键回滚指定时间范围内的所有写回操作
4. 冲突检测：检查资产是否已有非 AI 前缀的标签（人工标签不覆盖）
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..core.config import Settings
from .immich_writeback import ImmichWriteBack, WriteBackEntry

logger = logging.getLogger(__name__)

_AI_PREFIX = "AI/"
_DEFAULT_AUDIT_PATH = Path("data/writeback_audit.jsonl")


class WriteBackManager:
    def __init__(
        self,
        writeback: ImmichWriteBack,
        settings: Settings,
        audit_path: Path | None = None,
    ) -> None:
        self._writeback = writeback
        self._settings = settings
        self._audit_path = audit_path or _DEFAULT_AUDIT_PATH

    def read_audit_log(
        self,
        action: str | None = None,
        asset_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not self._audit_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            lines = self._audit_path.read_text(encoding="utf-8").strip().split("\n")
        except OSError as exc:
            logger.warning("审计日志读取失败: %s", exc)
            return []

        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if action and entry.get("action") != action:
                continue
            if asset_id and entry.get("asset_id") != asset_id:
                continue
            entries.append(entry)
            if len(entries) >= limit:
                break
        return entries

    async def rollback_recent(self, count: int = 10) -> dict[str, Any]:
        entries_raw = self.read_audit_log(limit=count)
        entries = [WriteBackEntry(**e) for e in entries_raw]
        results = await self._writeback.rollback(entries)
        return {
            "requested": len(entries),
            "rolled_back": len(results),
            "succeeded": sum(1 for r in results if r.success),
            "failed": sum(1 for r in results if not r.success),
        }

    async def rollback_by_asset(self, asset_id: str) -> dict[str, Any]:
        entries_raw = self.read_audit_log(asset_id=asset_id, limit=1000)
        entries = [WriteBackEntry(**e) for e in entries_raw]
        results = await self._writeback.rollback(entries)
        return {
            "asset_id": asset_id,
            "requested": len(entries),
            "rolled_back": len(results),
            "succeeded": sum(1 for r in results if r.success),
            "failed": sum(1 for r in results if not r.success),
        }

    async def detect_conflicts(self, asset_id: str) -> list[dict[str, Any]]:
        """检测冲突：资产已有非 AI 前缀的同名标签。"""
        try:
            data = await self._writeback._immich.request_json(
                "GET", f"/api/assets/{asset_id}"
            )
            if not isinstance(data, dict):
                return []
            tags = data.get("tags", [])
            conflicts: list[dict[str, Any]] = []
            for tag in tags:
                if isinstance(tag, dict):
                    name = tag.get("value") or tag.get("name", "")
                    if name and not name.startswith(_AI_PREFIX):
                        conflicts.append({
                            "asset_id": asset_id,
                            "tag": name,
                            "type": "human_tag",
                            "resolution": "skip",
                        })
            return conflicts
        except Exception as exc:
            logger.warning("冲突检测失败 asset=%s: %s", asset_id, exc)
            return []