"""人脸分类编排服务：检测 → 入库 → 搜索 → 并查集聚类 → 分组输出。

流程:
    1. 并发对每张图片调 FRS 人脸检测，得到人脸列表。
    2. 创建临时人脸库，将所有人脸入库获得 face_id。
    3. 对每个 face_id 在库中搜索相似人脸。
    4. 基于相似度阈值用并查集（Union-Find）聚类。
    5. 删除临时人脸库，按聚类结果分组返回。

错误降级策略:
    - 单张图片检测失败 → 标记为 unclassified，不阻塞整体。
    - 单个人脸入库/搜索失败 → 跳过该人脸，记录 warning。
    - 人脸库创建/删除失败 → 记录 warning，尽量继续。
"""
from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ..core.config import Settings
from ..core.errors import VisionError
from ..models.face import (
    BoundingBox,
    FaceClassifyRequest,
    FaceClassifyResponse,
    FaceGroup,
    FaceInfo,
)
from .ranking import clamp
from .vision.huawei_face import HuaweiFaceClient

logger = logging.getLogger(__name__)


def calculate_gaussian_probability(similarity: float, sigma: float) -> float:
    """基于高斯核计算两张人脸属于同一人的概率。

    公式：P(same_person) = exp(-((1 - Sim)^2) / (2 * sigma^2))

    - Sim=1.0 → P=1.0（完全相同）
    - Sim 越低 → P 越小（指数衰减）
    - sigma 控制衰减速度：sigma 越大越宽容，越小越严格
    """
    if sigma <= 0:
        return 1.0 if similarity >= 1.0 else 0.0
    distance = 1.0 - clamp(similarity)
    return math.exp(-(distance * distance) / (2.0 * sigma * sigma))


@dataclass
class _DetectedFace:
    """单个人脸的内部表示。"""

    image_url: str
    face_index: int
    bounding_box: dict[str, Any] | None = None
    face_id: str = ""
    similarity_to_rep: float = 1.0


class UnionFind:
    """并查集（带路径压缩与按秩合并）。"""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))
        self._rank = [0] * n

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self._rank[rx] < self._rank[ry]:
            rx, ry = ry, rx
        self._parent[ry] = rx
        if self._rank[rx] == self._rank[ry]:
            self._rank[rx] += 1


class FaceService:
    """人脸分类编排服务。"""

    def __init__(self, settings: Settings, face_client: HuaweiFaceClient) -> None:
        self._settings = settings
        self._face = face_client

    async def classify(self, request: FaceClassifyRequest) -> FaceClassifyResponse:
        urls = [str(u) for u in request.image_urls]
        warnings: list[str] = []

        # 1. 并发人脸检测
        detections = await self._detect_all(urls, request.max_faces, warnings)
        all_faces, unclassified = self._collect_faces(urls, detections)

        if not all_faces:
            return FaceClassifyResponse(
                total_images=len(urls),
                total_faces=0,
                group_count=0,
                unclassified_images=unclassified,
                warnings=warnings,
            )

        # 2. 创建临时人脸库 → 入库 → 搜索 → 聚类
        face_set_name = self._gen_face_set_name()
        created = await self._safe_create_face_set(face_set_name, len(all_faces), warnings)

        if not created:
            return FaceClassifyResponse(
                total_images=len(urls),
                total_faces=len(all_faces),
                group_count=0,
                unclassified_images=unclassified + [f.image_url for f in all_faces],
                warnings=warnings,
            )

        search_results: dict[str, dict[str, float]] = {}
        try:
            await self._add_all_faces(face_set_name, all_faces, warnings)
            search_results = await self._search_all_faces(
                face_set_name,
                all_faces,
                request.similarity_threshold,
                warnings,
            )
        finally:
            await self._face.delete_face_set(face_set_name)

        # 3. 并查集聚类（高斯概率软聚类）
        groups = self._cluster(
            all_faces,
            search_results,
            self._settings.face_gaussian_sigma,
            self._settings.face_probability_threshold,
        )

        # 4. 组装响应
        return self._build_response(
            urls, all_faces, groups, search_results, unclassified, warnings, request
        )

    # ---- 检测 ----

    async def _detect_all(
        self,
        urls: list[str],
        max_face: int,
        warnings: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        sem = asyncio.Semaphore(self._settings.huawei_face_max_concurrent)
        results: dict[str, list[dict[str, Any]]] = {}

        async def _detect(url: str) -> None:
            async with sem:
                try:
                    faces = await self._face.detect_faces(url, max_face=max_face)
                    results[url] = faces
                except (VisionError, ValueError, KeyError, TypeError) as exc:
                    logger.warning("人脸检测失败 url=%s err=%s", url, exc)
                    warnings.append(f"人脸检测失败: {url}")
                    results[url] = []

        await asyncio.gather(*(_detect(u) for u in urls))
        return results

    @staticmethod
    def _collect_faces(
        urls: list[str],
        detections: dict[str, list[dict[str, Any]]],
    ) -> tuple[list[_DetectedFace], list[str]]:
        all_faces: list[_DetectedFace] = []
        unclassified: list[str] = []
        for url in urls:
            faces = detections.get(url, [])
            if not faces:
                unclassified.append(url)
                continue
            for idx, face in enumerate(faces):
                all_faces.append(
                    _DetectedFace(
                        image_url=url,
                        face_index=idx,
                        bounding_box=face.get("bounding_box"),
                    )
                )
        return all_faces, unclassified

    # ---- 人脸库管理 ----

    def _gen_face_set_name(self) -> str:
        return f"{self._settings.huawei_face_set_prefix}{uuid.uuid4().hex[:12]}"

    async def _safe_create_face_set(
        self,
        name: str,
        face_count: int,
        warnings: list[str],
    ) -> bool:
        capacity = max(face_count * 2, 100)  # FRS 最低容量 100，按 2 倍冗余预留
        try:
            await self._face.create_face_set(name, capacity=capacity)
            return True
        except (VisionError, ValueError, KeyError, TypeError) as exc:
            logger.warning("创建人脸库 %s 失败: %s", name, exc)
            warnings.append(f"创建人脸库失败: {exc}")
            return False

    async def _add_all_faces(
        self,
        face_set_name: str,
        all_faces: list[_DetectedFace],
        warnings: list[str],
    ) -> None:
        sem = asyncio.Semaphore(self._settings.huawei_face_max_concurrent)

        async def _add(idx: int) -> None:
            async with sem:
                face = all_faces[idx]
                external_id = f"{face.image_url}#{face.face_index}"
                try:
                    face.face_id = await self._face.add_face_by_url(
                        face_set_name, face.image_url, external_id
                    )
                except (VisionError, ValueError, KeyError, TypeError) as exc:
                    logger.warning("添加人脸失败 idx=%d err=%s", idx, exc)
                    warnings.append(f"添加人脸失败: {face.image_url}")

        await asyncio.gather(*(_add(i) for i in range(len(all_faces))))

    async def _search_all_faces(
        self,
        face_set_name: str,
        all_faces: list[_DetectedFace],
        threshold: float,
        warnings: list[str],
    ) -> dict[str, dict[str, float]]:
        sem = asyncio.Semaphore(self._settings.huawei_face_max_concurrent)
        results: dict[str, dict[str, float]] = {}
        top_n = self._settings.huawei_face_search_top_n

        async def _search(face: _DetectedFace) -> None:
            if not face.face_id:
                return
            async with sem:
                try:
                    matches = await self._face.search_face_by_id(
                        face_set_name, face.face_id, top_n=top_n, threshold=threshold
                    )
                    results[face.face_id] = {
                        str(m.get("face_id", "")): clamp(float(m.get("similarity", 0.0)))
                        for m in matches
                    }
                except (VisionError, ValueError, KeyError, TypeError) as exc:
                    logger.warning("搜索人脸失败 face_id=%s err=%s", face.face_id, exc)
                    warnings.append(f"搜索人脸失败: {face.image_url}")

        await asyncio.gather(*(_search(f) for f in all_faces))
        return results

    # ---- 聚类 ----

    @staticmethod
    def _cluster(
        all_faces: list[_DetectedFace],
        search_results: dict[str, dict[str, float]],
        sigma: float,
        prob_threshold: float,
    ) -> dict[int, list[int]]:
        """并查集聚类，返回 {root_index: [member_indices]}。

        合并条件：高斯概率 P(same_person) >= prob_threshold，
        而非硬阈值 sim >= threshold，使相似度在阈值附近时概率平滑过渡。
        """
        n = len(all_faces)
        uf = UnionFind(n)
        face_id_to_idx: dict[str, int] = {}
        for i, face in enumerate(all_faces):
            if face.face_id:
                face_id_to_idx[face.face_id] = i

        for i, face in enumerate(all_faces):
            if not face.face_id:
                continue
            for other_id, sim in search_results.get(face.face_id, {}).items():
                j = face_id_to_idx.get(other_id)
                if j is not None and j != i:
                    prob = calculate_gaussian_probability(sim, sigma)
                    if prob >= prob_threshold:
                        uf.union(i, j)

        groups: dict[int, list[int]] = defaultdict(list)
        for i in range(n):
            groups[uf.find(i)].append(i)
        return groups

    # ---- 响应组装 ----

    def _build_response(
        self,
        urls: list[str],
        all_faces: list[_DetectedFace],
        groups: dict[int, list[int]],
        search_results: dict[str, dict[str, float]],
        unclassified: list[str],
        warnings: list[str],
        request: FaceClassifyRequest,
    ) -> FaceClassifyResponse:
        face_groups: list[FaceGroup] = []
        unclassified_images: list[str] = list(unclassified)
        sorted_groups = sorted(groups.values(), key=len, reverse=True)

        for gid, indices in enumerate(sorted_groups, 1):
            # 入库失败的人脸（无 face_id）会各自成孤立分组，归入 unclassified，避免误导性单成员分组。
            if len(indices) == 1 and not all_faces[indices[0]].face_id:
                unclassified_images.append(all_faces[indices[0]].image_url)
                continue
            rep_idx = next((i for i in indices if all_faces[i].face_id), indices[0])
            rep_face = all_faces[rep_idx]
            group_faces: list[FaceInfo] = []
            sims: list[float] = []

            for idx in indices:
                face = all_faces[idx]
                if idx == rep_idx:
                    sim = 1.0
                else:
                    sim = search_results.get(rep_face.face_id, {}).get(face.face_id, 0.0)
                sims.append(sim)
                face.similarity_to_rep = sim
                group_faces.append(
                    FaceInfo(
                        face_id=face.face_id or f"local-{idx}",
                        image_url=face.image_url,
                        bounding_box=self._to_bbox(face.bounding_box)
                        if request.return_face_boxes
                        else None,
                        similarity=round(clamp(sim), 6),
                    )
                )

            image_urls = list(dict.fromkeys(all_faces[i].image_url for i in indices))
            confidence = round(clamp(sum(sims) / len(sims)), 6) if sims else 0.0
            face_groups.append(
                FaceGroup(
                    group_id=gid,
                    face_count=len(indices),
                    image_count=len(image_urls),
                    representative_image_url=rep_face.image_url,
                    image_urls=image_urls,
                    faces=group_faces,
                    confidence=confidence,
                )
            )

        return FaceClassifyResponse(
            total_images=len(urls),
            total_faces=len(all_faces),
            group_count=len(face_groups),
            groups=face_groups,
            unclassified_images=unclassified_images,
            warnings=warnings,
        )

    @staticmethod
    def _to_bbox(raw: dict[str, Any] | None) -> BoundingBox | None:
        if not raw:
            return None
        try:
            return BoundingBox(
                top_left_x=float(raw.get("top_left_x", 0)),
                top_left_y=float(raw.get("top_left_y", 0)),
                width=float(raw.get("width", 0)),
                height=float(raw.get("height", 0)),
            )
        except (TypeError, ValueError):
            return None
