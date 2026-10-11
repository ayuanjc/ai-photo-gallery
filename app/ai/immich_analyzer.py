"""基于 Immich 人物分组的同人判定实现。

第一版复用 Immich 已有的人脸/人物数据，不引入新模型：
    - 人物判定：取两图资产(asset)的 people 分组 id 比对是否同一人。
    - 视觉相似度：以参考图 asset id 做相似搜索，从结果中取候选图 score。

降级策略：
    - 业务失败（无脸、URL 无法解析、资产 4xx 等）→ 降级返回 unrelated，不抛异常。
    - 上游基础设施故障（连接失败/超时）→ 抛 AI_ANALYZER_*，映射 502/504。
"""
from __future__ import annotations

import asyncio
import logging
import re

from ..core.config import Settings
from ..core.errors import (
    AIAnalyzerTimeoutError,
    AIAnalyzerUnavailableError,
    UpstreamError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from ..services.immich import ImmichClient
from .base import AnalysisResult, ImageAnalyzer, categorize

logger = logging.getLogger(__name__)

# Immich 资产 URL 中 asset id 的提取模式（如 /api/assets/{uuid}/thumbnail）。
_ASSET_ID_RE = re.compile(r"/assets/([0-9a-fA-F-]{8,})")


class ImmichAnalyzer(ImageAnalyzer):
    """复用 Immich 人物分组与语义搜索的 AI 分析器。"""

    name = "immich"

    def __init__(self, settings: Settings, immich: ImmichClient) -> None:
        self._settings = settings
        self._immich = immich
        self._timeout = settings.ai_analyze_timeout_seconds
        self._max_retries = max(0, settings.ai_analyzer_max_retries)
        self._backoff = max(0.0, settings.ai_retry_backoff_seconds)

    async def analyze(self, reference_url: str, candidate_url: str) -> AnalysisResult:
        for attempt in range(self._max_retries + 1):
            try:
                return await asyncio.wait_for(
                    self._analyze(reference_url, candidate_url),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError as exc:
                if attempt >= self._max_retries:
                    raise AIAnalyzerTimeoutError("AI 分析超时") from exc
                await asyncio.sleep(self._backoff * (2 ** attempt))
            except (AIAnalyzerUnavailableError, AIAnalyzerTimeoutError):
                if attempt >= self._max_retries:
                    raise
                await asyncio.sleep(self._backoff * (2 ** attempt))
            except Exception as exc:  # provider 未知异常：记录堆栈后降级，避免阻塞分析链
                logger.error("AI 分析 provider 异常，降级处理: %s", exc, exc_info=True)
                return self._degrade(str(exc))
        raise AIAnalyzerUnavailableError("AI 分析重试耗尽")

    async def _analyze(self, reference_url: str, candidate_url: str) -> AnalysisResult:
        ref_id = self._extract_asset_id(reference_url)
        cand_id = self._extract_asset_id(candidate_url)
        if not ref_id or not cand_id:
            return self._degrade("无法从 URL 解析 Immich asset id")

        ref_person, cand_person, similarity = await asyncio.gather(
            self._person_id(ref_id),
            self._person_id(cand_id),
            self._similarity(ref_id, cand_id),
        )

        if ref_person is not None and cand_person is not None:
            same_person = ref_person == cand_person
            person_confidence = 1.0
            person_note = "检测为同一人" if same_person else "检测到不同人物"
        else:
            same_person = False
            person_confidence = 0.0
            person_note = "至少一张图未检测到人脸"

        category, base_reason = categorize(
            same_person,
            person_confidence,
            similarity,
            self._settings.ai_person_confidence_threshold,
            self._settings.ai_similarity_threshold,
        )
        return AnalysisResult(
            same_person=same_person,
            person_confidence=round(person_confidence, 6),
            visual_similarity=round(similarity, 6),
            category=category,
            reason=f"{person_note}；{base_reason}",
        )

    async def _person_id(self, asset_id: str) -> str | None:
        """返回资产所属人物分组 id；无脸或资产不存在返回 None。"""
        try:
            asset = await self._immich.get_asset(asset_id)
        except UpstreamTimeoutError as exc:
            raise AIAnalyzerTimeoutError("Immich 获取资产超时") from exc
        except UpstreamUnavailableError as exc:
            raise AIAnalyzerUnavailableError("Immich 获取资产不可用") from exc
        except UpstreamError:
            return None  # 4xx 等视为该资产无人脸信息

        people = asset.get("people") or []
        for person in people:
            if isinstance(person, dict):
                pid = person.get("id") or person.get("personId")
                if pid:
                    return str(pid)
        return None

    async def _similarity(self, ref_id: str, cand_id: str) -> float:
        """以参考图做相似搜索，取候选图 score；失败兜底 0.0。"""
        try:
            items = await self._immich.similar_assets(ref_id, k=50)
        except UpstreamTimeoutError as exc:
            raise AIAnalyzerTimeoutError("Immich 相似搜索超时") from exc
        except UpstreamUnavailableError as exc:
            raise AIAnalyzerUnavailableError("Immich 相似搜索不可用") from exc
        except UpstreamError:
            return 0.0

        for item in items:
            if str(item.get("id")) == str(cand_id):
                return float(item.get("score", 0.0) or 0.0)
        return 0.0

    async def health_check(self) -> bool:
        try:
            await self._immich.list_people()
            return True
        except Exception:
            return False

    @staticmethod
    def _extract_asset_id(url: str) -> str | None:
        m = _ASSET_ID_RE.search(url)
        return m.group(1) if m else None

    @staticmethod
    def _degrade(reason: str) -> AnalysisResult:
        return AnalysisResult(
            same_person=False,
            person_confidence=0.0,
            visual_similarity=0.0,
            category="unrelated",
            reason=f"分析降级：{reason}",
        )