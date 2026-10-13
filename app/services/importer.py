"""批量导入编排服务：提取元数据 → 上传 Immich → 汇总结果。

流程:
    对每个本地文件路径（并发受 Semaphore 控制）：
        1. 同步提取 EXIF 元数据（通过 asyncio.to_thread 避免阻塞事件循环）；
        2. 异步上传到 Immich；
        3. 单个文件失败不阻塞整体，记录到对应 ImportResultItem.error。

设计要点:
    - 单文件失败降级为逐项错误，而非整体抛出异常。
    - 提取失败（文件不存在、EXIF 异常）与上传失败（上游错误）分开处理。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from ..core.config import Settings
from ..core.errors import AppError
from ..models.batch_import import (
    ImportRequest,
    ImportResponse,
    ImportResultItem,
)
from .immich_uploader import ImmichAssetUploader
from .metadata import MetadataExtractor

logger = logging.getLogger(__name__)


class BatchImporter:
    """批量导入编排服务。"""

    def __init__(
        self,
        settings: Settings,
        extractor: MetadataExtractor,
        uploader: ImmichAssetUploader,
    ) -> None:
        self._settings = settings
        self._extractor = extractor
        self._uploader = uploader
        # 配置字段正式定义在 Settings 中，此处 getattr 提供字段尚未定义时的默认值。
        self._max_concurrent = max(1, int(getattr(settings, "import_max_concurrent", 3)))
        self._default_device_id = getattr(settings, "import_default_device_id", "batch-import")

    async def import_files(self, request: ImportRequest) -> ImportResponse:
        """批量导入文件并返回逐项结果。

        参数:
            request: 批量导入请求（本地文件路径列表 + 可选 device_id/is_favorite）。

        返回:
            ImportResponse 汇总（total/succeeded/failed/results）。
        """
        device_id = request.device_id or self._default_device_id
        sem = asyncio.Semaphore(self._max_concurrent)

        if len(request.file_paths) > self._settings.import_max_files:
            raise ValueError(
                f"文件数量 {len(request.file_paths)} 超过上限 {self._settings.import_max_files}"
            )

        collected_warnings: list[str] = []

        async def _one(file_path: str) -> ImportResultItem:
            async with sem:
                return await self._import_single(file_path, device_id, request.is_favorite, collected_warnings)

        results = await asyncio.gather(*(_one(p) for p in request.file_paths))

        succeeded = sum(1 for r in results if r.success)
        failed = len(results) - succeeded
        return ImportResponse(
            total=len(results),
            succeeded=succeeded,
            failed=failed,
            results=list(results),
            warnings=collected_warnings,
        )

    async def _import_single(
        self,
        file_path: str,
        device_id: str,
        is_favorite: bool,
        warnings: list[str],
    ) -> ImportResultItem:
        """导入单个文件：提取元数据 + 上传，失败降级为逐项错误。"""
        metadata = None
        try:
            metadata = await asyncio.to_thread(self._extractor.extract, file_path)
        except FileNotFoundError:
            return ImportResultItem(
                file_path=file_path, success=False, error="文件不存在"
            )
        except Exception as exc:  # noqa: BLE001 - 提取失败降级为警告，继续上传
            logger.warning("元数据提取失败 file=%s err=%s", file_path, exc)
            warnings.append(f"元数据提取失败（已降级继续上传）: {file_path}: {exc}")

        taken_at = metadata.taken_at if metadata is not None else datetime.now(timezone.utc)

        try:
            asset = await self._uploader.upload(
                file_path,
                device_id=device_id,
                taken_at=taken_at,
                is_favorite=is_favorite,
            )
        except AppError as exc:
            return ImportResultItem(
                file_path=file_path, success=False, error=exc.message, metadata=metadata
            )
        except Exception as exc:  # noqa: BLE001 - 上传失败降级为逐项错误
            logger.warning("上传失败 file=%s err=%s", file_path, exc)
            return ImportResultItem(
                file_path=file_path, success=False, error=f"上传失败: {exc}", metadata=metadata
            )

        asset_id = self._extract_asset_id(asset)
        if asset_id is None:
            return ImportResultItem(
                file_path=file_path,
                success=False,
                error="上传响应缺少资产 ID",
                metadata=metadata,
            )
        return ImportResultItem(
            file_path=file_path,
            success=True,
            asset_id=asset_id,
            metadata=metadata,
        )

    @staticmethod
    def _extract_asset_id(asset: dict[str, Any]) -> str | None:
        """从上传响应中提取资产 ID。"""
        asset_id = asset.get("id") if isinstance(asset, dict) else None
        return str(asset_id) if asset_id else None