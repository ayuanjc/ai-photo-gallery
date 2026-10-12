"""二次排序算法：EXIF 拍摄时间与语义相似度的加权融合。

融合公式（权重已归一化）：
    final_score = α * time_freshness + β * similarity_score

- similarity_score 归一化到 [0, 1]（Immich 返回分数若超出该区间会被裁切）。
- time_freshness 由指数衰减得到，见 utils/time_parsing.py。
- 拍摄时间来源优先级：EXIF dateTimeOriginal > fileCreatedAt > 默认时间。
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..utils.time_parsing import DEFAULT_SOURCE, parse_datetime, time_freshness

# 各主场景动态排序权重表（alpha=时间权重，beta=语义权重，和为 1）。
# 未命中主场景时回退到调用方传入的默认权重（来源为环境变量 time_weight / similarity_weight）。
SCENE_WEIGHTS: dict[str, dict[str, float]] = {
    "people": {"alpha": 0.5, "beta": 0.5},
    "nature": {"alpha": 0.2, "beta": 0.8},
    "event": {"alpha": 0.4, "beta": 0.6},
}


def blend_scene_weights(
    scenes: list[str],
    default_alpha: float,
    default_beta: float,
) -> tuple[float, float]:
    """多场景权重加权综合：对 scenes 中每个场景取 SCENE_WEIGHTS 的 (alpha, beta) 算术平均。

    - 不在 SCENE_WEIGHTS 中的场景（含 "default" 标识）跳过。
    - scenes 为空或过滤后为空时回退 default_alpha / default_beta。
    - 平均后做归一化，保证 alpha + beta == 1。
    """
    alphas: list[float] = []
    betas: list[float] = []
    for scene in scenes or []:
        entry = SCENE_WEIGHTS.get(scene) if scene else None
        if entry is None or scene == "default":
            continue
        alphas.append(entry["alpha"])
        betas.append(entry["beta"])

    if not alphas:
        return default_alpha, default_beta

    alpha = sum(alphas) / len(alphas)
    beta = sum(betas) / len(betas)
    total = alpha + beta
    if total <= 0:
        return default_alpha, default_beta
    return alpha / total, beta / total


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _safe_float(value: object, default: float = 0.0) -> float:
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
    if total <= 0:
        alpha_norm = 0.0
    else:
        alpha_norm = base_alpha / total

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


def rank_assets(
    assets: list[dict],
    *,
    time_weight: float,
    similarity_weight: float,
    half_life_days: float,
    default_iso: str,
    now: datetime | None = None,
) -> list[tuple[dict, float, float, float, datetime | None, str]]:
    """对 Immich 返回的资产列表做加权融合排序。

    每条 asset 的融合权重按其 scenes 多场景综合选择（见 blend_scene_weights），
    scenes 为空时回退到 primary_scene 单场景，再为空则用传入的默认权重。

    返回按 final_score 降序排列的元组列表：
        (asset, final_score, similarity_score, time_freshness, captured_at, captured_at_source)
    """
    now = now or datetime.now(timezone.utc)
    scored: list[tuple[dict, float, float, float, datetime | None, str]] = []

    for asset in assets:
        # 按 asset 的多场景综合选权重；scenes 为空时回退到 primary_scene 单场景兜底。
        alpha, beta = blend_scene_weights(
            asset.get("scenes") or [asset.get("primary_scene")],
            default_alpha=time_weight,
            default_beta=similarity_weight,
        )
        similarity = clamp(_safe_float(asset.get("score", 1.0), default=1.0))
        captured_at, source = resolve_captured_at(asset, default_iso)
        freshness = time_freshness(captured_at, now=now, half_life_days=half_life_days)
        final = alpha * freshness + beta * similarity
        scored.append((asset, final, similarity, freshness, captured_at, source))

    scored.sort(key=lambda item: item[1], reverse=True)
    return scored
