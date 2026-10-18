"""文件路径安全校验：目录白名单 + 符号链接逃逸 + 文件类型过滤。"""
from __future__ import annotations

from pathlib import Path

_ALLOWED_IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
)

_ALLOWED_MIME_PREFIXES = frozenset({"image/"})


class PathValidationError(ValueError):
    """路径校验失败（消息不含服务器完整路径）。"""


def validate_file_path(
    raw_path: str,
    allowed_dirs: list[str],
    *,
    max_size_bytes: int = 0,
    check_exists: bool = True,
) -> str:
    """校验单个文件路径安全性，返回规范化后的绝对路径。

    检查项：
    - 空字节 / 控制字符 / 超长路径
    - 符号链接逃逸
    - 目录白名单（生产环境必须非空）
    - 仅允许普通文件
    - 仅允许图片扩展名
    - 可选：文件大小上限

    异常:
        PathValidationError: 校验失败，消息不泄露服务器完整路径。
    """
    p = raw_path.strip()
    if not p:
        raise PathValidationError("文件路径不能为空")
    if len(p) > 1024:
        raise PathValidationError("文件路径长度不能超过 1024 字符")
    if any(c in p for c in ("\x00", "\n", "\r")):
        raise PathValidationError("文件路径包含非法字符")

    p_path = Path(p)
    if p_path.is_symlink():
        raise PathValidationError("拒绝符号链接")

    resolved = p_path.resolve()

    if not allowed_dirs:
        raise PathValidationError("未配置导入目录白名单（IMPORT_ALLOWED_DIRS）")

    allowed_resolved = [Path(d).resolve() for d in allowed_dirs]
    if not any(resolved == allowed or allowed in resolved.parents for allowed in allowed_resolved):
        raise PathValidationError("文件路径不在允许的目录范围内")

    if check_exists:
        if not resolved.exists():
            raise PathValidationError("文件不存在")
        if not resolved.is_file():
            raise PathValidationError("路径不是普通文件")

        if resolved.suffix.lower() not in _ALLOWED_IMAGE_EXTENSIONS:
            raise PathValidationError(f"不支持的文件类型: {resolved.suffix}")

        if max_size_bytes > 0:
            size = resolved.stat().st_size
            if size > max_size_bytes:
                raise PathValidationError(f"文件大小超过上限 ({size} bytes)")
            if size == 0:
                raise PathValidationError("文件为空")

    return str(resolved)


def is_safe_image_extension(path: str) -> bool:
    """检查路径扩展名是否为允许的图片类型。"""
    return Path(path).suffix.lower() in _ALLOWED_IMAGE_EXTENSIONS
