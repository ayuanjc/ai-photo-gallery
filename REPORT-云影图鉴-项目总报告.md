# 云影图鉴 · 项目总报告

> 生成时间：2026-11-21 20:52 (Asia/Shanghai)
> 依据：仓库实测（git HEAD a263fba）、本机运行结果、线上服务器 106.52.116.164 实时探测。
> 凡标「实测」的条目都是本次跑出来的数字，不是文档转抄；凡标「记忆」的条目来自赛事记录，无法用代码复核。

---

## 0. 一页速览

| 项目 | 值 |
| --- | --- |
| 作品名 | 云影图鉴（AI 语义图库检索与活动素材工作流） |
| 团队 | 光影智码团队（指导老师 张阳意） |
| 赛事 | 华为开发者大赛 · 珠海科技学院校赛（华为云码道 Agent 创新赛）专业组 |
| 成绩（记忆记录） | 决赛二等奖（第二名）+ 最佳人气奖 |
| 版本 | v1.0.1（打包参数口径；FastAPI app.version 仍是 1.0.0） |
| 主仓库 | atomgit.com/ayuanjc/photo-gallery-ai（origin） |
| 镜像 | github.com/ayuanjc/ai-photo-gallery |
| 分支 / HEAD | main / a263fba（2026-11-20 14:27） |
| 提交数 | 234（实测 `git rev-list --count HEAD`） |
| 应用代码 | 107 个 .py，18,892 行（实测） |
| 测试代码 | 76 个文件，17,216 行，1,134 条用例全绿（实测 462 s） |
| HTTP 接口 | 62 paths / 66 operations / 14 个 tag（实测 OpenAPI） |
| 数据资产 | data/ 共 420 MB，其中文本塔模型 406 MB（不可再生） |
| 本地演示 | http://127.0.0.1:8125（当前运行中，487 张索引） |
| 线上服务 | yunying-demo.service active，uvicorn 127.0.0.1:8100，487 张索引、512 维、加载 10.1 s |
| 线上入口 | 公网无入口（备案整改后 80 端口 default_server 直接 444），子域入口配置已就绪未上线 |

---

## 1. 作品要解决的问题

校园里图片「存得下、找不回」是常态：一个学期几十场活动，照片散在微信群、手机相册、网盘和社团硬盘里。做宣传推文的人真正要回答的问题不是「有没有这张图」，而是三件事：

1. 这场活动的照片到底有哪些？——需要**边界**，不能靠模型猜。
2. 用一句话把这些里最合适的几张挑出来——需要**语义检索**。
3. 挑好的图要能直接交付（带尺寸、带选择理由）——需要**成稿链路**。

云影图鉴对这三件事的答案是：**活动台账（活动照片清单）+ CLIP 语义检索 + 素材包（封面 3 / 正文 5，可换可删、可导出 zip）**，加上一个「先提案后落库」的归档流程。

差异化不在「能不能搜图」（开源方案很多），而在：

- **活动是数据边界，不是提示词**。活动名交给 CLIP 猜会串场，台账把这件事变成可审计的字段匹配（见第 4 节）。
- **每一步都留下可复核的痕迹**：每张入选的图都有 reason 字符串，导出的 zip 里 manifest.csv 就是选图依据；写回有审计与回滚。
- **不越权写库**：搜索本身零写回，归档必须 propose（dry-run）→ commit 两步，比赛演示期间可用 `COMPETITION_READONLY` 整条关掉。

---

## 2. 系统架构

### 2.1 分层

| 层 | 位置 | 实测规模 | 职责 |
| --- | --- | --- | --- |
| 接入层 | app/main.py（414 行） | 1 文件 | FastAPI 实例、lifespan、/health /livez /readyz、/static 挂载、14 个 router 装配 |
| 路由层 | app/api/routes/*.py | 15 文件 / 63 个路由装饰器 | 参数校验、限流、错误映射 |
| 依赖层 | app/api/deps.py（213 行） | — | 懒加载服务单例，缓存到 app.state |
| 服务层 | app/services/*.py | 40 文件 | 检索编排、写回、评分、去重、人脸、水印、导入、统计 |
| 领域层 | app/workflow/（新）、app/scene/、app/ai/ | 6 + 6 + 4 文件 | 台账与素材包；CLIP 改写与推理；图库分析器抽象 |
| 数据访问 | app/repositories/、app/db.py、app/services/immich.py | 4 文件 | 评分持久化（SQL/SQLite）、Immich HTTP 客户端 |
| 基础设施 | app/core/*.py | 8 文件 | 配置（322 行）、错误码、限流、缓存、Redis、运行时配置、日志 |

### 2.2 一次搜索请求的实际链路

```
POST /api/v1/search/smart  {activity_id?, query, limit, time_weight, min_rating...}
  → app/api/routes/search.py:34  smart_search()
  → app/services/search_service.py:59  SearchService.search()
      :124 effective_asset_ids = request.asset_ids
      1c 段  activity_id → ActivityStore.asset_ids，与 effective_asset_ids 求交
      :163  await immich.smart_search(clip_query, k=limit, asset_ids=effective)
      :178  上游不认 assetIds 时本地交集过滤
  → ranking（时间新鲜度 × 相似度加权）/ dedup / min_rating 过滤
  → app/models/response.py:43  SearchResponse（results, warnings, translated_query, cached...）
  → 若 writeback_enabled 且有写回：BackgroundTask → scene/album/description 三路写回
```

关键点：**搜索默认零写回**。写回是搜索的副作用分支，条件是 `not response.cached and settings.writeback_enabled`（app/api/routes/search.py:52-58），本机 .env 里 `WRITEBACK_ENABLED=false`，比赛演示不会动图库。

### 2.3 组件依赖

- 外部图库：Immich（只调 HTTP API，未改其源码，AGPL 合规声明见 readme/03-开源组件说明.md）。
- 推理：onnxruntime 1.31.0 CPU + CLIP ViT-B-32 文本塔（只跑文本塔，图片向量由 Immich 侧提供或预计算）。
- 缓存/限流：可选 Redis（默认关闭）。
- 持久化：SQLite（评分）/ 文件（台账、素材包、审计）。

---

## 3. 检索内核

### 3.1 CLIP 文本塔

| 项 | 实测值 |
| --- | --- |
| 模型 | ViT-B-32__openai，文本塔 onnx（121,549 B） |
| 外部权重 | data/clip_text/model.data，406,042,852 B，sha256 `e186e7b40015384dfeb953f2140f479e7f1bcb94df7aaf8cbda900759e7804ed` |
| onnx sha256 | `8c408803675fd10c3fc3da7b6db481f6db2026c1d89db1265d31e13ad9148afa` |
| 分词器 | tokenizer.json 3,642,073 B，context 77，BOS 49406 / EOS 49407 |
| 运行库 | onnxruntime **1.31.0**（必须 ≥1.31：1.20 读不了 prepacked 外部数据） |
| 向量维度 | 512 |
| 索引规模（线上/本地） | 487 条，embeddings.npy 997,504 B = 487×512 float32 |
| 提示词缓存 | prompt_emb.npz 280,576 B |
| 冷启动 | 线上 load_ms 10,102.4（含外部数据惰性映射） |

### 3.2 中文改写（app/scene/query_translate.py，1,373 行）

词典型短语组合器，不是 LLM：命中名词 + 形容词 + 镜头词才拼英文，否则原样透传。实测行为：

| 输入 | 改写结果 | translated |
| --- | --- | --- |
| 海边日落 | a photo of the sunset at the beach | true |
| 夜晚的舞台表演 | a photo of a performance at night | true |
| 舞台 灯光 | a photo of a lamp | true（0.25 ms） |
| 迎新晚会 / 志愿服务 / 大合照 | 原样透传 | false |

这条边界很重要：**活动名不在改写词典的能力范围内**，所以活动名必须走台账（见第 4 节）。界面保留「模型读到的英文短语」这一行，就是为了让评委看到模型到底理解成了什么。

### 3.3 排序与融合

- `app/services/ranking.py`（261 行）：time_freshness 与 similarity 加权，`time_weight` / `similarity_weight` 可调，`min_rating` 过滤。
- `app/services/dedup.py`（173 行）：近重复去除，响应里返回 `dedup_removed`。
- `app/services/negation_parser.py`（225 行）：否定词解析，响应 `applied_negations`。
- `app/services/time_parser.py`（234 行）+ `app/utils/date_parse.py`：时间提示（`time_hint`）解析。
- `app/services/multi_condition_service.py`（338 行）：多条件检索（/search/multi-condition）。
- `app/services/suggest_service.py`（240 行）+ `GET /search/correct`：拼写建议与纠正。
- `app/services/vector_index.py`（127 行）：向量索引封装；`app/core/cache.py` + `redis_client.py`：查询缓存。

---

## 4. 活动台账（本次改造 ①）

### 4.1 为什么不用模型猜活动

实测「迎新晚会」改写结果为 `translated=False`，即模型根本没理解这个短语；即使改写成功，CLIP 也只能给出「像晚会」的图，无法区分 2025 与 2026 两届。活动归属是**元数据问题**（时间、地点、组织者、谁拍的），必须用字段匹配解决。

### 4.2 数据模型

文件：`data/activities/activities.json`（由 `tools/build_activity_ledger.py` 从 `data/demo/meta.json` 按拍摄时间落窗生成）。

```json
{
  "id": "act-2026-xinying",
  "name": "迎新晚会",
  "aliases": ["迎新晚会", "2026 迎新", "迎新汇演"],
  "starts_at": "2026-09-01", "ends_at": "2026-10-11",
  "organizer": "校团委", "venue": "大学生活动中心",
  "cover_asset_id": "7f3d1c5f-...",
  "asset_ids": ["...85 个..."],
  "note": "占位素材：当前 demo 图库为通用样例，换校园授权图库后名称与素材一致"
}
```

当前台账（实测）：

| 活动 | 时间窗 | 张数 | 别名 | 组织者 / 地点 |
| --- | --- | --- | --- | --- |
| act-2026-xinying 迎新晚会 | 2026-09-01 → 2026-10-11 | 85 | 迎新晚会 / 2026 迎新 / 迎新汇演 | 校团委 / 大学生活动中心 |
| act-2026-biye 毕业合影 | 2026-06-01 → 2026-08-31 | 81 | 大合照 / 毕业合影 / 毕业照 | 学院办公室 / 图书馆前广场 |
| act-2025-zhiyuan 志愿服务 | 2025-08-01 → 2025-09-15 | 36 | 志愿服务 / 志愿活动 / 社区服务 | 青年志愿者协会 / 社区服务中心 |
| 合计 | — | **202** | — | — |

### 4.3 解析算法（app/workflow/activity_store.py，307 行）

1. **归一化**：去空白与中英标点，小写（字符集过滤实现，非正则，避免引号地狱）。
2. **别名精确**：命中即 matched，但要求 key 长度 ≥ `EXACT_MIN_KEY_LEN = 3`——否则 2 字别名（如「迎新」）会把长名活动压掉，产生静默错配。
3. **子串**：任意方向包含即候选。
4. **模糊**：difflib 相似度 ≥ `RATIO_THRESHOLD = 0.72`。
5. **日期解析**：按 `date` 落 `DATE_WINDOW_DAYS = 1` 天窗口。
6. **多命中**：不猜，返回 `status=ambiguous` + `need=pick_one` + 候选列表。
7. **噪声剥离**：查询里的「推文封面 / 正文配图 / 找图 / 照片」等词先剔除。

实测（主应用 TestClient）：`{"query":"迎新晚会"}` → matched（别名精确）；`{"query":"运动会"}` → none（不猜）；`{"query":"志愿服务"}` → matched → act-2025-zhiyuan。

### 4.4 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /api/v1/activities | 活动列表（含每场张数） |
| GET | /api/v1/activities/{activity_id} | 活动详情 |
| POST | /api/v1/activities/resolve | 一句话/日期 → 活动，matched / ambiguous / none |
| POST | /api/v1/activities/reload | 热重载台账文件 |

### 4.5 检索侧接入

`SearchRequest.activity_id`（app/models/request.py）→ `SearchService._activity_asset_ids()` 取白名单 → 与 `effective_asset_ids` 求交；交集为空直接返回 `warnings=["活动 X 未登记任何照片，检索范围为空"]`，而不是退回全库搜索。这是「锁定范围」在代码层真正的落点。

---

## 5. 素材包工作流（本次改造 ②）

### 5.1 状态模型

一次「找图」不再是一个响应就结束，而是一个**可编辑的会话**：

```
session_id  →  revision(整数)  →  activity  →  slots{cover:3, body:5}
                                →  pool（候选池，默认 60 张）
                                →  package{cover:[...], body:[...]}
                                →  used / rejected
```

落盘：`data/packages/{session_id}.json`（`app/services/package_store.py`，93 行，原子写 + TTL 3600 s，过期即删）。

### 5.2 槽位与方向

- `DEFAULT_SLOTS = {"cover": 3, "body": 5}`。
- 方向：`any / landscape / portrait / square`；方形判定 `abs(w - h) <= 5%`。
- 封面优先横图（推文封面比例），正文不限；指定方向时**不满足方向的已选项全部换掉**，从池内按排序补位。
- `replace` 的语义是「池内重排」，**不重跑检索**——这一点在 UI 上直接显示为 `池内重排（没重跑检索）`，是本项目对「反复搜图慢」这个体验痛点的回答。

### 5.3 每张图都带 reason

模板实测输出：

```
画面场景未分类（纯语义相似度命中）；拍摄 2026-10-04 12:58 在活动时段内；
321×480 竖向，适配封面；检索分 0.2423，池内第 1 位
```

四段分别是：场景/语义来源 → 时间在活动窗内 → 尺寸与方向适配 → 检索分与池内位次。这是归档与导出可复核的基础。

### 5.4 编辑与版本

`apply_ops(state, ops, expected_revision)` 支持 `replace / drop / add / set_slot`，一次最多 20 个操作；`expected_revision` 不匹配直接拒绝（防并发覆盖），成功则 `revision + 1` 并返回 `changes + diff`。实测 UI：点「换横图」→ revision 1 → 2，diff 行 `cover：+… / -…`。

### 5.5 导出

`POST /api/v1/workflow/package/{sid}/export` → zip：

| 条目 | 内容 |
| --- | --- |
| manifest.csv | UTF-8 BOM；列含 slot / index / asset_id / fileName / width / height / taken_at / score / reason |
| manifest.json | 结构同上，便于程序消费 |
| README.txt | 会话、活动、版本、生成时间、说明 |
| thumbs/{slot}-{idx}-{asset_id}.jpg | 抓取失败的写进 MISSING.txt，不静默丢 |

实测一次导出 11 个条目（3 + 8 张缩略图）。

### 5.6 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /api/v1/workflow/resolve-activity | 活动解析（工作流入口） |
| POST | /api/v1/workflow/search-in-activity | 锁范围检索 + 建包 |
| GET | /api/v1/workflow/package/{session_id} | 取会话 |
| POST | /api/v1/workflow/package/edit | 改包（revision 校验） |
| POST | /api/v1/workflow/package/{session_id}/export | 导出 zip |

---

## 6. 归档：提案 → 确认（本次改造 ③）

### 6.1 两步式

| 阶段 | 端点 | 行为 | 写库 |
| --- | --- | --- | --- |
| 提案 | POST /api/v1/archive/propose | 查同名相册、算 will_add / already_in、生成 proposal_id（内存，TTL 600 s） | 否（dry-run） |
| 确认 | POST /api/v1/archive/commit | `get_or_create_album` + 逐张 `add_to_album`，返回 album_id 与直链 | 是 |
| 查询 | GET /api/v1/archive/proposals/{proposal_id} | 剩余张数与剩余秒数 | 否 |

规则：台账外资产直接 422；同一提案重复 commit 拒绝；提案过期拒绝；相册名 `云影图鉴 · {活动名} {开始日期}`（前缀可配）；相册不存在则创建。

### 6.2 只读硬闸

`app/core/readonly.py`（27 行）+ `COMPETITION_READONLY=true`：propose 与 commit 直接抛 `FORBIDDEN`（HTTP 403）。`ErrorCode` 新增 `FORBIDDEN="FORBIDDEN"`，`STATUS_CODE_MAP` 映射 403。演示现场打开这个开关，写链路整体不可达，不需要依赖「操作者别点错」。

### 6.3 零写回守卫

`tests/test_archive_readonly.py` 里有一条**静态守卫**用例：读取 search / activities / workflow 三个路由文件的源码，断言不出现 `writeback` 调用。这是用测试固定「搜索不写库」这条承诺，而不是写在文档里。

---

## 7. 既有的写回体系（本次改造未改，但报告必须交代）

| 写回器 | 文件 | 落点 |
| --- | --- | --- |
| 场景标签 | app/services/scene_writeback.py（203 行） | Immich tag |
| 自动相册 | app/services/album_writeback.py（157 行，:85 `_group_by_scene_month`） | 按「场景 + 月份」分组建相册 |
| 描述与评分 | app/services/description_writeback.py（171 行，:95 `_generate_description` / :147 `_calculate_rating`） | 资产 description + rating |

底层客户端 `app/services/immich_writeback.py`（526 行）：:123 `add_tag`、:188 `get_or_create_album`、:227 `add_to_album`、:275 `update_description`、:348 `rollback`、:374 `list_albums`。

管理端回滚：`app/api/routes/admin.py` :86 `GET /admin/api/writeback/audit`、:112 `POST /admin/api/writeback/rollback`、:126 `POST /admin/api/writeback/rollback/asset/{asset_id}`。

去重：`writeback_dedup_ttl_seconds = 86400`（app/core/config.py:128）。

---

## 8. 接口全表（62 paths / 66 operations / 14 tags）

### 8.1 业务接口（/api/v1，前缀统一）

| 组 | 方法 | 路径 | 用途 |
| --- | --- | --- | --- |
| search | POST | /api/v1/search/smart | 智能语义检索（主入口，支持 activity_id） |
| search | POST | /api/v1/search/multi-condition | 多条件检索 |
| search | POST | /api/v1/search/feedback | 记录相关性反馈 |
| search | GET | /api/v1/search/suggest | 搜索建议 |
| search | GET | /api/v1/search/correct | 拼写纠正 |
| search | GET | /api/v1/search/albums | 自动相册视图（album_enabled=false 时 503） |
| activities | GET | /api/v1/activities | 活动列表 |
| activities | GET | /api/v1/activities/{activity_id} | 活动详情 |
| activities | POST | /api/v1/activities/resolve | 一句话 → 活动 |
| activities | POST | /api/v1/activities/reload | 台账热重载 |
| workflow | GET | /api/v1/workflow/resolve-activity | 工作流入口解析 |
| workflow | POST | /api/v1/workflow/search-in-activity | 锁范围检索 + 建包 |
| workflow | GET | /api/v1/workflow/package/{session_id} | 取素材包会话 |
| workflow | POST | /api/v1/workflow/package/edit | 改包（revision 校验） |
| workflow | POST | /api/v1/workflow/package/{session_id}/export | 导出 zip |
| archive | POST | /api/v1/archive/propose | 归档提案（dry-run） |
| archive | POST | /api/v1/archive/commit | 归档确认（写库） |
| archive | GET | /api/v1/archive/proposals/{proposal_id} | 提案状态 |
| rating | GET/POST/PUT/DELETE | /api/v1/ratings… | 评分 CRUD、批量、统计 |
| assets | GET | /api/v1/assets/{asset_id}/thumbnail | 缩略图代理 |
| assets | GET | /api/v1/assets/{asset_id}/original | 原图代理 |
| scene | GET | /api/v1/scene/dict | 场景词典 |
| faces | POST | /api/v1/faces/classify | 人脸分类（华为云开关控制） |
| ai | POST | /api/v1/ai/analyze | 图像分析 |
| import | POST | /api/v1/import/batch | 批量导入 |
| watermark | POST/GET | /api/v1/watermark/apply、/templates、/licenses | 水印引擎 |
| plugin | POST | /api/v1/plugin/hook | 码道/Coze 回调钩子 |
| stats | GET | /api/v1/stats/exif、/gear、/map | EXIF / 器材 / 地图统计 |
| system | GET | /health、/livez、/readyz | 健康与探针 |

### 8.2 管理接口（/admin，无 /api/v1 前缀，共 26 个路由装饰器）

覆盖：状态（/admin/api/status）、设置（/admin/api/settings）、验证（/admin/api/verify）、能力探测（/capabilities、/features）、同步（/sync、/sync/reset、/immich/workflows/sync、/immich/cleanup）、向量统计（/vector/stats）、ML 模型管理（/ml/health、/ml/models、加载/卸载）、回填（/backfill/scene、/backfill/status）、工作流定义（/workflows、/workflows/setup、/workflows/{wf_id}）、写回审计与回滚（/writeback/audit、/writeback/descriptions、/writeback/rollback…）、翻译预览（/api/search/translate）。

---

## 9. 数据与资产

### 9.1 体积（实测）

| 路径 | 大小 | 说明 |
| --- | --- | --- |
| app/ | 1.79 MB | 107 个 py |
| tests/ | 2.80 MB | 76 个文件 |
| data/ | 420 MB | 见下表 |
| demo/ | 166 KB | serve_demo.py 484 行 + web/index.html 335 行 |
| docs/ | 105 KB | 12 篇 |
| _site/ | 10.3 MB | 静态站 |
| ppt/ | 2.08 MB | v1.0.1 演示稿 |
| video/ | 5.13 MB | 演示视频 |
| codearts_proof/ | 1.56 MB | 码道使用证据包 |
| tools/ | 1.41 MB | 构建/打包/台账工具 |

### 9.2 关键文件（不可再生，务必备份）

| 文件 | 字节 | sha256 |
| --- | --- | --- |
| data/clip_text/model.data | 406,042,852 | `e186e7b40015384dfeb953f2140f479e7f1bcb94df7aaf8cbda900759e7804ed` |
| data/clip_text/model.onnx | 121,549 | `8c408803675fd10c3fc3da7b6db481f6db2026c1d89db1265d31e13ad9148afa` |
| data/clip_text/tokenizer.json | 3,642,073 | —（BOS 49406 / EOS 49407 / ctx 77） |
| data/demo/embeddings.npy | 997,504 | 487 × 512 float32 |
| data/demo/prompt_emb.npz | 280,576 | 提示词向量缓存 |
| data/demo/thumbs/*.webp | 7,677,702（487 张） | 480px 短边缩略图 |

### 9.3 打包陷阱（已踩过）

`tools/pack_submission.py:36` 有 `BIG_DATA_LIMIT = 10 * 1024 * 1024`，:139-144 对「路径段含 data 且 >10 MB」的文件一律跳过 → 406 MB 的 `model.data` 会被静默丢弃，压缩包解开跑不起来。用 `--allow` 显式放行，并在 readme/01 与 install.sh 里写清「运行时必须自备 model.data，或用服务器上的 /opt/yunying/clip」。

---

## 10. 测试与质量

| 指标 | 实测 |
| --- | --- |
| 用例总数 | 1,134（既有 1,115 + 本次新增 19） |
| 文件 | 76 个 |
| 代码行 | 17,216 |
| 全量耗时 | 462 s（0:07:42） |
| 结果 | 全绿，10 条 DeprecationWarning（on_event） |
| 命令 | `python -m pytest -o addopts="" -q --tb=short` |

要点：

- `pyproject.toml` 里配了 `addopts = "-q"`，命令行再加 `-q` 变 `-qq`，汇总行会被吞掉——所以要看汇总必须 `-o addopts=""`。
- `tests/conftest.py:35` 在没有 pytest-asyncio 时把 async test 包成 `asyncio.run`；`:20 isolated_runtime_config` 隔离运行时配置；`:58` settings fixture 用 `immich_base_url="http://immich.test"`，所有涉及 Immich 的用例都走假客户端。
- 新增两个文件：`tests/test_activities_workflow.py`（台账解析、方向适配、drop 回填、revision 冲突、导出、TTL）、`tests/test_archive_readonly.py`（403 只读闸、propose 零写入、commit 不可重复、台账外资产拒绝、源码静态守卫）。

本次修掉的三个真 bug（都已用测试固定）：

1. 多命中未回 `ambiguous`——2 字别名抢了长名活动，静默错配。
2. `drop` 后被同池回填复活——排除集没带上刚删的 asset_id。
3. `_collect_ids` 空回退返回 `[]`——不带参数时应该默认归档该活动全部照片。

---

## 11. 演示与部署

### 11.1 本地三步

```bash
mkdir -p tmp/demo-run && cp demo/serve_demo.py tmp/demo-run/
cd tmp/demo-run
DEMO_BASE=. DEMO_CLIP_DIR=../../data/clip_text ORT_THREADS=2 \
  python -m uvicorn serve_demo:app --host 127.0.0.1 --port 8125
# 打开 http://127.0.0.1:8125/
```

环境变量：`DEMO_BASE / DEMO_CLIP_DIR / DEMO_ACTIVITIES / DEMO_PACKAGE_TTL / ORT_THREADS`。

实测（本次）：health `{count:487, dim:512, activities:3, load_ms≈360}`；首屏 facts「范围锁定 3 场活动 / 图库 487 张 / 模型 ViT-B-32__openai」；查询「舞台 灯光」→ 24 张；建包 8 张；改包 revision 2；导出 11 条目；归档提案 `将新增 8 张 / 提案阶段零写入`。

### 11.2 线上拓扑（2026-11-21 实时探测）

```
106.52.116.164  Debian 12 · 广州 · 2C4G/5M · nginx 1.22.1
├─ :80/:443   nginx
│   ├─ probe-block (default_server)  → return 444   ← 裸 IP 直接断连（2026-10-25 备案整改结论）
│   ├─ probe-root  server_name ayuanjc.online → /opt/demo-site（「探针」项目）
│   └─ demo-site   listen 8090 → /opt/demo-site 静态
├─ :8100  127.0.0.1  uvicorn yunying-demo.service（active since 2026-10-30 13:18，MemoryMax 1.5G）
│            GET /api/health → {"status":"ok","model":"ViT-B-32__openai","count":487,"dim":512,"load_ms":10102.4}
└─ /opt/yunying 723 MB（app/ clip/ data/ venv/ web/）· /opt/demo-site 6.0 MB
```

结论：服务与数据都在，**公网入口目前不存在**（8090 未对外开放、80 端口 444）。2026-10-25 的整改把「云影图鉴」从 nginx 引用里摘掉，文件保留。

### 11.3 三条恢复路线

| 方案 | 做法 | 代价 | 适用 |
| --- | --- | --- | --- |
| A | demo-site(8090) 加 /api 反代 + 开端口 | 低，但要动备案相关端口 | 临时 |
| B | 新建 yunying.ayuanjc.online vhost + 证书 | 中（DNS + certbot） | **推荐**，配置模板已写好 |
| C | 只走 ssh -L 隧道 | 最低 | 比赛已结束、只给自己人看 |

模板：`demo/nginx.yunying-subdomain.conf`（listen 80 + server_name yunying.ayuanjc.online，root /opt/yunying/web，/thumbs/ alias，/api/ proxy 127.0.0.1:8100，**不含 default_server**，与 444 结构并存）。

部署手册：`docs/在线演示站-活动工作流-部署与验收.md`（215 行，含 5 步上线、环境变量表、换校园图库 6 步、队外盲跑验收表、回滚、评委三问）。

### 11.4 一个必须写进交接的坑

`app.mount("/", StaticFiles(...))` 必须放在文件最后。Starlette 按注册顺序匹配，`Mount("/")` 会吞掉其后注册的所有 `/api/*`，症状是前端按钮报 `Method Not Allowed` 而 curl 同一路径 200——看起来像前端 bug，实际是路由顺序。已在 demo 代码里加注释固定。

---

## 12. 交付物清单

| 类别 | 路径 | 说明 |
| --- | --- | --- |
| 主仓库 | atomgit.com/ayuanjc/photo-gallery-ai | 234 提交，main |
| 参赛包参数 | submission.json | pack_submission.py 的输入（团队/版本/电话） |
| 演示稿 | ppt/云影图鉴-光影智码团队-v1.0.1.pptx（2.0 MB） | 14 页大纲见 docs |
| 演示视频 | video/…v1.0.1.mp4（5.13 MB） | — |
| 码道证据 | codearts_proof/（1.56 MB） | 00-总览 / 01-文件改动统计 / 02-提示词库 / 03-DIFF 样例 / 04-工具使用统计 / sessions / 团队协作 / MANIFEST |
| 静态站 | _site/（10.3 MB） | — |
| 说明文档 | readme/01–05 + install.sh | 环境、部署、开源组件、码道总结、项目地址 |
| 技术文档 | docs/ 12 篇 | 见下 |
| 交接与报告 | HANDOVER-云影图鉴.md（237 行）、REPORT-v2四项改造可行性.md、本报告 | 仓库根 |

docs 目录 12 篇：PPT提示词-精简版、前端改版提示词-给华为、参赛PPT大纲-14页、参赛PPT大纲-修正指令、在线演示站-活动工作流-部署与验收、在线演示站-部署与运维、场景重定位与答辩准备、实时监测面板、审查质量报告、服务器隧道与Telegram通道、比赛视频录制指南、现场演练题与演示检查清单。

---

## 13. 风险与雷区（12 条）

1. **别改码道项目名**。历史会话索引会断，证据链失效。
2. **码道数字会漂移**。对外只报稳定值：24 会话 / 143 提示词 / 9 天 / 1,115（现 1,134）测试 / 51（现 62）接口 / 234 提交。现场先看后台再报数。
3. **AGPL**。只调 Immich HTTP API，未改其源码；readme/03 有声明。若将来 embed Immich，整个项目要按 AGPL 处理。
4. **demo 必须用真实余弦**。任何 mock 分数或伪造排序，现场一问答不上。
5. **保留「模型读到的英文短语」这一行**。这是可解释性的证据，去掉就退化成黑盒。
6. **app/scene/query_translate.py 与 demo/query_translate.py 是两份拷贝**。改一处必须同步另一处，否则演示与线上行为不一致。
7. **security-log/ 属另一个项目**（智汇摄影站），交付必须排除。
8. **.gitignore 有 297 行未提交 diff**，其中 144 行是 CRLF 抖动，实际内容变更只 +9 行（新增 _site/、_shot.png、_detail.md、_diagram.html、_intro.txt、_publish_params.json、_cover.png）。提交前用 `git diff -w` 看。
9. **80 端口 444 是备案整改结论**，不要为了演示关掉它。
10. **服务器 /opt/yunying 是 ReadOnlyPaths**，demo 的素材包会话只能存在内存，刷新即失效是设计而不是缺陷。
11. **打包 10 MB 限制**会静默丢模型权重（第 9.3 节）。
12. **onnxruntime 必须 ≥1.31**。低版本读不了 prepacked 外部数据，报错信息很难懂。

---

## 14. 未完成 / 下一步

| # | 事项 | 阻塞点 | 预估 |
| --- | --- | --- | --- |
| 1 | 换校园授权图库（真活动名 + 真照片） | 需要真实素材与授权 | 1 人日（docs 第 5 节 6 步） |
| 2 | 子域上线（DNS + vhost + certbot） | 需要域名操作与服务器 | 0.5 人日 |
| 3 | 队外同学 6 步盲跑验收 | 需要人 | 0.5 人日 |
| 4 | 演示期打开 COMPETITION_READONLY | 决策 | 1 分钟 |
| 5 | 台账从真实图库重生成（tools/build_activity_ledger.py） | 依赖 1 | 0.5 人日 |
| 6 | git 收尾：535 个未跟踪/修改文件分类提交 | 需要决策哪些进版本库 | 1 人日 |

---

## 15. 附录

### 15.1 关键文件行号索引（改代码前先看这张表）

| 文件 | 位置 | 内容 |
| --- | --- | --- |
| app/main.py | :23-35 | routes 导入元组（含新增 activities/archive/workflow） |
| app/main.py | :257-274 | FastAPI 实例与 openapi_tags |
| app/main.py | :277-287 | include_router 区块（/api/v1 前缀，admin 例外） |
| app/main.py | :291 | StaticFiles 挂载 /static |
| app/main.py | :379 | GET /health（主应用健康检查是 /health，不是 /api/health） |
| app/services/search_service.py | :59 | SearchService.search |
| app/services/search_service.py | :124 | effective_asset_ids |
| app/services/search_service.py | :163-165 | immich.smart_search |
| app/services/search_service.py | :178-190 | 上游不支持 assetIds 时的本地交集 |
| app/services/immich_writeback.py | :123 / :188 / :227 / :275 / :348 / :374 | add_tag / get_or_create_album / add_to_album / update_description / rollback / list_albums |
| app/services/album_writeback.py | :27 / :32 / :85 | AlbumWriteBack / writeback_albums / _group_by_scene_month |
| app/services/description_writeback.py | :47 / :52 / :95 / :147 | DescriptionWriteBack / writeback_descriptions / _generate_description / _calculate_rating |
| app/api/routes/admin.py | :86 / :112 / :126 | 审计 / 回滚 / 单资产回滚 |
| app/api/routes/search.py | :34 / :102 / :114 / :157 / :177 / :203 | smart_search / multi_condition / record_feedback / suggest / correct / list_albums |
| app/core/config.py | :126 / :128 / :240-:246 | writeback_enabled / dedup_ttl / album_* |
| app/core/config.py | :246 之后 | activities_path / packages_dir / package_ttl_seconds / archive_proposal_ttl_seconds / activity_album_prefix / competition_readonly |
| app/workflow/activity_store.py | 全文 307 行 | 台账解析 |
| app/workflow/package.py | 全文 343 行 | 素材包 |
| app/services/workflow_service.py | 全文 243 行 | 检索→建包→改包→导出 |
| app/services/archive_service.py | 全文 208 行 | propose / commit |
| app/services/package_store.py | 全文 93 行 | 会话落盘与 TTL |
| app/core/readonly.py | 全文 27 行 | 只读硬闸 |
| app/models/activity.py | 全文 112 行 | 新端点模型 |

### 15.2 命令速查

```bash
# 代码规模
Get-ChildItem -Recurse app -Filter *.py | Measure-Object -Property Length
# 全量测试（注意 -o addopts=""）
python -m pytest -o addopts="" -q --tb=short
# 只跑本次新增
python -m pytest -o addopts="" -q --tb=short tests/test_activities_workflow.py tests/test_archive_readonly.py
# 接口数量
python -c "import app.main as m;print(len(m.app.openapi()['paths']))"
# 台账重生成
python tools/build_activity_ledger.py --meta data/demo/meta.json --out data/activities/activities.json
# 本地起 demo
DEMO_BASE=. DEMO_CLIP_DIR=../../data/clip_text python -m uvicorn serve_demo:app --port 8125
# 线上状态
ssh ayuanjc-1 'systemctl is-active yunying-demo; curl -s http://127.0.0.1:8100/api/health'
```

### 15.3 一句话总结

云影图鉴不是一个「AI 搜图 demo」，而是把**活动边界、检索、成稿、归档**串成一条可复核链路的校园图库工具；本次改造把「活动」从一个搜索词变成了数据边界，把一次搜索变成了可编辑可导出的素材包，把写库从搜索副作用变成了两步确认，并用测试和只读开关把这三件事钉住。剩下的是素材与上线，不是代码。
