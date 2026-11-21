"""活动台账：用结构化活动元数据锁定照片范围。

设计要点：
- 纯标准库、无 Immich 依赖，主应用与轻量 demo 共用。
- 台账用 JSON 文件承载（3 场活动、百数量级资产，无需数据库）。
- resolve 三级匹配：别名精确 → 子串/近似 → 时间窗；多命中一律返回 ambiguous，
  由编排层（Coze / 前端）追问，绝不替用户猜场次。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

DEFAULT_PATH = Path("data/activities/activities.json")
DATE_WINDOW_DAYS = 1
RATIO_THRESHOLD = 0.72
# 2 字以内的名字（「迎新」「志愿」「彩排」）太泛，不足以压过包含它的长名活动，
# 命中后降到子串层参与竞争，多命中即回 ambiguous，避免把场次猜错。
EXACT_MIN_KEY_LEN = 3

# 用途类后缀词：从「迎新晚会 推文封面」里剥掉，只留活动名去匹配。
SLOT_NOISE = (
    "推文封面", "封面图", "封面", "正文配图", "正文", "配图", "素材包", "素材",
    "横图", "竖图", "方形", "换一张", "再来一张", "找图", "选图", "照片", "图片",
)


_PUNCT = set(
    " \t\r\n\u3000,，.。、;；:：!！?？" + '"' + "'" + "“”‘’()（）[]【】<>《》/\\|-—_"
)


def _norm(text: str) -> str:
    """归一化：去空白与常见标点，小写（活动名匹配不区分标点与全半角空格）。"""
    return "".join(ch for ch in (text or "").lower() if ch not in _PUNCT)


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class ResolveOutcome:
    """一次活动解析的结构化结果。"""

    status: str  # matched | ambiguous | none
    activity: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    need: str = "none"  # pick_one | none
    matched_by: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "activity": self.activity,
            "candidates": self.candidates,
            "need": self.need,
            "matched_by": self.matched_by,
            "reason": self.reason,
        }


@dataclass
class Activity:
    """活动台账条目。"""

    id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    starts_at: str | None = None
    ends_at: str | None = None
    organizer: str = ""
    venue: str = ""
    asset_ids: list[str] = field(default_factory=list)
    cover_asset_id: str | None = None
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Activity":
        known = {"id", "name", "aliases", "starts_at", "ends_at", "organizer", "venue",
                 "asset_ids", "cover_asset_id", "note"}
        return cls(
            id=str(raw["id"]),
            name=str(raw.get("name") or raw["id"]),
            aliases=[str(a) for a in (raw.get("aliases") or [])],
            starts_at=raw.get("starts_at"),
            ends_at=raw.get("ends_at"),
            organizer=str(raw.get("organizer") or ""),
            venue=str(raw.get("venue") or ""),
            asset_ids=[str(a) for a in (raw.get("asset_ids") or [])],
            cover_asset_id=raw.get("cover_asset_id"),
            note=str(raw.get("note") or ""),
            extra={k: v for k, v in raw.items() if k not in known},
        )

    def to_summary(self) -> dict[str, Any]:
        """列表视图：不带全量 asset_ids。"""
        return {
            "id": self.id,
            "name": self.name,
            "aliases": self.aliases,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "organizer": self.organizer,
            "venue": self.venue,
            "cover_asset_id": self.cover_asset_id,
            "count": len(self.asset_ids),
        }

    def to_dict(self) -> dict[str, Any]:
        data = self.to_summary()
        data["asset_ids"] = list(self.asset_ids)
        data["note"] = self.note
        data.update(self.extra)
        return data

    def in_window(self, dt: datetime | None) -> bool:
        """判断时间是否落在活动时段（含首尾各放宽 1 天）。"""
        if dt is None:
            return False
        start = _parse_dt(self.starts_at)
        end = _parse_dt(self.ends_at) or start
        if start is None:
            return False
        pad = timedelta(days=DATE_WINDOW_DAYS)
        return (start - pad) <= dt <= ((end or start) + pad)

    def album_name(self, prefix: str = "云影图鉴 · ") -> str:
        date = (self.starts_at or "")[:10]
        return f"{prefix}{self.name}{(' ' + date) if date else ''}"


class ActivityStore:
    """活动台账存取与解析。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else DEFAULT_PATH
        self._activities: list[Activity] = []
        self._by_id: dict[str, Activity] = {}
        self.reload()

    # ---- 存取 -------------------------------------------------------
    def reload(self) -> None:
        self._activities = []
        self._by_id = {}
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        items = raw.get("activities") if isinstance(raw, dict) else raw
        for entry in items or []:
            try:
                act = Activity.from_dict(entry)
            except (KeyError, TypeError):
                continue
            self._activities.append(act)
            self._by_id[act.id] = act

    @property
    def path(self) -> Path:
        return self._path

    def list(self) -> list[Activity]:
        return list(self._activities)

    def get(self, activity_id: str) -> Activity | None:
        return self._by_id.get(activity_id)

    def find_by_name(self, name: str) -> list[Activity]:
        key = _norm(name)
        if not key:
            return []
        hits = []
        for act in self._activities:
            names = [_norm(act.name)] + [_norm(a) for a in act.aliases]
            if key in names:
                hits.append(act)
        return hits

    # ---- 解析 -------------------------------------------------------
    def resolve(
        self,
        query: str | None = None,
        date: str | None = None,
        name: str | None = None,
    ) -> ResolveOutcome:
        """把自然语言/日期解析为唯一活动，或返回需要追问的候选集。"""
        text = (name or query or "").strip()
        if text:
            outcome = self._resolve_text(text)
            if outcome.status != "none":
                return outcome
            # 剥掉用途类后缀再试一次（「迎新晚会 推文封面」→「迎新晚会」）
            stripped = self._strip_noise(text)
            if stripped and stripped != text:
                outcome = self._resolve_text(stripped)
                if outcome.status != "none":
                    return outcome
        if date:
            return self._resolve_date(date)
        return ResolveOutcome(status="none", reason="台账里没有可匹配的活动，请先给出活动名称或日期")

    def _strip_noise(self, text: str) -> str:
        out = text
        changed = True
        while changed:
            changed = False
            for token in SLOT_NOISE:
                if token in out:
                    out = out.replace(token, "", 1)
                    changed = True
        return out.strip()

    def _candidate(self, act: Activity, why: str) -> dict[str, Any]:
        data = act.to_summary()
        data["why"] = why
        return data

    def _rank(self, hits: Iterable[tuple[Activity, str]]) -> ResolveOutcome:
        seen: dict[str, dict[str, Any]] = {}
        for act, why in hits:
            if act.id not in seen:
                seen[act.id] = self._candidate(act, why)
        candidates = list(seen.values())
        # 场次多者优先，其次时间近者优先（字符串降序即时间倒序）
        candidates.sort(key=lambda c: (c.get("count") or 0, c.get("starts_at") or ""), reverse=True)
        if not candidates:
            return ResolveOutcome(status="none", reason="未匹配到活动")
        if len(candidates) == 1:
            act = self._by_id[candidates[0]["id"]]
            return ResolveOutcome(
                status="matched",
                activity=self._candidate(act, candidates[0]["why"]),
                candidates=candidates,
                matched_by=candidates[0]["why"],
                reason=f"唯一命中「{act.name}」",
            )
        return ResolveOutcome(
            status="ambiguous",
            candidates=candidates,
            need="pick_one",
            reason=f"命中 {len(candidates)} 场同名/相近活动，需用户选择场次",
        )

    def _resolve_text(self, text: str) -> ResolveOutcome:
        key = _norm(text)
        if not key:
            return ResolveOutcome(status="none")
        exact: list[tuple[Activity, str]] = []
        substring: list[tuple[Activity, str]] = []
        fuzzy: list[tuple[Activity, str]] = []
        for act in self._activities:
            names = [act.name] + list(act.aliases)
            norm_names = [_norm(n) for n in names if n]
            if key in norm_names:
                if len(key) >= EXACT_MIN_KEY_LEN:
                    exact.append((act, "别名精确"))
                else:
                    substring.append((act, "短名命中"))
                continue
            if any(len(n) >= 2 and (n in key or key in n) for n in norm_names):
                substring.append((act, "子串命中"))
                continue
            best = max((_ratio(key, n) for n in norm_names), default=0.0)
            if best >= RATIO_THRESHOLD:
                fuzzy.append((act, f"近似 {best:.2f}"))
        for bucket in (exact, substring, fuzzy):
            if bucket:
                return self._rank(bucket)
        return ResolveOutcome(status="none")

    def _resolve_date(self, date: str) -> ResolveOutcome:
        dt = _parse_dt(date)
        if dt is None:
            return ResolveOutcome(status="none", reason=f"无法解析日期: {date}")
        hits = [(act, "时间窗命中") for act in self._activities if act.in_window(dt)]
        if not hits:
            return ResolveOutcome(status="none", reason="该日期没有台账活动")
        return self._rank(hits)


def act_summary_of(outcome: ResolveOutcome) -> dict[str, Any]:
    return outcome.to_dict()
