"""搜索结果排序：以 Immich 召回顺序为权威，本服务只做诚实的展示计算。

两种模式（settings.rank_mode）：
    immich（默认）：结果顺序与 Immich 返回顺序完全一致。final_score 不编造
        （None）；similarity 仅在上游返回真实 score 时才有值。
    blend：final = (1-w_time)*relevance_prior + w_time*freshness，
        relevance_prior = 60/(60+immich_rank)（RRF 归一化），w_time 上限 0.2。
        场景动态权重（SCENE_WEIGHTS）已移除——场景标签绝不参与搜索排序。

- Immich v3.3.1 smart search 不返回相似度分数，任何按名次编造的
  similarity 都是伪数据（曾导致：前端展示假"匹配度%"、去重误删高分结果）。
- 拍摄时间来源优先级：EXIF dateTimeOriginal > fileCreatedAt > 默认时间。
"""
from __future__ import annotations

import math
import random as _random
from datetime import datetime, timezone

from ..utils.time_parsing import DEFAULT_SOURCE, parse_datetime, time_freshness


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def safe_float(value: object, default: float = 0.0) -> float:
    """安全转换为 float，非数字时返回 default。"""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# 查询意图关键词表
_RECENT_KEYWORDS = ("最近", "昨天", "今天", "今年", "上周")
_NOSTALGIC_KEYWORDS = ("以前", "老照片", "童年", "经典")


def intent_score(query: str) -> float:
    """根据查询关键词判定时间意图分数。

    - 包含"最近/昨天/今天/今年/上周"等近期词 → 1.0（偏向时间新鲜度）
    - 包含"以前/老照片/童年/经典"等怀旧词 → 0.0（偏向语义相似度）
    - 其他 → 0.5（中性）
    """
    for kw in _RECENT_KEYWORDS:
        if kw in query:
            return 1.0
    for kw in _NOSTALGIC_KEYWORDS:
        if kw in query:
            return 0.0
    return 0.5


def calculate_dynamic_weights(
    query: str,
    base_alpha: float,
    base_beta: float,
    gamma: float,
) -> tuple[float, float]:
    """基于查询意图动态分配 (α, β) 权重。

    公式：α_dynamic = α_base_normalized + γ * IntentScore
    其中 α_base_normalized 为归一化后的基础时间权重（保证 α+β=1 的前提下）。
    最终 clamp α 到 [0,1]，β = 1 - α。
    """
    total = base_alpha + base_beta
    alpha_norm = 0.0 if total <= 0 else base_alpha / total

    score = intent_score(query)
    alpha_dynamic = clamp(alpha_norm + gamma * score)
    beta_dynamic = 1.0 - alpha_dynamic
    return alpha_dynamic, beta_dynamic


def fuse_multimodal_query(
    query: str,
    vision_tags: list[str],
    lambda_weight: float,
) -> str:
    """多模态查询加权融合：V_final = λ * V_text + (1 - λ) * V_vision。

    Immich API 仅支持文本字符串，通过"加权拼接"在工程上模拟向量级融合：
    - 将原始 query 重复 text_repeat 次，vision_tags 拼接 1 次
    - text_repeat = max(1, round(λ / (1 - λ)))，使 text:vision ≈ λ:(1-λ)
    - λ=0.7 → query 重复 2 次（2:1 ≈ 0.67:0.33，接近 0.7:0.3）
    - λ=1.0 → 仅 query，λ=0.0 → 仅 vision_tags

    重复拼接的原理：在 BM25/TF-IDF 文本检索中，词频（TF）直接影响权重，
    重复 query N 次相当于将其在向量空间中的权重乘以 N。
    """
    query = (query or "").strip()
    seen_in_query = set(query.split())
    unique_tags: list[str] = []
    for tag in vision_tags or []:
        tag = (tag or "").strip()
        if tag and tag not in seen_in_query and tag not in unique_tags:
            unique_tags.append(tag)

    if not query and not unique_tags:
        return ""
    if not unique_tags:
        return query
    if not query:
        return " ".join(unique_tags)

    lam = clamp(lambda_weight)
    if lam >= 1.0:
        return query
    if lam <= 0.0:
        return " ".join(unique_tags)

    text_repeat = min(5, max(1, round(lam / (1.0 - lam))))
    text_part = " ".join([query] * text_repeat)
    return f"{text_part} {' '.join(unique_tags)}"


def resolve_captured_at(asset: dict, default_iso: str) -> tuple[datetime | None, str]:
    """按优先级解析拍摄时间，返回 (时间, 来源)。"""
    exif = asset.get("exifInfo") or {}
    exif_datetime = parse_datetime(exif.get("dateTimeOriginal"))
    if exif_datetime is not None:
        return exif_datetime, "exif"

    file_created = parse_datetime(asset.get("fileCreatedAt"))
    if file_created is not None:
        return file_created, "file_created"

    default_dt = parse_datetime(default_iso)
    return default_dt, DEFAULT_SOURCE


# blend 模式的 RRF 常数（rank 0 的 prior 归一化后为 1.0）。
_RRF_K = 60.0
# blend 模式时间权重上限（超过则截断，保证相关度主导）。
_BLEND_W_TIME_MAX = 0.2


def rank_results(
    assets: list[dict],
    *,
    mode: str = "immich",
    time_weight: float = 0.3,
    half_life_days: float = 365.0,
    default_iso: str = "1970-01-01T00:00:00+00:00",
    now: datetime | None = None,
) -> list[dict]:
    """按 rank_mode 计算展示字段；similarity 仅在真实 score 存在时有值。

    返回 dict 行列表（顺序即输出顺序）：
        {asset, immich_rank(0-based), final(None|float), similarity(None|float),
         freshness, captured_at, captured_at_source, similarity_source}

    - mode="immich"：输出顺序与输入（Immich 返回）顺序完全一致，final=None。
    - mode="blend"：final = (1-w)*60/(60+rank) + w*freshness，w=min(time_weight, 0.2)，
      按 final 降序稳定排序。SCENE_WEIGHTS 场景动态权重已移除，场景不参与排序。
    """
    now = now or datetime.now(timezone.utc)
    rows: list[dict] = []

    for rank, asset in enumerate(assets):
        captured_at, source = resolve_captured_at(asset, default_iso)
        freshness = time_freshness(captured_at, now=now, half_life_days=half_life_days)
        raw_score = asset.get("score")
        if raw_score is None:
            similarity = None
            similarity_source = "none"
        else:
            similarity = clamp(safe_float(raw_score))
            similarity_source = "upstream"
        rows.append(
            {
                "asset": asset,
                "immich_rank": rank,
                "final": None,
                "similarity": similarity,
                "similarity_source": similarity_source,
                "freshness": freshness,
                "captured_at": captured_at,
                "captured_at_source": source,
            }
        )

    if mode == "blend":
        w = min(max(time_weight, 0.0), _BLEND_W_TIME_MAX)
        for row in rows:
            prior = _RRF_K / (_RRF_K + row["immich_rank"])
            row["final"] = (1.0 - w) * prior + w * row["freshness"]
        rows.sort(key=lambda r: (-r["final"], r["asset"].get("id", "")))

    return rows


def apply_feedback_boost(final_score: float | None, boost: float) -> float | None:
    """将反馈加权值叠加到最终排序分数上，结果裁切到 [0, 1]。

    final_score 为 None（immich 模式未编分）时原样返回 None，不伪造分数。
    boost 为 NaN 时视为 0（防 NaN 传播到 final_score 破坏排序）；
    ±inf 保留 clamp 裁切语义（inf → 1.0，-inf → 0.0）。
    """
    if final_score is None:
        return None
    if math.isnan(boost):
        boost = 0.0
    return clamp(final_score + boost)

# ---- 排序维度切换 ----

# timezone-aware 兜底常量，用于 captured_at 为 None 时的排序占位。
_SORT_DATETIME_MIN = datetime(1, 1, 1, tzinfo=timezone.utc)
_SORT_DATETIME_MAX = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)


def _sort_key_newest(item: dict) -> datetime:
    """newest 排序键：无有效时间用 MIN（降序时排最后）。"""
    dt = item.get("captured_at")
    if isinstance(dt, datetime):
        return dt
    return _SORT_DATETIME_MIN


def _sort_key_oldest(item: dict) -> datetime:
    """oldest 排序键：无有效时间用 MAX（升序时排最后）。"""
    dt = item.get("captured_at")
    if isinstance(dt, datetime):
        return dt
    return _SORT_DATETIME_MAX


def apply_sort_by(
    items: list[dict],
    sort_by: str,
    *,
    random_seed: int = 0,
) -> list[dict]:
    """按 sort_by 维度重排搜索结果。

    - relevance: 返回原顺序（rank_assets 已排好）。
    - newest: 按 captured_at 降序，无时间排最后。
    - oldest: 按 captured_at 升序，无时间排最后。
    - random: 用 random.Random(seed) 打乱，相同 seed 产生相同顺序。

    items 中每个 dict 需含 "captured_at" 键（datetime | None）。
    不修改原列表，返回新列表。
    """
    if sort_by == "relevance":
        return list(items)

    if sort_by == "random":
        shuffled = list(items)
        _random.Random(random_seed).shuffle(shuffled)
        return shuffled

    if sort_by == "newest":
        return sorted(items, key=_sort_key_newest, reverse=True)

    if sort_by == "oldest":
        return sorted(items, key=_sort_key_oldest)

    return list(items)
