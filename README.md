# AI 图像语义搜索微服务

基于 FastAPI 的 AI 图像语义搜索微服务，对接 [Immich](https://immich.app/) 开源照片管理平台与华为云 FRS 人脸识别服务，提供语义搜索、复合条件搜索、人脸分类、AI 图像分析四大核心能力。

## 功能特性

### 核心算法（7 项）

| # | 功能 | 公式 / 说明 |
|---|------|-------------|
| 1 | **语义搜索** | 对接 Immich `POST /api/search/smart`，自然语言查图 |
| 2 | **指数时间衰减二次排序** | `Final = α·2^(-Age/HalfLife) + β·Similarity`，新照片加权更高 |
| 3 | **查询意图动态权重** | `α_dynamic = α_base + γ·IntentScore`，按查询语义自动调节时间/相似度权重 |
| 4 | **多模态向量加权融合** | `V_final = λ·V_text + (1-λ)·V_vision`，加权拼接模拟向量融合（λ=0.7） |
| 5 | **华为云 FRS 人脸分类** | SDK-HMAC-SHA256 签名 + 高斯核概率软聚类 + 并查集分组 |
| 6 | **华为云 Image Tagging** | AK/SK 签名调用 `/v1.0/image/tagging`，标签增强语义搜索 |
| 7 | **时间衰减 LFU 缓存** | `Heat = Freq·2^(-Age/HalfLife)`，替代 LRU，热门不淘汰 |

### 复合条件搜索（贝叶斯降级检索）

- 按 `person_id + location + query` 复合搜索，严格交集优先
- 交集为空时自动降级为加权评分合并：`Score = w1·S_person + w2·S_location + w3·S_query`
- 权重：person(0.5) > location(0.3) > query(0.2)

### Immich 原生集成

- **能力探测**：自动探测 Immich 版本与 9 项能力（搜索/元数据/标签/相册/人脸/插件/工作流…），依据能力降级
- **AI 场景标签写回**：搜索时异步将 AI 场景标签写回 Immich（幂等 + 可撤销 + 审计日志）
- **AI 相册写回**：按场景 + 月份自动创建/更新 Immich 相册（`AI·场景·YYYY-MM` 命名）
- **图片代理**：缩略图 / 原图 / 共享链接代理，避免前端直连 Immich
- **增量水位同步**：基于 `updatedAt` 水线增量拉取，避免全量扫描
- **bulk-upload-check**：上传前查重，避免重复入库
- **写回管理**：统一写回客户端（`idempotent` / `rollback` / `audit` 三模式），一键回滚
- **冲突策略**：标签/相册冲突时可配置策略（skip / overwrite / merge），失败不阻塞主流程

### Immich 插件 / 工作流原生嵌入

- **插件 hook 端点**：`POST /api/v1/plugin/hook`，接收 Immich 工作流 webhook 回调，触发场景分析 + 标签/相册写回
- **工作流管理**：通过 Immich v3.3+ 工作流 API 自动创建/更新/删除工作流
- **三个默认工作流**：
  - `yunying-on-upload`：`AssetCreate` → webhook → 场景分析 + 标签写回
  - `yunying-on-metadata`：`AssetMetadataExtraction` → webhook → 元数据补全
  - `yunying-on-tagged`：`AssetTagged` → webhook → 归档检查 + 相册写回
- **无需 WASM 插件**：直接使用 Immich 内置 `immich-plugin-core` 方法（`assetAddTags` / `assetAddToAlbums` / `webhook`）

### 工程化能力

- 统一错误码体系（`AppError`，区分 400/401/429/502/503/504/500）
- API Key 鉴权（`hmac.compare_digest` 防时序攻击）
- 令牌桶限流（Redis / 内存降级，QPS + 突发容量）
- 优雅降级（`degraded: true` + 缓存兜底，上游不可用不报 5xx）
- 可插拔 AI 架构（抽象基类 + 依赖注入，切换引擎无需改业务代码）
- SSRF 防护（拒绝内网/回环/链路本地地址）
- 错误响应脱敏（自动遮蔽密码/密钥/连接串）
- 场景词典与标注（10 大场景 + 子场景 + 视觉属性白名单）
- AI 图像分析（同一人 / 相似图 / 无关 三分类判定）
- 搜索建议与纠错（热门词 LRU + Levenshtein 编辑距离）
- 搜索结果去重（连拍/相似图检测，时间窗口 + 尺寸 + 相似度）
- 搜索结果可解释性（每条结果附带排序原因）
- 搜索反馈闭环（点击/跳过信号 → 排序微调）
- 否定语义搜索（不要/没有/不含/排除 前缀解析）
- 时间语义化（自然语言时间表达 → 日期范围过滤）
- 以图搜图（参考图相似度搜索 + 候选合并）
- 自动相册（场景 + 时间聚类，时间切分）
- 统计大屏（GPS 地图分布 + EXIF + 器材统计）
- 批量导入（本地文件 → EXIF 提取 → 上传 Immich）
- 水印管理（文字/图片水印，批量处理，模板化）
- Redis 缓存后端（可选，不可用时自动降级到内存）
- 运行时配置热更新（管理接口动态切换功能开关）

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/search/smart` | AI 语义搜索（视觉增强 + 二次排序 + 去重 + 可解释性） |
| POST | `/api/v1/search/multi-condition` | 复合条件搜索（贝叶斯降级检索） |
| POST | `/api/v1/search/feedback` | 搜索反馈闭环（点击/跳过信号采集） |
| GET | `/api/v1/search/suggest` | 搜索建议（热门词 + 前缀匹配） |
| GET | `/api/v1/search/correct` | 搜索纠错（Levenshtein 编辑距离） |
| GET | `/api/v1/search/albums` | 自动相册（场景 + 时间聚类） |
| POST | `/api/v1/faces/classify` | 人脸分类（检测 → 入库 → 搜索 → 聚类 → 分组） |
| POST | `/api/v1/ai/analyze` | AI 分析（同一人 / 相似图 / 无关判定） |
| POST | `/api/v1/import/batch` | 批量导入（本地文件 → EXIF → 上传 Immich） |
| GET | `/api/v1/scene/dict` | 获取场景词典（只读） |
| GET | `/api/v1/stats/map` | 统计大屏 — 地图分布（GPS 热力） |
| GET | `/api/v1/stats/exif` | 统计大屏 — EXIF 分布（相机/镜头/焦距） |
| GET | `/api/v1/stats/gear` | 统计大屏 — 器材统计（品牌/机型排行） |
| GET | `/api/v1/watermark/templates` | 水印模板列表 |
| GET | `/api/v1/watermark/licenses` | 水印字体许可证 |
| POST | `/api/v1/watermark/apply` | 批量添加水印（文字/图片） |
| GET | `/api/v1/assets/{id}/thumbnail` | 图片代理 — 缩略图/预览图 |
| GET | `/api/v1/assets/{id}/original` | 图片代理 — 原图 |
| POST | `/api/v1/plugin/hook` | Immich 插件/工作流 webhook 回调 |
| GET | `/admin/api/features` | 管理接口 — 功能开关查询 |
| PUT | `/admin/api/features` | 管理接口 — 功能开关更新 |
| GET | `/admin/api/status` | 管理接口 — 服务状态 |
| GET | `/admin/api/settings` | 管理接口 — 配置查询 |
| PUT | `/admin/api/settings` | 管理接口 — 配置更新 |
| GET | `/admin/api/capabilities` | 管理接口 — Immich 能力探测 |
| POST | `/admin/api/sync` | 管理接口 — 增量水位同步 |
| GET | `/admin/api/writeback/audit` | 管理接口 — 写回审计日志 |
| GET | `/admin/api/workflows` | 管理接口 — 工作流列表 |
| POST | `/admin/api/workflows/setup` | 管理接口 — 创建默认工作流 |
| DELETE | `/admin/api/workflows/{id}` | 管理接口 — 删除工作流 |
| GET | `/health` | 健康检查 |

### 请求示例

```json
POST /api/v1/search/smart
{
  "query": "海边日落",
  "asset_ids": ["3f2c...", "a1b2..."],
  "limit": 20,
  "include_metadata_fields": ["width", "height", "latitude", "longitude", "city"]
}
```

### 响应示例

```json
{
  "query": "海边日落",
  "total": 2,
  "degraded": false,
  "warnings": [],
  "enriched_query": "海边日落 海边日落 沙滩 夕阳 海鸥",
  "results": [
    {
      "asset_id": "3f2c...",
      "thumbnail_url": "http://immich:2283/api/assets/3f2c.../thumbnail?size=preview",
      "captured_at": "2024-06-10T18:20:31Z",
      "captured_at_source": "exif",
      "similarity_score": 0.81,
      "time_freshness": 0.92,
      "final_score": 0.843,
      "metadata": {"width": 4000, "height": 3000, "city": "三亚"}
    }
  ]
}
```

### 错误响应

```json
{
  "error": {
    "code": "UPSTREAM_UNAVAILABLE",
    "message": "Immich 服务不可用: POST /api/search/smart",
    "request_id": "9f3a...",
    "details": "ConnectError..."
  }
}
```

## 项目结构

```
a01/
├── app/
│   ├── main.py                          # FastAPI 入口（lifespan / 中间件 / 异常处理器）
│   ├── api/
│   │   ├── deps.py                      # 依赖注入（鉴权 / 服务获取）
│   │   └── routes/
│   │       ├── search.py                # 搜索路由（smart / multi-condition / suggest / correct / albums / feedback）
│   │       ├── faces.py                 # 人脸分类路由
│   │       ├── ai.py                    # AI 分析路由
│   │       ├── scene.py                 # 场景词典路由
│   │       ├── stats.py                 # 统计大屏路由（map / exif / gear）
│   │       ├── admin.py                 # 管理接口路由（功能开关 / 配置 / 状态 / 工作流）
│   │       ├── assets.py                # 资产代理路由（缩略图 / 原图 / 共享链接）
│   │       ├── plugin.py                # Immich 插件/工作流 hook 回调路由
│   │       ├── batch_import.py          # 批量导入路由
│   │       └── watermark.py            # 水印管理路由
│   ├── core/
│   │   ├── config.py                    # pydantic-settings 配置
│   │   ├── errors.py                    # 错误码与领域异常体系
│   │   ├── logging.py                   # 日志配置
│   │   ├── cache.py                     # 缓存抽象（内存 + Redis 降级）
│   │   ├── rate_limit.py                # 令牌桶限流（Redis / 内存降级）
│   │   ├── redis_client.py              # Redis 客户端封装
│   │   └── runtime_config.py            # 运行时配置（热更新）
│   ├── models/
│   │   ├── request.py                   # 请求模型
│   │   ├── response.py                  # 响应模型
│   │   ├── face.py                      # 人脸分类请求/响应模型
│   │   ├── ai.py                        # AI 分析请求模型
│   │   ├── batch_import.py              # 批量导入请求模型
│   │   ├── stats.py                     # 统计大屏响应模型
│   │   └── watermark.py                 # 水印请求/响应模型
│   ├── services/
│   │   ├── immich.py                    # Immich 客户端（重试/超时/错误分类）
│   │   ├── immich_uploader.py           # Immich 文件上传
│   │   ├── immich_capabilities.py       # Immich 能力探测（版本/搜索/插件/工作流…）
│   │   ├── immich_writeback.py          # 统一写回客户端（幂等/回滚/审计）
│   │   ├── writeback_manager.py         # 写回管理 + 冲突策略 + 一键回滚
│   │   ├── scene_writeback.py           # AI 场景标签写回
│   │   ├── album_writeback.py           # AI 相册写回（场景 + 月份）
│   │   ├── image_proxy.py               # 图片代理（缩略图/原图/共享链接）
│   │   ├── incremental_sync.py          # 增量水位同步（updatedAt 水线）
│   │   ├── workflow_manager.py          # Immich 工作流管理（创建/更新/删除/默认模板）
│   │   ├── ranking.py                   # 二次排序算法（衰减/动态权重/多模态融合/场景权重）
│   │   ├── search_service.py            # 搜索编排（视觉增强 + 场景标注 + 排序 + 去重）
│   │   ├── face_service.py              # 人脸分类编排（检测/入库/搜索/高斯聚类）
│   │   ├── multi_condition_service.py   # 复合条件搜索（严格交集 + 贝叶斯降级）
│   │   ├── album_service.py             # 自动相册（场景 + 时间聚类）
│   │   ├── suggest_service.py           # 搜索建议（热门词 + 前缀匹配）
│   │   ├── feedback_service.py          # 搜索反馈闭环
│   │   ├── image_query_service.py       # 以图搜图
│   │   ├── importer.py                  # 批量导入（EXIF 提取 + 上传）
│   │   ├── stats_provider.py            # 统计大屏数据层
│   │   ├── dedup.py                     # 搜索结果去重（连拍/相似图）
│   │   ├── explain.py                   # 搜索结果可解释性
│   │   ├── metadata.py                  # 元数据提取
│   │   ├── negation_parser.py           # 否定语义解析
│   │   ├── time_parser.py               # 时间语义解析
│   │   ├── watermark_engine.py          # 水印引擎
│   │   ├── watermark_processor.py       # 水印批处理
│   │   ├── watermark_templates.py       # 水印模板
│   │   └── vision/
│   │       ├── base.py                  # 视觉识别抽象基类 + 空实现
│   │       ├── huawei.py                # 华为云图像识别适配器（Image Tagging）
│   │       ├── huawei_face.py           # 华为云 FRS 人脸识别客户端
│   │       └── huawei_signer.py         # SDK-HMAC-SHA256 签名工具
│   ├── ai/
│   │   ├── base.py                      # AI 分析抽象基类 + 分类函数
│   │   ├── factory.py                   # 分析器工厂
│   │   ├── immich_analyzer.py           # 基于 Immich 的同人判定
│   │   └── huawei_analyzer.py           # 华为云人脸分析器
│   ├── scene/
│   │   ├── analyzer.py                  # 场景分析器（分级推理 + 缓存 + singleflight）
│   │   ├── dictionary.py                # 场景词典加载与白名单校验
│   │   ├── models.py                    # 场景标签数据模型
│   │   └── scene_dict.json              # 场景词典数据文件
│   └── utils/
│       ├── cache.py                     # 时间衰减 LFU 缓存
│       ├── date_parse.py                # 日期解析
│       ├── path_safety.py               # 路径安全校验
│       ├── time_parsing.py              # EXIF 时间解析与新鲜度计算
│       └── url_safety.py                # URL 安全校验（SSRF 防护）
├── tests/                               # 单元测试与集成测试（732+ 用例）
├── brute_test.py                        # 破坏性测试（160 用例，六大类场景）
├── verify_integration.py                # 端到端验收脚本（P1-P21，9 项检查）
├── .env.example                         # 环境配置示例
├── Dockerfile
├── docker-compose.yml
├── docker-compose.override.yml          # Immich 联合部署覆盖配置
├── nginx.conf                           # Nginx 反向代理配置
└── pyproject.toml
```

## 快速开始

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 IMMICH_BASE_URL 与 IMMICH_API_KEY

# 2. 安装依赖
pip install -e ".[dev]"

# 3. 启动服务
uvicorn app.main:app --reload

# 4. 打开接口文档
# http://localhost:8000/docs
```

## 配置项

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `IMMICH_BASE_URL` | `http://localhost:2283` | Immich 服务地址 |
| `IMMICH_API_KEY` | — | Immich API Key |
| `SEARCH_API_KEY` | `""` | 搜索接口鉴权 Key（空=不鉴权） |
| `ADMIN_API_KEY` | `""` | 管理接口鉴权 Key（空=不鉴权） |
| `TIME_WEIGHT` | `0.3` | 时间权重 α |
| `SIMILARITY_WEIGHT` | `0.7` | 相似度权重 β |
| `QUERY_INTENT_GAMMA` | `0.4` | 查询意图动态权重强度 γ |
| `MULTIMODAL_LAMBDA` | `0.7` | 多模态融合权重 λ |
| `TIME_DECAY_HALF_LIFE_DAYS` | `365` | 时间衰减半衰期（天） |
| `SCENE_ENABLED` | `true` | 是否启用场景分析 |
| `SCENE_INFERENCE_ENDPOINT` | `""` | 场景推理服务端点（空=本地词典推理） |
| `SCENE_CACHE_HALFLIFE_DAYS` | `7` | 场景缓存半衰期（天） |
| `HUAWEI_ENABLED` | `false` | 是否启用华为云视觉识别 |
| `HUAWEI_FACE_ENABLED` | `false` | 是否启用人脸分类 |
| `HUAWEI_AK` / `HUAWEI_SK` | — | 华为云 AK/SK |
| `HUAWEI_PROJECT_ID` | — | 华为云项目 ID |
| `HUAWEI_IMAGE_ENDPOINT` | `https://image.cn-north-4.myhuaweicloud.com` | 华为云 Image 服务端点 |
| `HUAWEI_FRS_ENDPOINT` | `https://face.cn-north-4.myhuaweicloud.com` | 华为云 FRS 服务端点 |
| `FACE_GAUSSIAN_SIGMA` | `0.1` | 高斯聚类 σ |
| `FACE_PROBABILITY_THRESHOLD` | `0.85` | 聚类概率阈值 |
| `RATE_LIMIT_ENABLED` | `true` | 是否启用限流 |
| `RATE_LIMIT_QPS` | `50` | 限流 QPS |
| `RATE_LIMIT_BURST` | `100` | 限流突发容量 |
| `REDIS_ENABLED` | `false` | 是否启用 Redis 缓存后端 |
| `SUGGEST_ENABLED` | `true` | 是否启用搜索建议/纠错 |
| `EXPLAIN_ENABLED` | `true` | 是否启用搜索结果可解释性 |
| `DEDUP_ENABLED` | `true` | 是否启用搜索结果去重 |
| `IMAGE_QUERY_ENABLED` | `true` | 是否启用以图搜图 |
| `ALBUM_ENABLED` | `true` | 是否启用自动相册 |
| `NEGATION_ENABLED` | `true` | 是否启用否定语义搜索 |
| `STATS_ENABLED` | `true` | 是否启用统计大屏 |
| `WATERMARK_ENABLED` | `true` | 是否启用水印功能 |
| `FEEDBACK_ENABLED` | `true` | 是否启用搜索反馈 |
| `WRITEBACK_ENABLED` | `false` | 是否启用 AI 标签/相册写回 Immich |
| `WRITEBACK_MODE` | `idempotent` | 写回模式（`idempotent` / `rollback` / `audit`） |
| `PLUGIN_HOOK_TOKEN` | `""` | 插件 hook 端点鉴权 token（空=不鉴权） |

## 错误码体系

| 错误码 | HTTP | 触发场景 |
|--------|------|----------|
| `INVALID_PARAMS` | 400 | 参数校验失败 |
| `UNAUTHORIZED` | 401 | API Key 缺失或不正确 |
| `UPSTREAM_UNAVAILABLE` | 502 | Immich 连接失败 / 5xx |
| `UPSTREAM_TIMEOUT` | 504 | Immich 超时 |
| `UPSTREAM_ERROR` | 502 | Immich 4xx（鉴权/协议错误） |
| `VISION_UNAVAILABLE` | 502 | 华为云 FRS 不可用 |
| `VISION_TIMEOUT` | 504 | 华为云 FRS 超时 |
| `VISION_ERROR` | 502 | 华为云 FRS 4xx |
| `AI_ANALYZER_UNAVAILABLE` | 502 | AI 分析器上游不可用 |
| `AI_ANALYZER_TIMEOUT` | 504 | AI 分析器超时 |
| `SERVICE_UNAVAILABLE` | 503 | 功能未启用 |
| `RATE_LIMITED` | 429 | 请求触发限流 |
| `INTERNAL_ERROR` | 500 | 未捕获的内部异常 |

## 测试

```bash
# 单元测试 + 集成测试
pytest                    # 732+ passed

# 破坏性测试（六大类：输入畸形/网络故障/并发耗尽/数学边界/数据污染/安全攻击）
pytest brute_test.py      # 160 passed

# 端到端验收（需启动服务 + Immich）
python verify_integration.py --base-url http://127.0.0.1:8000
```

### 测试覆盖

- 请求模型校验、时间解析、排序算法、缓存淘汰
- Immich 客户端重试/超时/错误分类、搜索编排降级
- 人脸分类全流程（检测/入库/搜索/高斯聚类/分组）
- 复合条件搜索（严格交集/贝叶斯降级/加权评分）
- 多模态融合、查询意图动态权重、场景分析
- API 集成（鉴权/错误状态码/统一错误结构）
- 破坏性测试（SSRF/注入/并发/资源耗尽/数学边界）

## 部署

### Docker

```bash
docker compose up --build
```

`Dockerfile` 使用非 root 用户运行，`docker-compose.yml` 提供健康检查与自动重启。

### 日志与监控

- 结构化日志，每条响应携带 `x-request-id` 便于关联追踪
- `/health` 存活探针
- 可接入 Loki/ELK 集中式日志与 Prometheus 指标

## 关键设计决策

1. **指数衰减而非线性归一化**：`freshness = 2^(-age/half_life)` 单调、有界、对极老照片友好，不受单次结果集分布影响。拍摄时间来源优先级：EXIF > 文件创建时间 > 默认值，带溯源标记。

2. **高斯核概率软聚类**：`P(same_person) = exp(-((1-Sim)²)/(2σ²))`，替代硬阈值。相似度 0.61 与 0.63 不再被判定为完全不同的人，概率平滑过渡。σ=0.1，概率阈值=0.85。

3. **时间衰减 LFU 缓存**：`Heat = Freq·2^(-Age/HalfLife)`，同时考虑访问频次和时间衰减。热门照片因高 Freq 保持高 Heat，冷门搜索因低 Freq 自然淘汰，解决 LRU "冷门挤掉热门"的问题。

4. **多模态加权拼接**：Immich API 仅支持文本，通过重复 query N 次模拟向量级权重（`text_repeat = round(λ/(1-λ))`）。λ=0.7 → query 重复 2 次，vision_tags 1 次，近似 0.67:0.33 的权重比。

5. **贝叶斯降级检索**：严格交集为空时不简单去掉某个条件，而是分别独立搜索后加权合并。某条件完全无命中时，其他条件的部分命中仍能贡献评分，避免"全有或全无"的脆弱性。

6. **可插拔架构**：视觉识别、AI 分析器、场景推理器均为抽象基类 + 依赖注入，新增厂商实现只需新增适配器并在 `lifespan` 中装配，业务逻辑零改动。

## 团队协作

- GitCode 保护分支 + Pull Request 审查流程
- 全程使用华为云 CodeArts 辅助开发
- 已合并 16+ PR，732+ 单元测试 + 160 破坏性测试全部通过

## Immich 原生工作流部署

```bash
# 1. 启动服务
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 2. 创建默认工作流（在 Immich 中自动注册三个工作流）
curl -X POST http://127.0.0.1:8000/admin/api/workflows/setup \
  -H "Content-Type: application/json" \
  -d '{"hook_url": "http://127.0.0.1:8000/api/v1/plugin/hook"}'

# 3. 验收
python verify_integration.py
```

三个工作流将在 Immich 中自动生效：
- 照片上传 → 自动场景分析 + AI 标签写回
- 元数据提取完成 → 自动补全描述
- 标签写入完成 → 自动归档检查 + AI 相册创建