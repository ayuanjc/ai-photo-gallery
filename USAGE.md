# 云影图鉴 · AI 图像语义搜索微服务 — 使用文档

> 基于 FastAPI 的 AI 图像语义搜索微服务，集成 [Immich](https://immich.app/) 照片管理平台（工作流事件原生触发，写回走公开 API）：中文语义搜图、CLIP 真实场景分析、AI 标签/描述/评分/相册自动写回。
>
> - 服务代码：本仓库（分支 `feat/immich-embed-final`）
> - 在线接口文档（Swagger）：`http://127.0.0.1:8000/docs`
> - 管理控制台：`http://127.0.0.1:8000/admin`

---

## 1. 系统架构

```
┌──────────────┐   x-api-key    ┌─────────────────────┐
│  浏览器/客户端 │ ─────────────▶ │  本服务 (FastAPI)     │
│  admin.html  │                │  127.0.0.1:8000     │
└──────────────┘                └─────────┬───────────┘
                                          │ x-api-key
                              ┌───────────▼───────────┐
                              │  Immich (Docker)      │
                              │  127.0.0.1:3200       │
                              │  · smart search (CLIP) │
                              │  · 标签/相册/描述 API   │
                              └───────────┬───────────┘
                              ┌───────────▼───────────┐
                              │  Immich ML 容器        │
                              │  127.0.0.1:3003       │
                              │  · ViT-B-32 CLIP 推理  │
                              └───────────────────────┘
```

三个服务角色：

| 服务 | 地址 | 用途 |
|------|------|------|
| 本微服务 | `127.0.0.1:8000` | 搜索编排、二次排序、场景分析、写回管理 |
| Immich 主服务 | `127.0.0.1:3200` | 照片库、CLIP 语义搜索、标签/相册存储 |
| Immich machine-learning | `127.0.0.1:3003` | CLIP 模型推理（图片/文本 embedding） |

> ⚠️ ML 容器默认不暴露宿主端口。需在 Immich 的 `docker-compose.yml` 中为 `immich-machine-learning` 添加 `ports: ["127.0.0.1:3003:3003"]` 后 `docker compose up -d immich-machine-learning` 重建。

---

## 2. 安装

### 环境要求

- Python 3.12+
- Docker + Immich v3.3.x（含 machine-learning 服务）

### 步骤

```bash
# 1. 安装依赖（建议虚拟环境）
pip install -e .

# 2. 开发/测试依赖
pip install -e ".[dev]"

# 3. 复制配置模板并填写
cp .env.example .env
```

### 必填配置（.env）

```ini
# Immich 连接（API Key 在 Immich 管理 → API Keys 中生成）
IMMICH_BASE_URL=http://127.0.0.1:3200
IMMICH_API_KEY=<你的 Immich API Key>

# CLIP 场景推理（接通后才产出真实 AI 分析结果）
SCENE_INFERRER_TYPE=clip
SCENE_INFERENCE_ENDPOINT=http://127.0.0.1:3003
SCENE_CLIP_MODEL=ViT-B-32__openai

# 写回开关（仅 source=analyzed 的真实分析结果才会写回）
WRITEBACK_ENABLED=true
```

其余配置均有合理默认值，完整清单见 `.env.example` 注释。

---

## 3. 启动与停止

### 启动

```bash
# 前台启动
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 后台启动（推荐，日志落 data/uvicorn.log）
nohup python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 > data/uvicorn.log 2>&1 &
```

### 验证

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}

# 确认 CLIP 预热完成（日志出现即表示场景提示词就绪）
grep "CLIP 场景提示词预热完成" data/uvicorn.log
# INFO app.scene.clip_inferrer CLIP 场景提示词预热完成: 67 个 key
```

### 停止

```bash
# Windows (git-bash)
netstat -ano | grep ":8000" | grep LISTENING   # 找到 PID
taskkill //F //PID <PID>
```

---

## 4. 快速开始

### 4.1 管理页面（推荐入口）

浏览器打开 `http://127.0.0.1:8000/admin`，内置：

- **AI 搜索页**：中文输入即搜，结果卡片显示场景标签、子场景、相似度与排序解释
- **统计大屏**：地图足迹 / EXIF 分布 / 器材偏好
- **写回管理**：审计日志查询、按资产/全量一键回滚
- **功能开关**：运行时配置（优先级高于 .env，存于 `data/runtime_config.json`）
- **工作流管理**：Immich 工作流的同步/删除/状态

### 4.2 命令行调用

```bash
# 语义搜索（如 .env 配置了 SEARCH_API_KEY，需加 -H "x-api-key: <KEY>"）
curl -X POST http://127.0.0.1:8000/api/v1/search/smart \
  -H "Content-Type: application/json" \
  -d '{"query": "夜晚城市霓虹灯", "limit": 5}'
```

响应示例（节选）：

```json
{
  "query": "夜晚城市霓虹灯",
  "translated_query": "a photo taken at night, a photo of neon signs at night",
  "total": 3,
  "results": [
    {
      "asset_id": "4191c104-...",
      "final_score": 0.947,
      "similarity_score": 1.0,
      "thumbnail_url": "/api/v1/assets/4191c104-.../thumbnail",
      "scene": {
        "primary_scene": "night",
        "scenes": ["night", "event"],
        "sub_scenes": ["neon", "festival"],
        "confidence": {"neon": 0.9, "night": 0.7},
        "source": "analyzed"
      }
    }
  ]
}
```

> 中文测试提示：git-bash 下 curl 传中文 JSON 可能编码失败，建议用 Python httpx 或管理页面。

---

## 5. API 一览

全部业务接口挂载在 `/api/v1` 前缀下；`/admin/*` 为管理接口。

### 5.1 搜索

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/search/smart` | **AI 图像语义搜索**（核心接口） |
| POST | `/api/v1/search/multi-condition` | 复合条件搜索（人物+地点+关键词，贝叶斯降级） |
| POST | `/api/v1/search/feedback` | 点击/跳过反馈（微调排序） |
| GET | `/api/v1/search/suggest` | 搜索建议（热门词 LRU） |
| GET | `/api/v1/search/correct` | 搜索纠错（Levenshtein 编辑距离） |
| GET | `/api/v1/search/albums` | 自动相册生成 |

`/search/smart` 主要请求参数：

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `query` | str | 必填 | 自然语言查询（中文自动翻译为英文 CLIP 检索词） |
| `limit` | int | 50 | 结果上限 1-200 |
| `asset_ids` | list | 空 | 限定资产范围搜索 |
| `time_weight` / `similarity_weight` | float | 0.3/0.7 | 排序权重覆盖 |
| `sort_by` | str | relevance | newest / oldest / random / relevance |
| `min_rating` | int | 无 | 按评分过滤 |
| `include_metadata_fields` | list | 空 | 附加 EXIF 字段 |

关键响应字段：

| 字段 | 说明 |
|------|------|
| `translated_query` | 中文查询翻译后的英文检索词（未翻译为 null） |
| `scene.source` | `analyzed`=真实 AI 分析；`inferred`=查询推断兜底；`fallback`=默认兜底 |
| `degraded` | 是否降级（上游部分可用） |
| `cached` | 是否命中缓存 |

### 5.2 场景

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/scene/dict` | 场景词典（10 大场景 + 57 子场景 + 视觉属性白名单） |

### 5.3 AI 分析 / 人脸

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/ai/analyze` | 同一人 / 相似图 / 无关 三分类判定 |
| POST | `/api/v1/faces/classify` | 人脸分类（华为云 FRS，默认关闭） |

### 5.4 资产 / 评分 / 水印 / 统计

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/assets/{id}/thumbnail` | 缩略图代理（免前端直连 Immich） |
| GET | `/api/v1/assets/{id}/original` | 原图代理 |
| POST/PUT/GET/DELETE | `/api/v1/ratings/*` | 资产评分 CRUD 与统计 |
| GET | `/api/v1/watermark/templates` | 水印模板列表 |
| POST | `/api/v1/watermark/apply` | 批量应用水印 |
| GET | `/api/v1/stats/map` `/stats/exif` `/stats/gear` | 地图足迹 / EXIF / 器材统计 |

### 5.5 Immich 集成

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/plugin/hook` | Immich 工作流 webhook 回调（上传自动打标） |
| POST | `/api/v1/batch-import` | 批量导入图片元数据 |
| GET/POST | `/admin/api/writeback/audit` `/rollback` | 写回审计 / 一键回滚 |
| POST | `/admin/api/sync` | 增量水位同步 |
| GET | `/admin/api/capabilities` | Immich 能力探测 |

---

## 6. AI 场景分析机制（重要）

### 6.1 三级推理链

```
第一级：Immich smartInfo/tags 白名单映射（v3.3 恒为空，几乎不命中）
第二级：CLIP 零样本推理（核心）
  ├─ 启动时预热 67 个场景提示词的文本 embedding
  ├─ 推理时下载缩略图 → 图片 embedding
  └─ 余弦相似度 ≥ 阈值命中：大场景 0.25 / 子场景 0.26
第三级：兜底（不写回）
  ├─ 查询推断：查询词命中提示词表 → source=inferred（仅展示）
  └─ 默认场景：plugin_fallback_scene → source=fallback（仅展示）
```

### 6.2 中文查询自动翻译

Immich 的 CLIP 模型（ViT-B-32__openai）以英文语料训练，**中文查询直接检索无效**。本服务自动将中文关键词映射为英文提示词后再检索：

```
"巍峨群山" → "a photo of mountains, a photo of natural scenery"
```

- 原始查询保留用于展示与反馈，仅发给 Immich 的检索词被翻译
- 翻译结果回填在响应 `translated_query` 字段
- 英文查询原样透传

### 6.3 写回闸门（防污染铁律）

**只有 `source=analyzed`（真实 CLIP 分析）的结果才会写回 Immich**。查询推断/兜底的猜测值仅用于前端展示，防止猜测被当事实污染图库。

写回内容（全部幂等、可撤销、有审计）：

| 类型 | 格式 | 示例 |
|------|------|------|
| 标签 | `AI·<场景中文名>` | `AI·夜景` `AI·猫` `AI·宠物` |
| 相册 | `AI·<场景>·YYYY-MM` | `AI·夜景·2026-07` |
| 描述 | 场景+视觉属性中文描述 | `AI 识别：夜景，城市灯光，霓虹` |
| 评分 | final_score → 1-5 星 | — |

审计日志：`data/writeback_audit.jsonl`；回滚入口：管理页面「写回管理」或 `POST /admin/api/writeback/rollback`。

### 6.4 排序算法

```
final_score = α · 时间新鲜度 + β · 语义相似度
```

- α/β 基础值 0.3/0.7，按查询意图动态调整（"最近/昨天"→偏时间；"老照片/童年"→偏相似度）
- Immich 不返回显式相似度分，按返回排名线性反推（第 1 名 1.0 → 末位 0.5）
- 时间新鲜度指数衰减：`2^(-距拍摄天数/365)`

---

## 7. Immich 工作流自动打标（可选）

服务启动时自动同步三个默认工作流到 Immich（`IMMICH_WORKFLOW_SYNC_ENABLED=true`）：

| 工作流 | 触发 | 动作 |
|--------|------|------|
| `yunying-on-upload` | 新照片入库 | CLIP 场景分析 + 标签写回 |
| `yunying-on-metadata` | 元数据提取完成 | 元数据补全 |
| `yunying-on-tagged` | 打标完成 | 归档检查 + 相册写回 |

配置要点：Immich 在 Docker 中运行时，`PLUGIN_CALLBACK_BASE_URL` 必须用 `http://host.docker.internal:8000`（不能用 127.0.0.1）。

---

## 8. 测试

```bash
# 全量测试（1027 个用例，约 3 分钟）
python -m pytest

# 单个测试
python -m pytest tests/test_clip_inferrer.py -v
python -m pytest -k test_name

# 代码检查
ruff check app/ tests/
```

测试环境隔离：`tests/conftest.py` 自动屏蔽真实 `SCENE_INFERENCE_ENDPOINT`，全量测试不依赖 Immich/ML 运行状态。

---

## 9. 常见问题（FAQ）

**Q1：启动报 `ImportError: cannot import name 'sentinel' from 'typing_extensions'`**

anyio 4.15+ 要求新版 typing_extensions。执行 `pip install -U typing_extensions` 后重启。

**Q2：搜索结果 `scene.source` 全是 `inferred`/`fallback`，没有 `analyzed`**

CLIP 推理未接通。检查：① `.env` 是否配置 `SCENE_INFERRER_TYPE=clip` 与 `SCENE_INFERENCE_ENDPOINT`；② ML 容器端口映射（见第 1 节）；③ `curl http://127.0.0.1:3003/ping` 是否返回 pong；④ 日志中是否有"CLIP 场景提示词预热完成"。

**Q3：中文搜索结果完全不相关**

确认响应中有 `translated_query` 字段。若为 null 且查询含中文，说明翻译表未命中该词——可向 `app/scene/fallback.py` 的 `_QUERY_SCENE_HINTS` 补充词汇。

**Q4：搜索有缓存，改了配置不生效？**

搜索结果缓存 TTL 默认 300 秒（`SEARCH_CACHE_TTL_SECONDS`），场景分析缓存默认 1 天。验证新逻辑请换新查询词绕过缓存，或重启服务（进程内缓存清空）。

**Q5：写回了错误标签，如何撤销？**

管理页面「写回管理」→ 选择资产或全量回滚；或调 `POST /admin/api/writeback/rollback`（body: `{"asset_id": "..."}` 或全量）。所有历史操作在 `data/writeback_audit.jsonl`。

**Q6：服务响应 401**

`.env` 配置了 `SEARCH_API_KEY` 时，请求必须携带 `x-api-key` 请求头。

**Q7：Windows git-bash 下 curl 中文查询报错/乱码**

git-bash 的 curl 对 UTF-8 JSON 支持不稳定，改用 Python：

```python
import httpx
r = httpx.post("http://127.0.0.1:8000/api/v1/search/smart",
               json={"query": "夜晚城市霓虹灯", "limit": 5}, trust_env=False)
print(r.json())
```

---

## 10. 目录速览

```
app/
├── main.py                 # 应用入口、服务装配、CLIP 预热
├── core/config.py          # 全部配置项（Settings）
├── api/routes/             # 路由：search/scene/ai/faces/stats/...
├── scene/
│   ├── clip_inferrer.py    # CLIP 零样本推理器 + 场景提示词表
│   ├── analyzer.py         # 三级推理链 + 场景缓存
│   ├── query_translate.py  # 中文查询 → 英文 CLIP 检索词
│   ├── fallback.py         # 查询推断/默认场景兜底
│   └── scene_dict.json     # 场景词典（白名单）
├── services/
│   ├── search_service.py   # 搜索编排（翻译/搜索/排序/组装）
│   ├── ranking.py          # 二次排序算法
│   ├── immich.py           # Immich 客户端
│   └── *_writeback.py      # 标签/相册/描述写回
└── static/admin.html       # 管理控制台（AI 搜索页）
data/                       # 运行时数据（日志/审计/配置，不入库）
tests/                      # 1027 个测试用例
```