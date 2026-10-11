"""应用配置管理。

使用 pydantic-settings 从环境变量 / .env 文件加载配置，
并通过 lru_cache 提供全局单例，供依赖注入使用。
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 应用基础 ----
    app_name: str = "image-semantic-search"
    app_env: str = "development"
    log_level: str = "INFO"

    # ---- API Key 鉴权（搜索接口）----
    # 空字符串 = 不启用鉴权（向后兼容）；非空 = 启用，请求头需携带 x-api-key。
    search_api_key: str = ""

    # ---- Immich 上游 ----
    immich_base_url: str = "http://localhost:2283"
    immich_api_key: str = ""
    immich_timeout_seconds: float = 10.0
    immich_connect_timeout_seconds: float = 3.0
    immich_max_retries: int = 2
    immich_retry_backoff_seconds: float = 0.5

    # ---- 搜索结果 ----
    search_max_concurrent_vision: int = 5
    search_max_concurrent_scene: int = 10

    # ---- 排序权重（二次排序：时间 α + 相似度 β）----
    time_weight: float = 0.3
    similarity_weight: float = 0.7
    # 查询意图动态权重调整强度（0=完全静态，1=最大动态偏移）。
    query_intent_gamma: float = 0.4
    # 多模态查询融合权重 λ（V_final = λ*V_text + (1-λ)*V_vision）。
    multimodal_lambda: float = 0.7
    # 时间新鲜度指数衰减的半衰期（天）。半衰期越小，越“喜新厌旧”。
    time_decay_half_life_days: float = 365.0
    # EXIF 与文件创建时间都缺失时的回退时间。
    default_datetime_iso: str = "1970-01-01T00:00:00+00:00"

    # ---- 智能场景分析（多标签体系）----
    scene_enabled: bool = True
    scene_cache_size: int = 2000
    scene_infer_timeout_seconds: float = 3.0

    # ---- AI 分析（同一个人 vs 相似图片）----
    ai_analyzer_provider: str = "immich"
    ai_person_confidence_threshold: float = 0.6
    ai_similarity_threshold: float = 0.75
    ai_analyze_timeout_seconds: float = 5.0
    ai_analyzer_max_retries: int = 2
    ai_retry_backoff_seconds: float = 0.5
    ai_max_concurrent: int = 10

    # ---- 华为云图像识别（预留扩展，默认关闭）----
    huawei_enabled: bool = False
    huawei_ak: str = ""
    huawei_sk: str = ""
    huawei_project_id: str = ""

    # ---- 华为云人脸识别 FRS（用于人脸分类，默认关闭）----
    huawei_face_enabled: bool = False
    huawei_frs_endpoint: str = "https://face.cn-north-4.myhuaweicloud.com"
    huawei_face_similarity_threshold: float = 0.62
    huawei_face_search_top_n: int = 10
    huawei_face_max_concurrent: int = 5
    huawei_face_set_prefix: str = "tmp_classify_"
    huawei_face_timeout_seconds: float = 15.0
    huawei_face_connect_timeout_seconds: float = 5.0
    # 高斯核软聚类参数
    face_gaussian_sigma: float = 0.1
    face_probability_threshold: float = 0.85
    # 复合条件搜索权重（贝叶斯降级加权评分）
    multi_condition_weight_person: float = 0.5
    multi_condition_weight_location: float = 0.3
    multi_condition_weight_query: float = 0.2

    @field_validator("immich_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def weights(self) -> tuple[float, float]:
        """归一化后的 (时间权重, 相似度权重)，保证两者之和为 1。"""
        total = self.time_weight + self.similarity_weight
        if total <= 0:
            return (0.0, 1.0)
        return (self.time_weight / total, self.similarity_weight / total)


@lru_cache
def get_settings() -> Settings:
    return Settings()