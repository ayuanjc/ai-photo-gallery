# AI 图像语义搜索微服务

基于 FastAPI 的 AI 图像语义搜索微服务，对接 [Immich](https://immich.app/) 开源照片管理平台与华为云 FRS 人脸识别服务，提供语义搜索、复合条件搜索、人脸分类、AI 图像分析四大核心能力。

## 功能特性

### 核心算法（6 项）

| # | 功能 | 公式 / 说明 |
|---|------|-------------|
| 1 | **语义搜索** | 对接 Immich `POST /api/search/smart`，自然语言查图 |
| 2 | **指数时间衰减二次排序** | `Final = α·2^(-Age/HalfLife) + β·Similarity`，新照片加权更高 |
| 3 | **查询意图动态权重** | `α_dynamic = α_base + γ·IntentScore`，按查询语义自动调节时间/相似度权重 |
| 4 | **多模态向量加权融合** | `V_final = λ·V_text + (1-λ)·V_vision`，加权拼接模拟向量融合（λ=0.7） |
| 5 | **华为云 FRS 人脸分类** | SDK-HMAC-SHA256 签名 + 高斯核概率软聚类 + 并查集分组 |
| 6 | **时间衰减 LFU 缓存** | `Heat = Freq·2^(-Age/HalfLife)`，替代 LRU，热门不淘汰 |

### 复合条件搜索（贝叶斯降级检索）

- 按 `person_id + location + query` 复合搜索，严格交集优先
- 交集为空时自动降级为加权评分合并：`Score = w1·S_person + w2·S_location + w3·S_query`
- 权重：person(0.5) > location(0.3) > query(0.2)

### 工程化能力

- 统一错误码体系（`AppError`，区分 400/401/502/503/504/500）
- API Key 鉴权（`hmac.compare_digest` 防时序攻击）
- 优雅降级（`degraded: true` + 缓存兜底，上游不可用不报 5xx）
- 可插拔 AI 架构（抽象基类 + 依赖注入，切换引擎无需改业务代码）
- SSRF 防护（拒绝内网/回环/链路本地地址）
- 错误响应脱敏（自动遮蔽密码/密钥/连接串）
- 场景词典与标注（10 大场景 + 子场景 + 视觉属性白名单）
- AI 图像分析（同一人 / 相似图 / 无关 三分类判定）

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/search/smart` | AI 语义搜索（视觉增强 + 二次排序） |
| POST | `/api/v1/search/multi-condition` | 复合条件搜索（贝叶斯降级检索） |
| POST | `/api/v1/faces/classify` | 人脸分类（检测 → 入库 → 搜索 → 聚类 → 分组） |
| POST | `/api/v1/ai/analyze` | AI 分析（同一人 / 相似图 / 无关判定） |
| GET | `/api/v1/scene/dict` | 获取场景词典（只读） |
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
│   │       ├── search.py                # 搜索路由（smart + multi-condition）
│   │       ├── faces.py                 # 人脸分类路由
│   │       ├── ai.py                    # AI 分析路由
│   │       └── scene.py                 # 场景词典路由
│   ├── core/
│   │   ├── config.py                    # pydantic-settings 配置
│   │   ├── errors.py                    # 错误码与领域异常体系
│   │   └── logging.py                   # 日志配置
│   ├── models/
│   │   ├── request.py                   # 请求模型（SearchRequest / MultiConditionSearchRequest）
│   │   ├── response.py                  # 响应模型
│   │   ├── face.py                      # 人脸分类请求/响应模型
│   │   └── ai.py                        # AI 分析请求模型
│   ├── services/
│   │   ├── immich.py                    # Immich 客户端（重试/超时/错误分类）
│   │   ├── ranking.py                   # 二次排序算法（衰减/动态权重/多模态融合/场景权重）
│   │   ├── search_service.py            # 搜索编排（视觉增强 + 场景标注 + 排序）
│   │   ├── face_service.py              # 人脸分类编排（检测/入库/搜索/高斯聚类）
│   │   ├── multi_condition_service.py   # 复合条件搜索（严格交集 + 贝叶斯降级）
│   │   └── vision/
│   │       ├── base.py                  # 视觉识别抽象基类 + 空实现
│   │       ├── huawei.py                # 华为云图像识别适配器（预留）
│   │       ├── huawei_face.py           # 华为云 FRS 人脸识别客户端
│   │       └── huawei_signer.py         # SDK-HMAC-SHA256 签名工具
│   ├── ai/
│   │   ├── base.py                      # AI 分析抽象基类 + 分类函数
│   │   ├── factory.py                   # 分析器工厂
│   │   ├── immich_analyzer.py           # 基于 Immich 的同人判定
│   │   └── huawei_analyzer.py           # 华为云人脸分析器（预留）
│   ├── scene/
│   │   ├── analyzer.py                  # 场景分析器（分级推理 + 缓存 + singleflight）
│   │   ├── dictionary.py                # 场景词典加载与白名单校验
│   │   ├── models.py                    # 场景标签数据模型
│   │   └── scene_dict.json              # 场景词典数据文件
│   └── utils/
│       ├── cache.py                     # 时间衰减 LFU 缓存
│       ├── time_parsing.py              # EXIF 时间解析与新鲜度计算
│       └── url_safety.py                # URL 安全校验（SSRF 防护）
├── tests/                               # 单元测试与集成测试（250+ 用例）
├── brute_test.py                        # 破坏性测试（160 用例，六大类场景）
├── .env.example                         # 环境配置示例
├── Dockerfile
├── docker-compose.yml
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
| `TIME_WEIGHT` | `0.3` | 时间权重 α |
| `SIMILARITY_WEIGHT` | `0.7` | 相似度权重 β |
| `QUERY_INTENT_GAMMA` | `0.4` | 查询意图动态权重强度 γ |
| `MULTIMODAL_LAMBDA` | `0.7` | 多模态融合权重 λ |
| `TIME_DECAY_HALF_LIFE_DAYS` | `365` | 时间衰减半衰期（天） |
| `SCENE_ENABLED` | `true` | 是否启用场景分析 |
| `SCENE_CACHE_HALFLIFE_DAYS` | `7` | 场景缓存半衰期（天） |
| `HUAWEI_ENABLED` | `false` | 是否启用华为云视觉识别 |
| `HUAWEI_FACE_ENABLED` | `false` | 是否启用人脸分类 |
| `HUAWEI_AK` / `HUAWEI_SK` | — | 华为云 AK/SK |
| `HUAWEI_PROJECT_ID` | — | 华为云项目 ID |
| `FACE_GAUSSIAN_SIGMA` | `0.1` | 高斯聚类 σ |
| `FACE_PROBABILITY_THRESHOLD` | `0.85` | 聚类概率阈值 |

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
| `INTERNAL_ERROR` | 500 | 未捕获的内部异常 |

## 测试

```bash
# 单元测试 + 集成测试
pytest                    # 250 passed

# 破坏性测试（六大类：输入畸形/网络故障/并发耗尽/数学边界/数据污染/安全攻击）
pytest brute_test.py      # 160 passed
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
- 已合并 12+ PR，250+ 单元测试 + 160 破坏性测试全部通过