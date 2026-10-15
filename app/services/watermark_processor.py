"""批量水印处理器：并行分发到子进程执行 Pillow 图像操作。

通过 ``concurrent.futures.ProcessPoolExecutor`` 将每个文件的
「打开 → 渲染 → 保存」放到子进程中执行，避免主进程事件循环被
CPU/IO 密集的 Pillow 操作阻塞。单个文件失败降级为逐项错误。
"""
from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from ..core.config import Settings
from ..core.errors import AppError, ErrorCode
from ..models.watermark import (
    ImageWatermarkStyle,
    LicenseType,
    TextWatermarkStyle,
    TileOptions,
    WatermarkRequest,
    WatermarkResponse,
    WatermarkResultItem,
    WatermarkTemplate,
)
from ..utils.path_safety import is_safe_image_extension
from .watermark_engine import apply_to_file
from .watermark_templates import merged_template

logger = logging.getLogger(__name__)


class BatchWatermarkProcessor:
    """批量水印处理器：解析模板 → 校验路径 → 子进程并行渲染。"""

    def __init__(self, settings: Settings, default_font_path: str | None = None) -> None:
        self._settings = settings
        self._default_font_path = default_font_path or getattr(
            settings, "watermark_default_font", ""
        ) or None
        self._max_workers = max(1, int(getattr(settings, "watermark_max_workers", 4)))
        self._max_files = int(getattr(settings, "watermark_max_files", 200))
        self._max_file_size_mb = int(getattr(settings, "watermark_max_file_size_mb", 100))
        self._allowed_dirs = list(getattr(settings, "watermark_allowed_dirs", []) or [])
        self._pool = ProcessPoolExecutor(max_workers=self._max_workers)

    def close(self) -> None:
        """关闭持久化进程池，在应用 lifespan 中调用。"""
        self._pool.shutdown(wait=False)

    async def process(self, request: WatermarkRequest) -> WatermarkResponse:
        """批量应用水印并返回逐项结果。

        参数:
            request: 批量水印请求（源路径 + 模板/自定义样式 + 输出配置）。

        返回:
            WatermarkResponse（total/succeeded/failed/results）。
        """
        if len(request.source_paths) > self._max_files:
            raise AppError(
                ErrorCode.INVALID_PARAMS,
                f"文件数量 {len(request.source_paths)} 超过上限 {self._max_files}",
            )

        try:
            template = self._resolve_template(request)
        except ValueError as exc:
            raise AppError(ErrorCode.INVALID_PARAMS, str(exc)) from exc

        if template.image is not None and template.image.image_path:
            self._validate_extra_path(template.image.image_path, "图片水印路径")
        if template.text is not None and template.text.font_path:
            self._validate_extra_path(template.text.font_path, "字体路径")

        text_dict = template.text.model_dump() if template.text is not None else None
        image_dict = template.image.model_dump() if template.image is not None else None
        tile_dict = template.tile.model_dump() if template.tile is not None else None

        validated_paths = await self._validate_paths(request.source_paths)
        output_paths = await self._compute_output_paths(validated_paths, request.output_dir)

        tasks = [
            (source, output, request.overwrite)
            for source, output in zip(validated_paths, output_paths)
        ]

        results = await self._run_in_pool(
            tasks, text_dict, image_dict, tile_dict
        )

        item_map = {r["source_path"]: r for r in results}
        items: list[WatermarkResultItem] = []
        for source in validated_paths:
            raw = item_map.get(source, {
                "source_path": source, "success": False,
                "output_path": None, "error": "处理结果缺失",
            })
            items.append(
                WatermarkResultItem(
                    source_path=raw["source_path"],
                    success=bool(raw["success"]),
                    output_path=raw.get("output_path"),
                    error=raw.get("error"),
                )
            )

        succeeded = sum(1 for it in items if it.success)
        return WatermarkResponse(
            total=len(items),
            succeeded=succeeded,
            failed=len(items) - succeeded,
            results=items,
            warnings=[],
        )

    def _resolve_template(self, request: WatermarkRequest) -> WatermarkTemplate:
        """根据请求解析最终水印模板（预置模板 + 请求覆盖，或纯自定义）。"""
        if request.template_name:
            return merged_template(
                request.template_name,
                license_type=request.license_type,
                text=request.text,
                image=request.image,
                tile=request.tile,
            )
        if request.text is None and request.image is None:
            raise AppError(
                ErrorCode.INVALID_PARAMS,
                "必须指定 template_name 或至少提供文字/图片水印样式",
            )
        return WatermarkTemplate(
            name="custom",
            license_type=request.license_type or LicenseType.COPYRIGHT,
            text=request.text,
            image=request.image,
            tile=request.tile,
        )

    async def _validate_paths(self, source_paths: list[str]) -> list[str]:
        """校验源路径（存在性/文件类型/符号链接/可选白名单），失败抛 400。"""
        max_size_bytes = self._max_file_size_mb * 1024 * 1024

        def _validate_one(raw_path: str) -> str:
            p = raw_path.strip()
            if not p:
                raise ValueError("源文件路径不能为空")
            if len(p) > 1024:
                raise ValueError("源文件路径长度不能超过 1024 字符")
            if any(c in p for c in ("\x00", "\n", "\r")):
                raise ValueError("源文件路径包含非法字符")

            p_path = Path(p)
            if p_path.is_symlink():
                raise ValueError("拒绝符号链接")
            resolved = p_path.resolve()
            if not resolved.exists():
                raise ValueError("源文件不存在")
            if not resolved.is_file():
                raise ValueError("路径不是普通文件")
            if not is_safe_image_extension(str(resolved)):
                raise ValueError(f"不支持的文件类型: {resolved.suffix}")

            if self._allowed_dirs:
                allowed_resolved = [Path(d).resolve() for d in self._allowed_dirs]
                if not any(
                    resolved == allowed or allowed in resolved.parents
                    for allowed in allowed_resolved
                ):
                    raise ValueError("源文件路径不在允许的目录范围内")

            if max_size_bytes > 0:
                size = resolved.stat().st_size
                if size > max_size_bytes:
                    raise ValueError(f"文件大小超过上限 ({size} bytes)")
                if size == 0:
                    raise ValueError("文件为空")
            return str(resolved)

        errors: list[str] = []
        validated: list[str] = []
        for raw_path in source_paths:
            try:
                validated.append(await asyncio.to_thread(_validate_one, raw_path))
            except ValueError as exc:
                errors.append(f"{raw_path}: {exc}")
        if errors:
            raise AppError(ErrorCode.INVALID_PARAMS, "; ".join(errors))
        return validated

    def _validate_extra_path(self, raw_path: str, label: str) -> None:
        """校验辅助文件路径（字体/图片水印），限制在 allowed_dirs 范围内。"""
        if not raw_path:
            return
        p_path = Path(raw_path)
        if p_path.is_symlink():
            raise AppError(ErrorCode.INVALID_PARAMS, f"{label}：拒绝符号链接")
        resolved = p_path.resolve()
        if self._allowed_dirs:
            allowed_resolved = [Path(d).resolve() for d in self._allowed_dirs]
            if not any(
                resolved == allowed or allowed in resolved.parents
                for allowed in allowed_resolved
            ):
                raise AppError(ErrorCode.INVALID_PARAMS, f"{label}：不在允许的目录范围内")

    async def _compute_output_paths(
        self, validated_paths: list[str], output_dir: str | None
    ) -> list[str | None]:
        """计算输出路径：指定 output_dir 时统一输出到该目录，否则返回 None（同目录）。"""
        if not output_dir:
            return [None] * len(validated_paths)

        out_path = Path(output_dir)
        if out_path.is_symlink():
            raise AppError(ErrorCode.INVALID_PARAMS, "output_dir：拒绝符号链接")
        resolved_out = out_path.resolve()
        if self._allowed_dirs:
            allowed_resolved = [Path(d).resolve() for d in self._allowed_dirs]
            if not any(
                resolved_out == allowed or allowed in resolved_out.parents
                for allowed in allowed_resolved
            ):
                raise AppError(ErrorCode.INVALID_PARAMS, "output_dir：不在允许的目录范围内")

        await asyncio.to_thread(os.makedirs, str(resolved_out), exist_ok=True)
        paths: list[str] = []
        for source in validated_paths:
            base, ext = os.path.splitext(os.path.basename(source))
            paths.append(os.path.join(str(resolved_out), f"{base}_watermarked{ext}"))
        return paths

    async def _run_in_pool(
        self,
        tasks: list[tuple[str, str | None, bool]],
        text_dict: dict | None,
        image_dict: dict | None,
        tile_dict: dict | None,
    ) -> list[dict]:
        """通过 ProcessPoolExecutor 在子进程并行执行水印渲染。"""
        loop = asyncio.get_running_loop()
        pool = self._pool
        futures = [
            loop.run_in_executor(
                pool,
                apply_to_file,
                source,
                text_dict,
                image_dict,
                tile_dict,
                output,
                overwrite,
                self._default_font_path,
            )
            for source, output, overwrite in tasks
        ]
        return list(await asyncio.gather(*futures))