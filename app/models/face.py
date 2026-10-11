"""人脸分类请求/响应模型（Pydantic）。"""
from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl, field_validator

from ..utils.url_safety import validate_url_or_raise


class BoundingBox(BaseModel):
    """人脸边界框（像素坐标）。"""

    top_left_x: float
    top_left_y: float
    width: float
    height: float


class FaceInfo(BaseModel):
    """单个人脸检测信息。"""

    face_id: str = Field(description="人脸库中的人脸标识")
    image_url: str = Field(description="该人脸所属图片 URL")
    bounding_box: BoundingBox | None = Field(default=None, description="人脸边界框")
    similarity: float | None = Field(default=None, ge=0.0, le=1.0, description="与分组代表的相似度")


class FaceGroup(BaseModel):
    """一个人物分组（同一人的所有人脸/图片）。"""

    group_id: int = Field(description="分组序号，从 1 开始")
    face_count: int = Field(ge=0, description="该组人脸总数")
    image_count: int = Field(ge=0, description="该组包含的不同图片数")
    representative_image_url: str = Field(description="代表图片 URL")
    image_urls: list[str] = Field(default_factory=list, description="该组所有图片 URL（去重）")
    faces: list[FaceInfo] = Field(default_factory=list, description="该组所有人脸详情")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="分组置信度（组内平均相似度）")


class FaceClassifyRequest(BaseModel):
    """人脸分类请求。

    - image_urls: 待分类的图片 URL 列表（必填、非空）。
    - similarity_threshold: 归为同一人的相似度阈值（0~1，默认 0.62）。
    - return_face_boxes: 是否在结果中返回人脸边界框。
    - max_faces: 单张图片最多检测的人脸数（1~10，默认 10）。
    """

    image_urls: list[HttpUrl] = Field(..., min_length=1, max_length=200, description="图片 URL 列表")
    similarity_threshold: float = Field(
        default=0.62, ge=0.0, le=1.0, description="归为同一人的相似度阈值"
    )
    return_face_boxes: bool = Field(default=True, description="是否返回人脸边界框")
    max_faces: int = Field(default=10, ge=1, le=10, description="单张图片最多检测人脸数")

    @field_validator("image_urls")
    @classmethod
    def _dedup_urls(cls, value: list[HttpUrl]) -> list[HttpUrl]:
        seen: list[HttpUrl] = []
        for url in value:
            if url not in seen:
                validate_url_or_raise(str(url))
                seen.append(url)
        return seen


class FaceClassifyResponse(BaseModel):
    """人脸分类统一响应。"""

    total_images: int = Field(description="输入图片总数")
    total_faces: int = Field(description="检测到的人脸总数")
    group_count: int = Field(description="人物分组数")
    groups: list[FaceGroup] = Field(default_factory=list, description="人物分组列表（按人脸数降序）")
    unclassified_images: list[str] = Field(
        default_factory=list, description="未检测到人脸的图片 URL"
    )
    warnings: list[str] = Field(default_factory=list, description="降级警告信息")