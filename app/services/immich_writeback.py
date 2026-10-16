"""Immich 统一写回客户端：幂等 + 可撤销 + 审计。

所有对 Immich 的写操作（标签/相册/描述）都经过此客户端，保证：
- 幂等：重复调用同一操作不会产生副作用
- 可撤销：审计日志记录每次操作，rollback() 可逆向回滚
- 非阻塞：失败返回 error 条目，不抛异常
- 可开关：通过 runtime_config 的 writeback_enabled 控制
- dry_run：仅审计不实际写入

审计日志格式（JSONL，每行一条）：
    {"action": "add_tag", "asset_id": "xxx", "target": "AI/自然", ...}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..core.config import Settings
from ..core.runtime_config import runtime_enabled
from .immich import ImmichClient

logger = logging.getLogger(__name__)

_AI_PREFIX = "AI/"


class WriteBackEntry(BaseModel):
    action: str = Field(description="add_tag|remove_tag|add_to_album|remove_from_album|create_album|update_description|update_rating")
    asset_id: str | None = None
    target: str = Field(description="标签名或相册 ID/名称")
    target_id: str | None = None
    timestamp: str = ""
    success: bool = False
    error: str | None = None
    dry_run: bool = False
    old_value: str | None = None

    def is_reverse_of(self, other: WriteBackEntry) -> bool:
        if self.asset_id != other.asset_id or self.target != other.target:
            return False
        pairs = {
            ("add_tag", "remove_tag"),
            ("add_to_album", "remove_from_album"),
            ("update_description", "update_description"),
            ("update_rating", "update_rating"),
        }
        return (other.action, self.action) in pairs


class ImmichWriteBack:
    def __init__(
        self,
        immich: ImmichClient,
        settings: Settings,
        audit_path: Path | None = None,
    ) -> None:
        self._immich = immich
        self._settings = settings
        self._audit_path = audit_path or Path("data/writeback_audit.jsonl")

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _audit(self, entry: WriteBackEntry) -> None:
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open("a", encoding="utf-8") as f:
                f.write(entry.model_dump_json() + "\n")
        except OSError as exc:
            logger.warning("审计日志写入失败: %s", exc)

    def _enabled(self) -> bool:
        return runtime_enabled("writeback_enabled", getattr(self._settings, "writeback_enabled", False))

    async def add_tag(
        self, asset_id: str, tag: str, dry_run: bool = False
    ) -> WriteBackEntry:
        if not self._enabled() and not dry_run:
            return WriteBackEntry(
                action="add_tag", asset_id=asset_id, target=tag,
                timestamp=self._now(), success=False, error="写回未启用",
            )

        entry = WriteBackEntry(
            action="add_tag", asset_id=asset_id, target=tag,
            timestamp=self._now(), dry_run=dry_run,
        )

        if dry_run:
            entry.success = True
            self._audit(entry)
            return entry

        try:
            tag_id = await self._find_or_create_tag(tag)
            entry.target_id = tag_id
            await self._immich.request_json(
                "PUT", f"/api/tags/{tag_id}/assets",
                json={"ids": [asset_id]},
            )
            entry.success = True
        except Exception as exc:
            entry.error = str(exc)
            logger.warning("标签写回失败 asset=%s tag=%s: %s", asset_id, tag, exc)

        self._audit(entry)
        return entry

    async def remove_tag(self, asset_id: str, tag: str) -> WriteBackEntry:
        entry = WriteBackEntry(
            action="remove_tag", asset_id=asset_id, target=tag,
            timestamp=self._now(),
        )
        try:
            tag_id = await self._find_tag(tag)
            if tag_id is None:
                entry.success = True
                entry.error = "标签不存在，无需移除"
                self._audit(entry)
                return entry
            entry.target_id = tag_id
            await self._immich.request_json(
                "DELETE", f"/api/tags/{tag_id}/assets",
                json={"ids": [asset_id]},
            )
            entry.success = True
        except Exception as exc:
            entry.error = str(exc)
            logger.warning("标签移除失败 asset=%s tag=%s: %s", asset_id, tag, exc)

        self._audit(entry)
        return entry

    async def get_or_create_album(
        self, name: str, dry_run: bool = False
    ) -> tuple[str | None, WriteBackEntry]:
        entry = WriteBackEntry(
            action="create_album", target=name,
            timestamp=self._now(), dry_run=dry_run,
        )

        if dry_run:
            entry.success = True
            self._audit(entry)
            return None, entry

        try:
            albums = await self._immich.request_json("GET", "/api/albums")
            if isinstance(albums, list):
                for album in albums:
                    if isinstance(album, dict) and album.get("albumName") == name:
                        entry.target_id = album["id"]
                        entry.success = True
                        entry.error = "已存在"
                        self._audit(entry)
                        return album["id"], entry

            result = await self._immich.request_json(
                "POST", "/api/albums",
                json={"albumName": name, "assetIds": []},
            )
            album_id = result.get("id") if isinstance(result, dict) else None
            entry.target_id = album_id
            entry.success = album_id is not None
        except Exception as exc:
            entry.error = str(exc)
            logger.warning("相册创建失败 name=%s: %s", name, exc)

        self._audit(entry)
        return entry.target_id, entry

    async def add_to_album(
        self, asset_id: str, album_id: str, dry_run: bool = False
    ) -> WriteBackEntry:
        entry = WriteBackEntry(
            action="add_to_album", asset_id=asset_id, target=album_id,
            timestamp=self._now(), dry_run=dry_run,
        )

        if dry_run:
            entry.success = True
            self._audit(entry)
            return entry

        try:
            await self._immich.request_json(
                "PUT", f"/api/albums/{album_id}/assets",
                json={"ids": [asset_id]},
            )
            entry.success = True
        except Exception as exc:
            entry.error = str(exc)
            logger.warning("相册添加失败 asset=%s album=%s: %s", asset_id, album_id, exc)

        self._audit(entry)
        return entry

    async def remove_from_album(
        self, asset_id: str, album_id: str
    ) -> WriteBackEntry:
        entry = WriteBackEntry(
            action="remove_from_album", asset_id=asset_id, target=album_id,
            timestamp=self._now(),
        )
        try:
            await self._immich.request_json(
                "DELETE", f"/api/albums/{album_id}/assets",
                json={"ids": [asset_id]},
            )
            entry.success = True
        except Exception as exc:
            entry.error = str(exc)
            logger.warning("相册移除失败 asset=%s album=%s: %s", asset_id, album_id, exc)

        self._audit(entry)
        return entry

    async def update_description(
        self, asset_id: str, description: str, dry_run: bool = False
    ) -> WriteBackEntry:
        if not self._enabled() and not dry_run:
            return WriteBackEntry(
                action="update_description", asset_id=asset_id, target=description,
                timestamp=self._now(), success=False, error="写回未启用",
            )

        entry = WriteBackEntry(
            action="update_description", asset_id=asset_id, target=description,
            timestamp=self._now(), dry_run=dry_run,
        )

        if dry_run:
            entry.success = True
            self._audit(entry)
            return entry

        try:
            old = await self._immich.request_json("GET", f"/api/assets/{asset_id}")
            if isinstance(old, dict):
                entry.old_value = str(old.get("description") or "")
            await self._immich.request_json(
                "PUT", f"/api/assets/{asset_id}",
                json={"description": description},
            )
            entry.success = True
        except Exception as exc:
            entry.error = str(exc)
            logger.warning("描述写回失败 asset=%s: %s", asset_id, exc)

        self._audit(entry)
        return entry

    async def update_rating(
        self, asset_id: str, rating: int, dry_run: bool = False
    ) -> WriteBackEntry:
        rating = max(0, min(5, int(rating)))

        if not self._enabled() and not dry_run:
            return WriteBackEntry(
                action="update_rating", asset_id=asset_id, target=str(rating),
                timestamp=self._now(), success=False, error="写回未启用",
            )

        entry = WriteBackEntry(
            action="update_rating", asset_id=asset_id, target=str(rating),
            timestamp=self._now(), dry_run=dry_run,
        )

        if dry_run:
            entry.success = True
            self._audit(entry)
            return entry

        try:
            old = await self._immich.request_json("GET", f"/api/assets/{asset_id}")
            if isinstance(old, dict):
                entry.old_value = str(old.get("rating") or "")
            await self._immich.request_json(
                "PUT", f"/api/assets/{asset_id}",
                json={"rating": rating},
            )
            entry.success = True
        except Exception as exc:
            entry.error = str(exc)
            logger.warning("评分写回失败 asset=%s rating=%d: %s", asset_id, rating, exc)

        self._audit(entry)
        return entry

    async def rollback(self, entries: list[WriteBackEntry]) -> list[WriteBackEntry]:
        results: list[WriteBackEntry] = []
        for entry in reversed(entries):
            if not entry.success or entry.dry_run:
                continue
            if entry.action == "add_tag" and entry.asset_id:
                results.append(await self.remove_tag(entry.asset_id, entry.target))
            elif entry.action == "add_to_album" and entry.asset_id:
                results.append(await self.remove_from_album(entry.asset_id, entry.target))
            elif entry.action == "update_description" and entry.asset_id:
                old_desc = entry.old_value or ""
                results.append(await self.update_description(entry.asset_id, old_desc))
            elif entry.action == "update_rating" and entry.asset_id:
                old_rating = int(entry.old_value) if entry.old_value and entry.old_value.isdigit() else 0
                results.append(await self.update_rating(entry.asset_id, old_rating))
        return results

    async def _find_tag(self, name: str) -> str | None:
        tags = await self._immich.request_json("GET", "/api/tags")
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, dict) and tag.get("value") == name:
                    return tag.get("id")
                if isinstance(tag, dict) and tag.get("name") == name:
                    return tag.get("id")
        return None

    async def _find_or_create_tag(self, name: str) -> str:
        existing = await self._find_tag(name)
        if existing:
            return existing
        result = await self._immich.request_json("POST", "/api/tags", json={"name": name})
        tag_id = result.get("id") if isinstance(result, dict) else None
        if not tag_id:
            found = await self._find_tag(name)
            if found:
                return found
            raise RuntimeError(f"标签创建失败: {name}")
        return tag_id