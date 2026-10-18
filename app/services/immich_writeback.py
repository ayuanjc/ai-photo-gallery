"""Immich 统一写回客户端：幂等 + 可撤销 + 审计。

所有对 Immich 的写操作（标签/相册/描述）都经过此客户端，保证：
- 幂等：重复调用同一操作不会产生副作用（Immich 侧 find-or-create + Redis SETNX 窗口）
- 可撤销：审计日志记录每次操作，rollback() 可逆向回滚
- 非阻塞：失败返回 error 条目，不抛异常
- 可开关：writeback_enabled 只认 .env / Settings 单一来源（无运行时覆盖）
- dry_run：仅审计不实际写入

审计日志格式（JSONL，每行一条）：
    {"action": "add_tag", "asset_id": "xxx", "target": "AI/自然", ...}
"""
from __future__ import annotations

import asyncio
import functools
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from ..core.config import Settings
from .immich import ImmichClient

if TYPE_CHECKING:
    from ..core.redis_client import RedisClient

logger = logging.getLogger(__name__)

_AI_PREFIX = "AI·"

# 冒充搜索入口的历史误建相册，cleanup 默认一并清理。
DEFAULT_CLEANUP_ALBUM_NAMES: tuple[str, ...] = ("🌐 云影图鉴 — AI 智能搜索",)

# 对 Immich 的写操作全局并发上限：突发大量搜索触发写回时避免打爆上游
_MAX_CONCURRENT_WRITES = 8


def _limited(fn):
    """顶层写操作统一限流：同一 ImmichWriteBack 实例内最多 8 个并发写。"""
    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        async with self.write_semaphore:
            return await fn(self, *args, **kwargs)
    return wrapper


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
        redis_client: RedisClient | None = None,
    ) -> None:
        self._immich = immich
        self._settings = settings
        self._audit_path = audit_path or Path("data/writeback_audit.jsonl")
        self._redis = redis_client
        # 实例级信号量：随 app.state 生命周期创建，避免跨事件循环复用
        self.write_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_WRITES)

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
        """写回开关只认 .env / Settings 单一来源（不读运行时覆盖）。"""
        return bool(self._settings.writeback_enabled)

    async def _claim_once(self, dedup_key: str) -> bool:
        """Redis SETNX 幂等窗口：True=首次（可写），False=窗口内已写过。

        Redis 未启用/不可用/出错时返回 True 降级（由 Immich 侧 find-or-create
        + PUT assets 的天然幂等兜底）。
        """
        if self._redis is None or not getattr(self._redis, "connected", False):
            return True
        try:
            result = await self._redis.redis.set(
                dedup_key, "1",
                ex=self._settings.writeback_dedup_ttl_seconds,
                nx=True,
            )
            return bool(result)
        except Exception as exc:
            logger.warning("写回幂等键写入失败（降级为直写）: %s", exc)
            return True

    @_limited
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

        if not await self._claim_once(f"writeback:tag:{asset_id}:{tag}"):
            entry.success = True
            entry.error = "幂等窗口内已写过，跳过"
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

    @_limited
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

    @_limited
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

    @_limited
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

    @_limited
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

    @_limited
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

    async def list_tags(self) -> list[dict]:
        """列出 Immich 全部标签（兼容裸数组与 {tags: [...]} 两种响应形态）。"""
        data = await self._immich.request_json("GET", "/api/tags")
        if isinstance(data, list):
            return [t for t in data if isinstance(t, dict)]
        if isinstance(data, dict) and isinstance(data.get("tags"), list):
            return [t for t in data["tags"] if isinstance(t, dict)]
        return []

    async def list_albums(self) -> list[dict]:
        """列出 Immich 全部相册（兼容裸数组与 {albums: [...]} 两种响应形态）。"""
        data = await self._immich.request_json("GET", "/api/albums")
        if isinstance(data, list):
            return [a for a in data if isinstance(a, dict)]
        if isinstance(data, dict) and isinstance(data.get("albums"), list):
            return [a for a in data["albums"] if isinstance(a, dict)]
        return []

    @staticmethod
    def _tag_name(tag: dict) -> str:
        return str(tag.get("value") or tag.get("name") or "")

    @_limited
    async def delete_tag(self, tag_id: str, name: str) -> WriteBackEntry:
        """整体删除 Immich 标签（含全部资产关联）。

        管理员显式清理操作，不受 writeback_enabled 开关限制，但同样审计。
        """
        entry = WriteBackEntry(
            action="delete_tag", target=name, target_id=tag_id,
            timestamp=self._now(),
        )
        try:
            resp = await self._immich.request("DELETE", f"/api/tags/{tag_id}")
            if resp.is_success:
                entry.success = True
            else:
                entry.error = f"HTTP {resp.status_code}"
        except Exception as exc:
            entry.error = str(exc)
            logger.warning("标签删除失败 id=%s name=%s: %s", tag_id, name, exc)
        self._audit(entry)
        return entry

    @_limited
    async def delete_album(self, album_id: str, name: str) -> WriteBackEntry:
        """整体删除 Immich 相册（只删相册，不动资产本体）。

        管理员显式清理操作，不受 writeback_enabled 开关限制，但同样审计。
        """
        entry = WriteBackEntry(
            action="delete_album", target=name, target_id=album_id,
            timestamp=self._now(),
        )
        try:
            resp = await self._immich.request("DELETE", f"/api/albums/{album_id}")
            if resp.is_success:
                entry.success = True
            else:
                entry.error = f"HTTP {resp.status_code}"
        except Exception as exc:
            entry.error = str(exc)
            logger.warning("相册删除失败 id=%s name=%s: %s", album_id, name, exc)
        self._audit(entry)
        return entry

    async def cleanup(
        self,
        *,
        tag_prefix: str = _AI_PREFIX,
        album_prefix: str = _AI_PREFIX,
        album_names: tuple[str, ...] | list[str] = DEFAULT_CLEANUP_ALBUM_NAMES,
        dry_run: bool = True,
    ) -> dict:
        """清理本服务写回的污染数据：前缀匹配的标签/相册 + 精确匹配的额外相册。

        dry_run=True 仅扫描并返回将删除的清单（零写入、零审计）；
        dry_run=False 逐项删除并逐条审计（delete_tag/delete_album）。
        单项失败计入 errors 并继续，不中断整体清理。
        """
        report: dict = {
            "dry_run": dry_run,
            "tags": [],
            "albums": [],
            "deleted_tags": 0,
            "deleted_albums": 0,
            "failed": 0,
            "errors": [],
        }
        try:
            tags = await self.list_tags()
        except Exception as exc:
            report["errors"].append(f"标签列表获取失败: {exc}")
            tags = []
        try:
            albums = await self.list_albums()
        except Exception as exc:
            report["errors"].append(f"相册列表获取失败: {exc}")
            albums = []

        extra_names = set(album_names or ())
        matched_tags = [t for t in tags if self._tag_name(t).startswith(tag_prefix)]
        matched_albums = [
            a
            for a in albums
            if str(a.get("albumName") or "").startswith(album_prefix)
            or str(a.get("albumName") or "") in extra_names
        ]
        report["tags"] = [
            {"id": t.get("id"), "name": self._tag_name(t)} for t in matched_tags
        ]
        report["albums"] = [
            {
                "id": a.get("id"),
                "name": a.get("albumName"),
                "asset_count": a.get("assetCount"),
            }
            for a in matched_albums
        ]
        if dry_run:
            return report

        for t in matched_tags:
            name = self._tag_name(t)
            entry = await self.delete_tag(str(t.get("id")), name)
            if entry.success:
                report["deleted_tags"] += 1
            else:
                report["failed"] += 1
                report["errors"].append(f"标签删除失败 {name}: {entry.error}")
        for a in matched_albums:
            name = str(a.get("albumName") or "")
            entry = await self.delete_album(str(a.get("id")), name)
            if entry.success:
                report["deleted_albums"] += 1
            else:
                report["failed"] += 1
                report["errors"].append(f"相册删除失败 {name}: {entry.error}")
        return report

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
