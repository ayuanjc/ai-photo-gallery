"""视觉识别抽象层（预留华为云图像识别接入点）。

通过抽象基类 + 依赖注入实现与核心业务解耦：
    - 业务层只依赖 VisionProvider 接口，不关心具体厂商。
    - 后续接入华为云 / 其他云只需新增实现类并在容器中注册。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class VisionResult:
    """单张图片的视觉识别结果。"""

    labels: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def keywords(self) -> list[str]:
        """合并 labels/tags 作为可用于增强语义查询的关键词。"""
        seen: list[str] = []
        for term in (*self.labels, *self.tags):
            term = (term or "").strip()
            if term and term not in seen:
                seen.append(term)
        return seen


class VisionProvider(ABC):
    """图像识别能力抽象接口。"""

    name: str = "abstract"

    @abstractmethod
    async def analyze(self, image_url: str) -> VisionResult:
        """对单个图片 URL 执行识别，返回结构化结果。"""

    def build_enriched_query(self, query: str, keywords: list[str]) -> str:
        """默认查询增强策略：原始 query + 去重后的识别关键词。

        各实现可覆盖此方法定制融合逻辑。
        """
        unique = [k for k in keywords if k and k not in query]
        if not unique:
            return query
        return f"{query} {' '.join(unique[:10])}"

class NullVisionProvider(VisionProvider):
    """默认空实现：不增强查询，仅透传。用于未启用视觉识别时的兜底。"""

    name = "null"

    async def analyze(self, image_url: str) -> VisionResult:
        return VisionResult()