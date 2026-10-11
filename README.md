# AI 图像语义搜索微服务

基于 FastAPI 的图像语义搜索微服务，对接 [Immich](https://immich.app/) 开源照片管理平台的语义搜索接口，并对结果做「拍摄时间 + 语义相似度」加权二次排序。同时预留华为云图像识别 API 的接入点。

## 功能特性

- 批量接收图片 URL 与自然语言关键词，Pydantic 严格校验。
- 调用 Immich `POST /api/search/smart` 执行语义搜索。
- 按 EXIF 拍摄时间（回退到文件创建时间、再回退到默认值）与语义相似度做加权融合排序；权重 α/β 可配置。
- 异步并发、超时、指数退避重试与降级机制。
- 统一响应模型与错误码体系，恰当的 HTTP 状态码与错误详情。
- 通过抽象基类 + 依赖注入预留华为云图像识别接口，与业务逻辑解耦。

## 项目结构

```
a01/
├── app/
│   ├── main.py                     # FastAPI 入口（lifespan、中间件、异常处理器、健康检查）
│   ├── api/
│   │   ├── deps.py                 # 依赖注入
│   │   └── routes/
│   │       └── search.py           # 搜索路由
│   ├── core/
│   │   ├── config.py               # pydantic-settings 配置
│   │   ├── errors.py               # 错误码与领域异常
│   │   └── logging.py              # 日志
│   ├── models/
│   │   ├── request.py              # 请求模型与字段校验
│   │   └── response.py             # 响应模型
│   ├── services/
│   │   ├── immich.py               # Immich 客户端（重试/超时/错误分类）
│   │   ├── ranking.py              # 加权二次排序算法
│   │   ├── search_service.py       # 搜索编排（视觉增强 + 搜索 + 排序）
│   │   └── vision/
│   │       ├── base.py             # 视觉识别抽象基类 + 空实现
│   │       └── huawei.py           # 华为云图像识别预留适配器
│   └── utils/
│       └── time_parsing.py         # EXIF 时间解析与新鲜度计算
├── tests/                          # 单元测试与集成测试
├── .env.example                    # 环境配置示例
├── Dockerfile
├── docker-compose.yml
├── requirements.txt / requirements-dev.txt
└── pyproject.toml
```

## 快速开始

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 IMMICH_BASE_URL 与 IMMICH_API_KEY

# 2. 安装依赖
pip install -r requirements-dev.txt

# 3. 启动服务
uvicorn app.main:app --reload

# 4. 打开接口文档
# http://localhost:8000/docs
```

## 请求 / 响应示例

### 请求

```json
POST /api/v1/search/smart
{
  "query": "海边日落",
  "image_urls": ["https://example.com/photo1.jpg", "https://example.com/photo2.jpg"],
  "limit": 20,
  "time_weight": 0.3,
  "similarity_weight": 0.7,
  "include_metadata_fields": ["width", "height", "latitude", "longitude", "city"]
}
```

### 成功响应（200）

```json
{
  "query": "海边日落",
  "total": 2,
  "degraded": false,
  "warnings": [],
  "enriched_query": "海边日落 沙滩 夕阳 海鸥",
  "results": [
    {
      "asset_id": "3f2c...",
      "thumbnail_url": "http://immich:2283/api/assets/3f2c.../thumbnail?size=preview",
      "original_url": "http://immich:2283/api/assets/3f2c.../original",
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

### 错误响应示例

```json
// 上游不可用（502）
{
  "error": {
    "code": "UPSTREAM_UNAVAILABLE",
    "message": "Immich 服务不可用: POST /api/search/smart",
    "request_id": "9f3a...",
    "details": "ConnectError..."
  }
}
```

## 错误码体系

| 错误码 | HTTP 状态码 | 触发场景 |
| --- | --- | --- |
| INVALID_PARAMS | 400 | 参数校验失败（空关键词、非法 URL、非法字段等） |
| UPSTREAM_UNAVAILABLE | 502 | Immich 连接失败 / 重试后仍 5xx |
| UPSTREAM_TIMEOUT | 504 | Immich 读/写超时 |
| UPSTREAM_ERROR | 502 | Immich 4xx（API Key 无效、请求被拒等） |
| INTERNAL_ERROR | 500 | 未捕获的内部异常 |

## 运行测试

```bash
pytest
```

测试覆盖：请求模型校验、时间解析、排序算法、Immich 客户端重试/超时/错误分类、搜索编排降级、API 集成（含错误状态码与统一错误结构）。

## 部署建议

### Docker

```bash
docker compose up --build
```

`Dockerfile` 使用非 root 用户运行，`docker-compose.yml` 提供健康检查与自动重启；`IMMICH_BASE_URL` 指向 Immich 服务名（如 `http://immich-server:2283`）。

### 日志与监控

- 结构化日志（`app/core/logging.py`），每条响应携带 `x-request-id` 便于关联追踪。
- 可将日志接入集中式日志系统（如 Loki/ELK）。
- 通过 `/health` 做存活探针；Prometheus 可在路由层 `/metrics` 扩展（starlette-exporter）。

### 配置管理

通过环境变量 / `.env`（`pydantic-settings`）管理，12-Factor 风格，敏感信息（API Key、AK/SK）不入库。

## 关键设计决策说明

1. **二次排序采用指数衰减加权融合**：`final = α·freshness + β·similarity`，其中 `freshness = 2^(-age/half_life)`。选择指数时间衰减而非对结果集做 min-max 归一化，是因为后者会受单次结果集分布影响而不稳定；指数衰减单调、有界、对极老照片友好，且无需感知整个结果集。α/β 由配置归一化到和为 1，支持单请求覆盖。拍摄时间来源有明确优先级（EXIF > 文件创建时间 > 默认值）且带回溯源标记。

2. **错误处理策略**：以领域异常（`AppError` 体系）贯穿服务层，路由层不感知 HTTP 细节；由全局异常处理器统一映射为 `{ error: { code, message, request_id, details } }` 结构与对应状态码。上游错误进一步分类（不可用 / 超时 / 业务错误），分别映射 502 / 504 / 502，便于调用方与告警系统精准区分。

3. **视觉识别扩展采用抽象基类 + 依赖注入**：业务层只依赖 `VisionProvider` 接口，华为云等厂商实现作为独立适配器在 `lifespan` 中按配置装配。这样新增厂商不需改动搜索编排逻辑，与现有业务完全解耦；且视觉识别失败只降级（记录 warning）不阻塞主流程。

4. **异步并发与降级**：httpx 连接池复用，视觉识别用 `asyncio.Semaphore` 限流并 `asyncio.gather` 并发；Immich 请求带连接/读写分设超时与指数退避重试。视觉识别任意失败均不影响返回，保证服务健壮。

## 与替代方案的对比

| 维度 | 本服务（Immich 搜索 + 二次排序） | 直接使用 Immich 前端搜索 | 自建 CLIP 向量检索 |
| --- | --- | --- | --- |
| 适用场景 | 需要定制排序、统一 API 面、聚合多图识别增强、服务集成 | 个人/小团队在 Immich UI 内快速搜图 | 完全可控的大规模私有语义检索 |
| 排序灵活性 | 高（时间/相似度权重可配） | 低（受 Immich 默认排序） | 高（但需自实现） |
| 扩展性 | 高（视觉/检索后端可插拔） | 低 | 中 |
| 运维成本 | 中（加一层服务） | 最低 | 高（自建模型与向量库） |
| 优点 | 不改 Immich、可加二次排序与多源增强、统一错误码 | 零开发 | 无外部依赖、可控性强 |
| 缺点 | 多一跳网络、需维护服务 | 无法定制排序与接口、不利于程序调用 | 开发/维护成本最高 |

## 后续扩展（预留）

- 补全 `HuaweiVisionProvider`：接入华为云图像识别标签/主体检测，将标签并入语义查询增强（已预留鉴权与接口占位）。
- 增加结果缓存（如 Redis）与限流，降低 Immich 压力。
- 接入 Prometheus 指标与链路追踪。