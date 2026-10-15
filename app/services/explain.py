"""搜索结果可解释性：为每条搜索结果生成"为什么排在这里"的可读解释。

解释结构（纯 dict，不做 Pydantic 强校验，保证解释失败不阻塞搜索）：
    {
        "reasons": ["语义召回名次靠前", "拍摄时间较新", "命中场景：自然"],
        "semantic_score": 0.85,
        "time_score": 0.72,
        "matched_scenes": ["nature"],
        "weights_used": {"alpha": 0.3, "beta": 0.7},
    }

注意：semantic_score 实际是"召回名次分"（按召回排名线性补分 1.0 - idx/total），
非真实向量相似度。字段名保留 semantic_score 以兼容前端，内部口径为名次分。

reasons 生成规则：
    - rank_score >= 0.8 → "语义召回名次靠前"
    - rank_score >= 0.4 → "语义召回名次居中"
    - time_score >= 0.7 → "拍摄时间较新"
    - matched_scenes 非空 → "命中场景：{场景中文名}"
    - 都不满足 → "综合得分"

场景中文名从 app/scene/scene_dict.json 读取，未知场景自动过滤。
"""
from __future__ import annotations

import logging
import math
from typing import Any

from ..scene.dictionary import get_scene_dict

logger = logging.getLogger(__name__)

_SEMANTIC_THRESHOLD_HIGH = 0.8
_SEMANTIC_THRESHOLD_MID = 0.4
_TIME_THRESHOLD = 0.7


def _safe_score(value: Any) -> float:
    """安全转分数：非数字 / NaN / Infinity 返回 0.0。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(f) or math.isinf(f):
        return 0.0
    return f


def _weights_pair(weights: Any) -> tuple[float, float]:
    """从 weights 提取 (alpha, beta)，异常返回 (0.0, 0.0)。

    兼容 tuple/list（(alpha, beta)）与 dict（{"alpha":.., "beta":..}）。
    """
    if isinstance(weights, dict):
        return _safe_score(weights.get("alpha", 0.0)), _safe_score(weights.get("beta", 0.0))
    if isinstance(weights, (tuple, list)) and len(weights) >= 2:
        return _safe_score(weights[0]), _safe_score(weights[1])
    return 0.0, 0.0


def _collect_scene_candidates(asset: Any, scene_tags: Any) -> list[str]:
    """收集场景候选（场景 key），优先 scene_tags，回退 asset。"""
    if isinstance(scene_tags, dict):
        scenes = scene_tags.get("scenes") or []
        if not scenes and scene_tags.get("primary_scene"):
            scenes = [scene_tags["primary_scene"]]
    elif scene_tags is not None:
        scenes = list(getattr(scene_tags, "scenes", None) or [])
        if not scenes and getattr(scene_tags, "primary_scene", None):
            scenes = [getattr(scene_tags, "primary_scene")]
    else:
        scenes = []

    if not scenes and isinstance(asset, dict):
        scenes = asset.get("scenes") or []
        if not scenes and asset.get("primary_scene"):
            scenes = [asset["primary_scene"]]

    return [s for s in scenes if isinstance(s, str) and s and s != "default"]


def _matched_scenes(asset: Any, scene_tags: Any) -> list[str]:
    """提取白名单内的大场景 key 列表（去重、保序）。"""
    try:
        scene_dict = get_scene_dict()
    except Exception as exc:  # 词典加载失败不应阻断解释
        logger.warning("场景词典加载失败，忽略场景解释: %s", exc)
        return []

    matched: list[str] = []
    seen: set[str] = set()
    for scene_key in _collect_scene_candidates(asset, scene_tags):
        if scene_key in seen or not scene_dict.is_valid_scene(scene_key):
            continue
        seen.add(scene_key)
        matched.append(scene_key)
    return matched


def _scene_label(scene_key: str) -> str:
    """返回场景中文名，未收录返回 key 兜底。"""
    try:
        entry = get_scene_dict().scenes.get(scene_key)
        if entry and entry.get("label"):
            return str(entry["label"])
    except Exception:
        pass
    return scene_key


def build_explanation(
    asset: Any,
    sem: Any,
    time_s: Any,
    weights: Any,
    scene_tags: Any,
) -> dict[str, Any]:
    """生成单条搜索结果的排序解释。任何异常都降级返回，不抛。"""
    try:
        semantic_score = _safe_score(sem)
        time_score = _safe_score(time_s)
        alpha, beta = _weights_pair(weights)
        matched_scenes = _matched_scenes(asset, scene_tags)

        reasons: list[str] = []
        if semantic_score >= _SEMANTIC_THRESHOLD_HIGH:
            reasons.append("语义召回名次靠前")
        elif semantic_score >= _SEMANTIC_THRESHOLD_MID:
            reasons.append("语义召回名次居中")
        if time_score >= _TIME_THRESHOLD:
            reasons.append("拍摄时间较新")
        for scene_key in matched_scenes:
            reasons.append(f"命中场景：{_scene_label(scene_key)}")
        if not reasons:
            reasons.append("综合得分")

        return {
            "reasons": reasons,
            "semantic_score": round(semantic_score, 4),
            "rank_score": round(semantic_score, 4),
            "time_score": round(time_score, 4),
            "matched_scenes": matched_scenes,
            "weights_used": {"alpha": round(alpha, 4), "beta": round(beta, 4)},
        }
    except Exception as exc:
        logger.warning("解释生成失败，返回降级解释: %s", exc)
        return {
            "reasons": ["综合得分"],
            "semantic_score": 0.0,
            "rank_score": 0.0,
            "time_score": 0.0,
            "matched_scenes": [],
            "weights_used": {"alpha": 0.0, "beta": 0.0},
        }