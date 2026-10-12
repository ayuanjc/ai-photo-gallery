"""复合条件搜索编排服务：人物 + 地点 + 语义 → 贝叶斯降级检索。

流程:
    1. 并发按 person_id 和 location 在 Immich 中过滤候选 asset_id 列表。
    2. 取交集后用候选 asset_id 调用语义搜索（严格模式）。
    3. 如果严格模式结果为空，触发贝叶斯降级检索：
       分别按每个条件独立搜索，然后用加权评分合并。
       Score_multi = w1 * S_person + w2 * S_location + w3 * S_query
       优先返回满足高权重条件（person_id=0.5 > location=0.3 > query=0.2）的近似结果。
    4. 按加权评分降序排列返回。

降级逻辑说明:
    当 person_id + location + query 三个条件严格交集为空时，系统自动放宽条件。
    放宽策略不是简单去掉某个条件，而是分别按每个条件独立搜索，
    再用贝叶斯加权评分合并结果。这样即使某个条件完全无命中，
    其他条件的部分命中仍能贡献到最终评分，避免"全有或全无"的脆弱性。
    权重越高的条件（如 person_id=0.5）对最终排序影响越大，
    体现"优先返回满足高权重条件的近似结果"的设计意图。
"""
from __future__ import annotations

import asyncio
import logging

from ..core.config import Settings
from ..models.request import MultiConditionSearchRequest
from ..models.response import MultiConditionResultItem, MultiConditionSearchResponse
from .immich import ImmichClient
from .ranking import _safe_float, clamp

logger = logging.getLogger(__name__)


class MultiConditionSearchService:
    """复合条件搜索编排服务。"""

    def __init__(self, settings: Settings, immich: ImmichClient) -> None:
        self._settings = settings
        self._immich = immich

    async def search(self, request: MultiConditionSearchRequest) -> MultiConditionSearchResponse:
        """执行复合条件搜索，严格模式优先，空结果时降级。"""
        warnings: list[str] = []

        # ---- 严格模式：同时满足所有提供的条件 ----
        strict_results = await self._strict_search(request, warnings)
        if strict_results:
            return MultiConditionSearchResponse(
                total=len(strict_results),
                degraded=False,
                warnings=warnings,
                results=strict_results,
            )

        # ---- 降级模式：贝叶斯加权评分合并 ----
        degraded_results, degraded_conditions = await self._degraded_search(request, warnings)
        return MultiConditionSearchResponse(
            total=len(degraded_results),
            degraded=True,
            degraded_conditions=degraded_conditions,
            warnings=warnings,
            results=degraded_results,
        )

    # ---- 严格模式 ----

    async def _strict_search(
        self,
        request: MultiConditionSearchRequest,
        warnings: list[str],
    ) -> list[MultiConditionResultItem]:
        """同时满足所有提供的条件（交集），有结果则返回。"""
        # 并发获取 person 和 location 的候选 asset_id 列表
        person_assets, location_assets = await self._fetch_candidates(
            request.person_id, request.location, warnings
        )

        # 计算交集候选 asset_id
        candidate_ids = self._intersect_candidate_ids(
            person_assets, location_assets, request.person_id is not None, request.location is not None
        )

        # 如果有候选 asset_id 但没有 query，直接返回候选列表
        if candidate_ids and not request.query:
            return self._build_results_from_candidates(candidate_ids, person_assets, location_assets)

        # 如果有 query，用候选 asset_id 调用语义搜索
        if request.query:
            # 有 person/location 条件但交集为空 → 严格模式无结果，触发降级
            has_filter = request.person_id is not None or request.location is not None
            if has_filter and not candidate_ids:
                return []
            query_assets = await self._safe_smart_search(
                request.query, request.limit, candidate_ids or None, warnings
            )
            if query_assets:
                return self._build_strict_results(
                    query_assets, person_assets, location_assets, request
                )

        return []

    async def _fetch_candidates(
        self,
        person_id: str | None,
        location: str | None,
        warnings: list[str],
    ) -> tuple[list[dict], list[dict]]:
        """并发获取 person 和 location 的候选资产列表。"""
        person_task = self._safe_get_assets_by_person(person_id, warnings) if person_id else _empty()
        location_task = self._safe_search_by_location(location, warnings) if location else _empty()
        results = await asyncio.gather(person_task, location_task)
        return results[0], results[1]

    async def _safe_get_assets_by_person(
        self, person_id: str, warnings: list[str]
    ) -> list[dict]:
        try:
            return await self._immich.get_assets_by_person(person_id)
        except Exception as exc:
            logger.warning("人物过滤失败 person_id=%s err=%s", person_id, exc)
            warnings.append(f"人物过滤失败: {exc}")
            return []

    async def _safe_search_by_location(
        self, location: str, warnings: list[str]
    ) -> list[dict]:
        try:
            return await self._immich.search_by_location(location)
        except Exception as exc:
            logger.warning("地点过滤失败 location=%s err=%s", location, exc)
            warnings.append(f"地点过滤失败: {exc}")
            return []

    async def _safe_smart_search(
        self,
        query: str,
        limit: int,
        asset_ids: list[str] | None,
        warnings: list[str],
    ) -> list[dict]:
        try:
            return await self._immich.smart_search(query, k=limit, asset_ids=asset_ids)
        except Exception as exc:
            logger.warning("语义搜索失败 query=%s err=%s", query, exc)
            warnings.append(f"语义搜索失败: {exc}")
            return []

    @staticmethod
    def _intersect_candidate_ids(
        person_assets: list[dict],
        location_assets: list[dict],
        has_person: bool,
        has_location: bool,
    ) -> list[str]:
        """计算 person 和 location 候选 asset_id 的交集。"""
        person_ids = {a["id"] for a in person_assets if "id" in a}
        location_ids = {a["id"] for a in location_assets if "id" in a}

        if has_person and has_location:
            return list(person_ids & location_ids)
        if has_person:
            return list(person_ids)
        if has_location:
            return list(location_ids)
        return []

    # ---- 降级模式（贝叶斯加权评分） ----

    async def _degraded_search(
        self,
        request: MultiConditionSearchRequest,
        warnings: list[str],
    ) -> tuple[list[MultiConditionResultItem], list[str]]:
        """贝叶斯降级检索：分别按每个条件独立搜索，加权评分合并。

        Score_multi = w1 * S_person + w2 * S_location + w3 * S_query

        降级时放宽的条件按权重从低到高排序记录。
        """
        w_person = self._settings.multi_condition_weight_person
        w_location = self._settings.multi_condition_weight_location
        w_query = self._settings.multi_condition_weight_query

        # 并发执行三个独立搜索
        person_task = self._safe_get_assets_by_person(request.person_id, warnings) if request.person_id else _empty()
        location_task = self._safe_search_by_location(request.location, warnings) if request.location else _empty()
        query_task = self._safe_smart_search(request.query, request.limit, None, warnings) if request.query else _empty()

        person_assets, location_assets, query_assets = await asyncio.gather(
            person_task, location_task, query_task
        )

        # 构建每个条件的 asset_id → score 映射
        person_scores = {a["id"]: clamp(_safe_float(a.get("score", 0.0))) for a in person_assets if "id" in a}
        location_scores = {a["id"]: clamp(_safe_float(a.get("score", 0.0))) for a in location_assets if "id" in a}
        query_scores = {a["id"]: clamp(_safe_float(a.get("score", 0.0))) for a in query_assets if "id" in a}

        # 合并所有 asset_id
        all_ids = set(person_scores) | set(location_scores) | set(query_scores)

        # 计算加权评分并构建结果
        results: list[MultiConditionResultItem] = []
        for asset_id in all_ids:
            s_person = person_scores.get(asset_id, 0.0)
            s_location = location_scores.get(asset_id, 0.0)
            s_query = query_scores.get(asset_id, 0.0)

            multi_score = w_person * s_person + w_location * s_location + w_query * s_query
            if multi_score <= 0:
                continue

            matched: list[str] = []
            if asset_id in person_scores:
                matched.append("person")
            if asset_id in location_scores:
                matched.append("location")
            if asset_id in query_scores:
                matched.append("query")

            results.append(
                MultiConditionResultItem(
                    asset_id=asset_id,
                    thumbnail_url=self._immich.thumbnail_url(asset_id),
                    multi_score=round(multi_score, 6),
                    matched_conditions=matched,
                    person_score=round(s_person, 6),
                    location_score=round(s_location, 6),
                    query_score=round(s_query, 6),
                )
            )

        # 按加权评分降序排列
        results.sort(key=lambda r: r.multi_score, reverse=True)

        # 限制返回数量
        results = results[: request.limit]

        # 记录降级放宽的条件（按权重从低到高排序）
        degraded_conditions: list[str] = []
        if request.query:
            degraded_conditions.append("query")
        if request.location:
            degraded_conditions.append("location")
        if request.person_id:
            degraded_conditions.append("person")

        if not results:
            warnings.append("降级检索仍未找到匹配结果")

        return results, degraded_conditions

    # ---- 结果构建 ----

    def _build_strict_results(
        self,
        query_assets: list[dict],
        person_assets: list[dict],
        location_assets: list[dict],
        request: MultiConditionSearchRequest,
    ) -> list[MultiConditionResultItem]:
        """严格模式结果：所有条件都命中，multi_score = query_score。"""
        person_ids = {a["id"] for a in person_assets if "id" in a}
        location_ids = {a["id"] for a in location_assets if "id" in a}

        results: list[MultiConditionResultItem] = []
        for asset in query_assets:
            asset_id = asset.get("id", "")
            if not asset_id:
                continue
            score = clamp(_safe_float(asset.get("score", 0.0)))
            matched: list[str] = []
            if request.person_id and asset_id in person_ids:
                matched.append("person")
            if request.location and asset_id in location_ids:
                matched.append("location")
            if request.query:
                matched.append("query")

            results.append(
                MultiConditionResultItem(
                    asset_id=asset_id,
                    thumbnail_url=self._immich.thumbnail_url(asset_id),
                    multi_score=round(score, 6),
                    matched_conditions=matched,
                    person_score=round(score, 6) if request.person_id else 0.0,
                    location_score=round(score, 6) if request.location else 0.0,
                    query_score=round(score, 6),
                )
            )
        return results[: request.limit]

    def _build_results_from_candidates(
        self,
        candidate_ids: list[str],
        person_assets: list[dict],
        location_assets: list[dict],
    ) -> list[MultiConditionResultItem]:
        """无 query 时直接从候选 asset_id 构建结果。"""
        person_scores = {a["id"]: clamp(_safe_float(a.get("score", 0.0))) for a in person_assets if "id" in a}
        location_scores = {a["id"]: clamp(_safe_float(a.get("score", 0.0))) for a in location_assets if "id" in a}

        results: list[MultiConditionResultItem] = []
        for asset_id in candidate_ids:
            s_person = person_scores.get(asset_id, 0.0)
            s_location = location_scores.get(asset_id, 0.0)
            matched: list[str] = []
            if asset_id in person_scores:
                matched.append("person")
            if asset_id in location_scores:
                matched.append("location")

            w_person = self._settings.multi_condition_weight_person
            w_location = self._settings.multi_condition_weight_location
            multi_score = w_person * s_person + w_location * s_location

            results.append(
                MultiConditionResultItem(
                    asset_id=asset_id,
                    thumbnail_url=self._immich.thumbnail_url(asset_id),
                    multi_score=round(multi_score, 6),
                    matched_conditions=matched,
                    person_score=round(s_person, 6),
                    location_score=round(s_location, 6),
                )
            )
        results.sort(key=lambda r: r.multi_score, reverse=True)
        return results


async def _empty() -> list[dict]:
    return []
