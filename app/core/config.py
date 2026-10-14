"""应用配置管理。

使用 pydantic-settings 从环境变量 / .env 文件加载配置，
并通过 lru_cache 提供全局单例，供依赖注入使用。
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
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

    # ---- 管理接口（功能开关及管理）----
    # 空字符串 = 开发环境允许无鉴权访问管理接口；非空 = 请求头需携带 x-admin-key。
    admin_api_key: str = ""

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
    query_intent_gamma: float = Field(default=0.4, ge=0.0, le=1.0)
    # 多模态查询融合权重 λ（V_final = λ*V_text + (1-λ)*V_vision）。
    multimodal_lambda: float = 0.7
    # 时间新鲜度指数衰减的半衰期（天）。半衰期越小，越“喜新厌旧”。
    time_decay_half_life_days: float = 365.0
    default_datetime_iso: str = "1970-01-01T00:00:00+00:00"

    # ---- 智能场景分析（多标签体系）----
    scene_enabled: bool = True
    scene_cache_size: int = 2000
    scene_cache_halflife_days: float = 7.0
    scene_infer_timeout_seconds: float = 3.0

    # ---- AI 分析（同一个人 vs 相似图片）----
    ai_analyzer_provider: str = "immich"
    ai_person_confidence_threshold: float = 0.6
    ai_similarity_threshold: float = 0.75
    ai_analyze_timeout_seconds: float = 5.0
    ai_analyzer_max_retries: int = 2
    ai_retry_backoff_seconds: float = 0.5
    ai_max_concurrent: int = 10

    # ---- 批量导入（本地文件 → 提取 EXIF → 上传 Immich）----
    import_max_concurrent: int = 3
    import_default_device_id: str = "batch-import"
    # 允许导入的目录白名单（空列表 = 允许所有路径，仅限开发环境）。
    import_allowed_dirs: list[str] = []
    # 单个文件大小上限（MB），超过则拒绝上传。
    import_max_file_size_mb: int = 100
    # 单次批量导入文件数量上限。
    import_max_files: int = 1000

    # ---- 水印管理（本地图片 → 批量添加文字/图片水印）----
    # 水印功能总开关，关闭后 /api/v1/watermark/apply 返回 503。
    watermark_enabled: bool = True
    # ProcessPoolExecutor 最大子进程数。
    watermark_max_workers: int = 4
    # 单次批量水印处理的文件数量上限。
    watermark_max_files: int = 200
    # 单个文件大小上限（MB）。
    watermark_max_file_size_mb: int = 100
    # 允许处理源文件的目录白名单（空列表 = 允许所有路径，仅限开发环境）。
    watermark_allowed_dirs: list[str] = []
    # 默认水印字体路径（空 = 使用 Pillow 默认字体）。
    watermark_default_font: str = ""

    # ---- 华为云图像识别（预留扩展，默认关闭）----
    huawei_enabled: bool = False
    huawei_ak: str = ""
    huawei_sk: SecretStr = SecretStr("")
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

    # ---- Redis 缓存后端（默认关闭，不可用时自动降级到内存）----
    redis_enabled: bool = False
    redis_url: str = "redis://localhost:6379/0"
    redis_timeout_seconds: float = 2.0
    redis_key_prefix: str = "imgsearch:"

    # ---- 令牌桶限流（Redis 不可用时自动放行）----
    rate_limit_enabled: bool = True
    rate_limit_qps: int = 50
    rate_limit_burst: int = 100
    # 可信代理 IP 列表（空 = 不信任任何 X-Forwarded-For，直接用连接 IP）。
    trusted_proxies: list[str] = []

    # ---- 缓存 TTL（秒）----
    scene_cache_ttl_seconds: int = 86400
    search_cache_ttl_seconds: int = 300

    # ---- 搜索建议与纠错 ----
    # 开启后 /api/v1/search/suggest 和 /correct 可用；关闭后返回空结构，不报错。
    suggest_enabled: bool = True
    # 建议接口返回的默认条数上限。
    suggest_limit: int = 10
    # 热门搜索词内存 LRU 计数容量。
    suggest_stats_cache_size: int = 1000
    # 纠错接口的 Levenshtein 编辑距离上限（距离 <= 此值才返回纠正建议）。
    suggest_edit_distance_max: int = 2

    # ---- 搜索结果可解释性（为什么排在这里）----
    # 开启后每条搜索结果附带 explanation 字段；关闭后 explanation 为 None，不影响主流程。
    explain_enabled: bool = True

    # ---- 统计大屏（数据 Provider 基础层）----
    # 统计功能总开关，关闭后 iter_assets 返回空。
    stats_enabled: bool = True
    # 单次拉取资产上限，防止内存溢出。
    stats_max_items: int = 10000
    # 分页大小，每页向 Immich 请求的资产数。
    stats_page_size: int = 100
    # 统计结果缓存 TTL（秒），供后续统计模块使用。
    stats_cache_ttl_seconds: int = 600

    # ---- 搜索反馈闭环 ----
    feedback_enabled: bool = True
    feedback_ttl_seconds: int = 604800
    feedback_boost_max: float = 0.05

    # ---- 搜索结果去重（连拍/相似图只保留一张）----
    # 开启后排序结果中相似度极高的条目被去重，保留 final_score 最高的一张。
    dedup_enabled: bool = True
    # 拍摄时间窗口（秒），在此范围内 + 尺寸相同视为连拍。
    dedup_time_window_seconds: int = 3
    # 画面相似度阈值，超过此值 + 尺寸相同视为重复。
    dedup_similarity_threshold: float = 0.95

    # ---- 以图搜图（image_urls 参与搜索）----
    # 开启后请求携带 image_urls 时，从参考图提取相似 asset_id 并合并到搜索候选集。
    image_query_enabled: bool = True
    # 每张参考图取相似图的数量上限。
    image_query_top_k: int = 50
    # 以图搜图单次请求超时（秒）。
    image_query_timeout_seconds: float = 5.0

    # ---- 自动相册（按场景 + 时间聚类）----
    # 开启后 GET /api/v1/search/albums 可用；关闭后返回 503。
    album_enabled: bool = True
    # 同一场景内相邻照片时间差超过此天数则切分为新相册。
    album_time_gap_days: int = 3
    # 相册数上限，防止生成过多相册。
    album_max_count: int = 100
    # 相册结果缓存 TTL（秒）。
    album_cache_ttl_seconds: int = 600

    # ---- 否定语义搜索（query 中否定前缀解析 + 场景标签过滤）----
    # 开启后解析 query 中的否定前缀（不要/没有/不含/排除/-），过滤掉命中否定词的结果。
    negation_enabled: bool = True
    # 否定前缀列表，可通过环境变量自定义。
    negation_prefixes: list[str] = ["不要", "没有", "不含", "排除", "-"]

    # ---- 排序维度切换 ----
    # 开启后请求可携带 sort_by 参数选择排序方式；关闭后忽略 sort_by，始终用 relevance。
    sort_by_enabled: bool = True
    # random 排序的随机种子，相同种子产生相同顺序（0 = 默认种子）。
    sort_by_random_seed: int = 0

    # ---- 时间语义化（自然语言时间表达 → 日期范围过滤）----
    # 开启后从 query 中识别「去年夏天」「上个月」等时间表达，转成日期范围过滤结果。
    time_parser_enabled: bool = True

    @field_validator("immich_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _production_safety(self) -> "Settings":
        if self.app_env == "production":
            if not self.search_api_key:
                raise ValueError("生产环境必须配置 SEARCH_API_KEY（非空）")
            if not self.immich_api_key:
                raise ValueError("生产环境必须配置 IMMICH_API_KEY（非空）")
            if not self.import_allowed_dirs:
                raise ValueError(
                    "生产环境必须配置 IMPORT_ALLOWED_DIRS（非空），"
                    "防止批量导入接口被用于读取任意服务器文件"
                )
            if not self.watermark_allowed_dirs:
                raise ValueError(
                    "生产环境必须配置 WATERMARK_ALLOWED_DIRS（非空），"
                    "防止水印接口被用于读取任意服务器文件"
                )
        return self

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
