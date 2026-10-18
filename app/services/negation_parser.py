"""否定语义解析器：从查询词中提取否定前缀与否定词。

支持前缀：不要 / 没有 / 不含 / 排除 / -
示例：
    "海边 不要人"       → ("海边", ["人"])
    "海边不要人"        → ("海边", ["人"])
    "海边 不要 人"      → ("海边", ["人"])
    "海边 不要人 排除动物" → ("海边", ["人", "动物"])
    "海边"              → ("海边", [])

设计原则：
    - 纯规则解析，不依赖模型，不引入新依赖。
    - 解析失败时返回原 query 与空否定列表，不报错。
    - 0 个否定词时与改动前行为完全一致。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_PREFIXES: list[str] = ["不要", "没有", "不含", "排除", "-"]


def parse_negations(
    query: str, prefixes: list[str] | None = None
) -> tuple[str, list[str]]:
    """从查询词中解析否定前缀，返回 (清理后正向 query, 否定词列表)。

    Args:
        query: 原始查询词，可能包含否定前缀。
        prefixes: 否定前缀列表，为 None 时使用 DEFAULT_PREFIXES。

    Returns:
        (positive_query, negation_tokens)
        - positive_query: 移除否定前缀及否定词后的正向查询词。
        - negation_tokens: 提取出的否定词列表（原始文本，未归一化）。
    """
    if not query or not query.strip():
        return query, []

    if prefixes is None:
        active_prefixes = DEFAULT_PREFIXES
    else:
        active_prefixes = prefixes
    active_prefixes = sorted(set(active_prefixes), key=len, reverse=True)
    if not active_prefixes:
        return query, []

    occurrences = _find_prefix_occurrences(query, active_prefixes)
    if not occurrences:
        return query, []

    consumed_ranges: list[tuple[int, int]] = []
    negations: list[str] = []

    sorted_occurrences = sorted(occurrences)
    for i, (oc_pos, oc_len) in enumerate(sorted_occurrences):
        after_pos = oc_pos + oc_len
        token_start = after_pos
        while token_start < len(query) and query[token_start] == " ":
            token_start += 1

        if token_start >= len(query):
            consumed_ranges.append((oc_pos, after_pos))
            continue

        next_oc_start = (
            sorted_occurrences[i + 1][0]
            if i + 1 < len(sorted_occurrences)
            else len(query)
        )

        token_end = token_start
        while (
            token_end < len(query)
            and query[token_end] != " "
            and token_end < next_oc_start
        ):
            token_end += 1

        token = query[token_start:token_end].strip()
        if token:
            negations.append(token)
        consumed_ranges.append((oc_pos, token_end))

    positive = _build_positive(query, consumed_ranges)
    return positive, negations


def _find_prefix_occurrences(
    query: str, sorted_prefixes: list[str]
) -> list[tuple[int, int]]:
    """扫描 query 中所有否定前缀出现位置（非重叠，先到先得）。"""
    occurrences: list[tuple[int, int]] = []
    pos = 0
    while pos < len(query):
        found = False
        for prefix in sorted_prefixes:
            if query[pos:].startswith(prefix):
                occurrences.append((pos, len(prefix)))
                pos += len(prefix)
                found = True
                break
        if not found:
            pos += 1
    return occurrences


def _build_positive(query: str, consumed_ranges: list[tuple[int, int]]) -> str:
    """从 query 中移除已消费范围，拼接剩余正向文本。"""
    if not consumed_ranges:
        return query

    sorted_ranges = sorted(consumed_ranges)
    parts: list[str] = []
    last_end = 0
    for start, end in sorted_ranges:
        if start > last_end:
            chunk = query[last_end:start].strip()
            if chunk:
                parts.append(chunk)
        last_end = max(last_end, end)
    if last_end < len(query):
        chunk = query[last_end:].strip()
        if chunk:
            parts.append(chunk)

    return " ".join(parts)

def _resolve_negation_to_scenes(token: str, alias_map: dict[str, str]) -> set[str]:
    """将否定词解析为可能匹配的大场景 key 集合。

    匹配策略（复用场景词典别名映射）：
        1. 归一化后直接命中别名映射 → 对应大场景 key。
        2. 否定词是某场景 label 的子串（如 "人" ⊂ "人物"）→ 对应大场景 key。
        3. 某场景 label 是否定词的子串 → 对应大场景 key。
    """
    from ..scene.dictionary import normalize

    norm = normalize(token)
    matched: set[str] = set()

    if norm in alias_map:
        matched.add(alias_map[norm])

    for alias_token, scene_key in alias_map.items():
        if not alias_token or alias_token == norm:
            continue
        if norm in alias_token or alias_token in norm:
            matched.add(scene_key)

    return matched


def _asset_matches_negation(
    asset: dict, token: str, alias_map: dict[str, str], sub_scene_keys: frozenset[str]
) -> bool:
    """判断单条 asset 的场景标签是否命中否定词。

    命中条件（任一满足即命中）：
        - 否定词解析到大场景 key，且 asset 的 primary_scene 或 scenes 包含该 key。
        - 否定词归一化后匹配 asset 的某个 sub_scene。
    """
    from ..scene.dictionary import normalize

    scene_tags = asset.get("scene_tags")
    primary_scene = asset.get("primary_scene")
    scenes_list = getattr(scene_tags, "scenes", None) or []
    sub_scenes_list = getattr(scene_tags, "sub_scenes", None) or []

    norm = normalize(token)

    matched_scene_keys = _resolve_negation_to_scenes(token, alias_map)
    if matched_scene_keys:
        if primary_scene and primary_scene in matched_scene_keys:
            return True
        for sk in scenes_list:
            if sk in matched_scene_keys:
                return True

    for ss in sub_scenes_list:
        if norm == normalize(ss):
            return True

    return False


def filter_by_negations(assets: list[dict], negations: list[str]) -> list[dict]:
    """过滤掉场景标签命中否定词的 asset。

    Args:
        assets: 已附加 scene_tags 的 asset 列表。
        negations: 否定词列表（原始文本）。

    Returns:
        过滤后的 asset 列表（保留原顺序）。negations 为空时原样返回。
    """
    if not negations or not assets:
        return assets

    try:
        from ..scene.dictionary import get_scene_dict

        scene_dict = get_scene_dict()
        alias_map = scene_dict.scene_alias_map
        sub_scene_keys = scene_dict.sub_scene_keys
    except Exception as exc:
        logger.warning("否定过滤加载场景词典失败，跳过过滤: %s", exc)
        return assets

    result: list[dict] = []
    for asset in assets:
        hit = False
        for token in negations:
            try:
                if _asset_matches_negation(asset, token, alias_map, sub_scene_keys):
                    hit = True
                    break
            except Exception as exc:
                logger.warning("否定匹配异常，跳过该否定词 token=%s: %s", token, exc)
                continue
        if not hit:
            result.append(asset)
    return result
