"""活动台账 / 素材包 / 归档的请求与响应模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ActivityId = str


class ActivityItem(BaseModel):
    """活动摘要（列表视图）。"""

    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    starts_at: str | None = None
    ends_at: str | None = None
    organizer: str = ""
    venue: str = ""
    cover_asset_id: str | None = None
    count: int = 0


class ActivityDetail(ActivityItem):
    asset_ids: list[str] = Field(default_factory=list)
    note: str = ""


class ActivityListResponse(BaseModel):
    total: int
    activities: list[ActivityItem] = Field(default_factory=list)
    source: str = ""
    warnings: list[str] = Field(default_factory=list)


class ResolveRequest(BaseModel):
    """活动解析请求：名称/整句/日期三选一（可组合）。"""

    utterance: str | None = Field(default=None, max_length=200, description="整句，如「迎新晚会 推文封面」")
    name: str | None = Field(default=None, max_length=100, description="活动名")
    date: str | None = Field(default=None, max_length=40, description="ISO 日期或时间，如 2026-09-20")
    query: str | None = Field(default=None, max_length=200, description="utterance 的别名（兼容旧调用）")

    @field_validator("utterance", "name", "date", "query")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value


class ResolveResponse(BaseModel):
    status: Literal["matched", "ambiguous", "none"]
    activity: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    need: str = "none"
    matched_by: str | None = None
    reason: str = ""


class SearchInActivityRequest(BaseModel):
    activity_id: str = Field(..., max_length=100)
    query: str = Field(default="", max_length=200, description="画面语义描述，可为空=按分数/时间取")
    slots: dict[str, int] | None = Field(default=None, description="用途分组，如 {cover:3, body:5}")
    orientation: Literal["any", "landscape", "portrait", "square"] = "any"
    limit: int = Field(default=60, ge=1, le=200, description="候选池上限")


class PackageEditRequest(BaseModel):
    session_id: str = Field(..., max_length=64)
    revision: int = Field(..., ge=1)
    ops: list[dict[str, Any]] = Field(..., min_length=1, max_length=20)


class PackageEditResponse(BaseModel):
    session_id: str
    revision: int
    changes: list[dict[str, Any]] = Field(default_factory=list)
    diff: dict[str, Any] = Field(default_factory=dict)
    package: dict[str, Any] = Field(default_factory=dict)
    empty_slots: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ArchiveProposeRequest(BaseModel):
    activity_id: str = Field(..., max_length=100)
    asset_ids: list[str] | None = Field(default=None, max_length=500, description="为空则用素材包内全部图")
    session_id: str | None = Field(default=None, max_length=64, description="素材包会话（可选）")


class ArchiveCommitRequest(BaseModel):
    proposal_id: str = Field(..., max_length=64)


class ArchiveProposeResponse(BaseModel):
    proposal_id: str
    dry_run: bool = True
    album: dict[str, Any] = Field(default_factory=dict)
    will_add: int = 0
    already_in: int = 0
    tags: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    expires_in: int = 600


class ArchiveCommitResponse(BaseModel):
    album_id: str | None = None
    album_url: str | None = None
    album_name: str = ""
    added: int = 0
    skipped: int = 0
    failed: list[dict[str, Any]] = Field(default_factory=list)
    audit_ids: list[str] = Field(default_factory=list)
