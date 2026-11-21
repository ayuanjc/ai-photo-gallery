# 云影图鉴 v2：四项优先级改造可行性报告

结论先行：四条都能做，不需要重写系统。拆成三层新增（台账数据层 → 编排接口层 → 确认归档层）+ 一个新入口，合计约 6–7 人日。其中 ①②③ 是在现有 FastAPI 内核上做「新增模型 + 新增路由 + 复用写回服务」，④ 是重新做样例库与入口，不碰主业务代码。

---

## 0. 先复核评审查出的两个事实（我用代码和实测确认了）

### 0.1 中文改写：属实，且不是 bug，是词表覆盖问题

我直接跑了 `demo/query_translate.py`：

| 输入 | translate_query_for_clip | translated | nouns |
|---|---|---|---|
| 海边日落 | `a photo of the sunset at the beach` | True | 海边→the beach / at the beach；日落→the sunset / at sunset |
| 夜晚的舞台表演 | `a photo of a performance at night` | True | 夜晚→a night scene / at night；舞台表演→a performance |
| 迎新晚会 | 原样透传 | False | [] |
| 志愿服务 | 原样透传 | False | [] |
| 大合照 | 原样透传 | False | [] |

机制：`app/scene/query_translate.py`（52.8 KB）是**词典驱动**的短语组合器——名词表 + 形容词表 + 镜头词表，命中就拼成英文短语，未命中就 fallback 原文。校园专有名词（迎新晚会/志愿服务/大合照）不在词表，于是整句中文直接进 CLIP 文本塔，而 CLIP 的 BPE 对中文 token 覆盖率很低，召回必然差。

三条路，按性价比排：

1. **把活动名从语义查询里剥出来，当结构化过滤**（推荐）。活动名不该交给 CLIP 猜，它是元数据不是画面语义。这也是优先级 1 的真正理由。
2. 扩词表：校园类目约 40 个词（晚会/舞台/合唱/颁奖/合影/军训/运动会/志愿服务/横幅/展板/签到…），改 `query_translate.py` 一处，两份拷贝都要改。
3. 把改写搬到模型侧：Coze 里先让 LLM 输出英文 query，再调 HTTP 接口。零代码侵入，但依赖平台。

### 0.2 试用入口：属实，且压缩包结构上不可能跑起来

- `tools/pack_submission.py:36`：`BIG_DATA_LIMIT = 10 * 1024 * 1024`；`:139-144`：任何路径段含 `data` 且单文件 >10 MB 直接跳过。`data/clip_text/model.data` 是 **406,042,852 B**，必然被丢弃；`model.onnx`(121 KB)、`tokenizer.json`(3.6 MB)、`embeddings.npy`(997 KB) 反而会进包。
- `demo/serve_demo.py:29`：`CLIP = Path(os.environ.get("DEMO_CLIP_DIR", "/opt/yunying/clip"))` —— 默认读服务器绝对路径。
- 所以「轻量 Demo」从来不轻量：要有 venv（onnxruntime + tokenizers + numpy/fastapi）、要有 406 MB 外部权重、要有 3.6 MB tokenizer。评委解压后无法自启动，是设计使然，不是打包失误。

结论：优先级 4 的正确形态不是「把模型塞进 zip」，而是**一个不需要本地环境的线上实例 + 一套自带的校园样例库**。

---

## 1. 现状基线（本报告依据的代码事实）

| 事实 | 位置 |
|---|---|
| 全仓无 activity/活动台账概念 | `grep -r "activity\|活动台账" app/` → 0 命中 |
| 搜索请求无活动/相册/日期区间字段 | `app/models/request.py:57-...`（query/asset_ids/limit/time_weight/similarity_weight/include_metadata_fields/include_original_url/image_urls/negations/sort_by/time_hint/min_rating） |
| 复合检索只有 person_id + location + query | `app/models/request.py:145-159` |
| 搜索侧写回是 BackgroundTask 副作用 | `app/api/routes/search.py:52-78`（写场景标签 + AI 相册 + 描述） |
| 写回总开关只认 .env，无运行时覆盖 | `app/core/config.py:126` `writeback_enabled=False`；`app/core/runtime_config.py:92` 注释「写回是危险开关」 |
| 自动相册按「场景 + 月份」分组 | `app/services/album_writeback.py:85 _group_by_scene_month` |
| 写回组件已完整（建相册/加图/标签/描述/回滚） | `app/services/immich_writeback.py:188 get_or_create_album`、`:227 add_to_album`、`:123 add_tag`、`:275 update_description`、`:348 rollback`、`:374 list_albums` |
| 已有一套写回审计与回滚接口 | `app/api/routes/admin.py:86 audit`、`:112 rollback`、`:126 rollback/asset/{id}` |
| demo 搜索只有 query/top_k/scene | `demo/serve_demo.py:43-46`；`/api/search` :114、`/api/scenes` :175、`/api/item/{id}` :183 |
| demo 响应已含素材包所需字段 | `id/score/rel/thumb/w/h/takenAt/city/country/scene/sub/scene_sim`（:144-158） |
| demo 已用 numpy 全量打分，加白名单过滤只改 1 处 | `:132-138`（`scores = mat @ qv` → `argsort` → scene 过滤 → `[:top_k]`） |
| demo 数据是通用图库，不是校园素材 | `data/demo/meta.json`：487 张，takenAt 2018-02→2026-10，横竖混排（480×321 有 209 张、321×480 有 66 张）；seed 分布是 snow mountain / dog / cat / city skyline at night / hot pot，**scene_key 有 76 张为 null** |
| 页面只有搜索和看大图 | `demo/web/index.html:64-130` |
| 打包排除规则 | `tools/pack_submission.py:28-44`（.env/密钥/证书/db/大 data 文件） |

---

## 2. 优先级 1：用活动台账锁定照片范围（可做｜2 人日）

### 设计

台账不必上数据库。3 场活动约 100–300 张，用一份 JSON + 一个 `ActivityStore` 类即可，但**接口形状按表设计**，将来换 DB 不动调用方。

```jsonc
// data/activities/activities.json
{
  "version": 1,
  "activities": [
    {
      "id": "act-2026-11-xinying-wanhui",
      "slug": "xinying-wanhui-2026",
      "name": "迎新晚会",
      "aliases": ["迎新晚会", "2026 迎新", "迎新汇演"],
      "starts_at": "2026-11-08T19:00:00+08:00",
      "ends_at":   "2026-11-08T22:00:00+08:00",
      "organizer": "校团委",
      "venue": "大学生活动中心",
      "asset_ids": ["<immich-or-demo-id>", "..."],
      "cover_asset_id": "<id>",
      "shots": { "group": 12, "stage": 40, "crowd": 28 }   // 画面内可选池
    },
    { "id": "act-2026-10-zhiyuan", "name": "志愿服务", "aliases": ["志愿服务", "志愿活动"], "...": "..." },
    { "id": "act-2026-06-biye", "name": "毕业合影", "aliases": ["大合照", "毕业合影", "合影"], "...": "..." }
  ]
}
```

新路由（`app/api/routes/activities.py`，约 120 行）：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/v1/activities` | 列表（id/name/日期/数量/封面） |
| GET | `/api/v1/activities/{id}` | 详情 + 可选画面池统计 |
| POST | `/api/v1/activities/resolve` | 入参 `{query?, date?, name?}` → `matched \| ambiguous \| none` |

**resolve 三级匹配**（对应「同名多场次先问用户」）：

1. 别名精确 → 1 命中直接 matched；>1 命中（同名不同场次）→ ambiguous，返回候选数组，每项带日期/主办方/张数。
2. 子串 + `difflib.SequenceMatcher.ratio()` ≥ 0.72 → 候补。
3. 时间窗：`takenAt` 落在 `[starts_at−1d, ends_at+1d]` → 候补（用于「上周三那个晚会」这类表达）。

歧义时**不猜**：返回 ambiguous + 让编排层追问。这是本项的核心约束。

**检索落地两种（都只是小改）**：

- demo 侧：`SearchReq` 加 `activity: str | None` 与 `ids: list[str] | None`，在 `:135` 的 scene 过滤旁加一行 `idx = np.array([i for i in idx if items[i]["id"] in allow])`；`allow = set(activity.asset_ids)`。3 行。
- 完整版：`SearchRequest` 加 `activity_id`，`SearchService` 里先取台账 asset_ids 作为候选集（Immich `/api/search/metadata` 支持 `albumIds`，也可直接用台账白名单）。

### 验收

- 三场活动，每场 ≥20 张，`takenAt` 与台账一致。
- 输入「迎新晚会」→ 结果集合 ⊆ 该场 asset_ids（可断言）。
- 输入「晚会」（两场都有）→ resolve 返回 2 候选 + 日期/主办方，且不返回任何图片。
- 输入「大合照」→ 命中毕业合影场，且画面内按 `shots.group` 优先。

### 注意

demo meta 里 `scene_key` 有 76/487 为空，靠 scene 过滤不可靠；**用 ids 白名单才是稳的**，这也是为什么台账优先于 scene。

---

## 3. 优先级 2：把找图接成能完成任务的工作流（可做｜2–3 人日）

### 分工边界（关键）

- **FastAPI 只出原子接口 + 素材包状态**，不做 agent loop。
- **Coze 负责对话、追问、意图改写**；它的 HTTP 插件指向我们三个接口。

### 三个接口

```
POST /api/v1/workflow/resolve-activity
  { "utterance": "迎新晚会 推文封面" }
  → { "status": "matched|ambiguous|none", "activity": {...}, "candidates": [...], "need": "pick_one|none" }

POST /api/v1/workflow/search-in-activity
  { "activity_id": "...", "query": "领导讲话 特写", "slots": {"cover":3,"body":5}, "orientation": "any|landscape|portrait" }
  → { "session_id": "...", "revision": 1,
      "package": { "activity": {...},
                   "cover": [ {asset_id, score, rel, w, h, takenAt, thumb, reason} x3 ],
                   "body":  [ ... x5 ] },
      "pool_size": 68, "rejected": [ {"asset_id":..., "why": "score<0.22"} ] }

POST /api/v1/workflow/package/edit        # 「换成横图」「去掉这张」
  { "session_id": "...", "revision": 1,
    "ops": [ {"op":"replace","slot":"cover","orientation":"landscape"},
             {"op":"drop","asset_id":"..."} ] }
  → { "revision": 2, "package": {...}, "changed": {...} }

GET  /api/v1/workflow/package/export?session_id=...&revision=2
  → manifest.json + manifest.csv + 缩略图 zip
```

### 为什么素材包必须是「可变状态」而不是一次性响应

「换成横图」不能重跑检索——重跑会换掉整组图，用户看到的其他 7 张全变，体验直接崩。正确做法：服务端保留候选池（本活动全部资产 + 本次打分），编辑操作只在池内与包内重排。

- 候选池落盘：`data/packages/{session_id}.json`（含 pool 的 score 快照）。3 场活动量级下 JSON 足够，不用 DB。
- `orientation`：直接用 `w > h` 判横竖（demo meta 已有 w/h，实测 480×321 与 321×480 各占一批）。
- `drop` 后补位：同 slot 内按 score 降序取下一个未用项；池空则该 slot 标 `"empty": true` 并提示，不静默凑数。
- `revision` 单调递增，`edit` 必须带 revision 防并发覆盖（前端/Coze 拿到旧包就报冲突）。

### reason 怎么生成（评审最看这一行）

不要写「AI 认为相关」这种空话。用可验证的三段式：

```
"reason": "画面含舞台与人群（scene=event 0.41）；拍摄时间 2026-11-08 19:42 在活动时段内；
           480×321 横向，适合封面；同组另有 2 张更亮候选未选。"
```

字段来源：`scene/scene_sim`（demo 已有）、`takenAt` 与台账比对、`w/h`、`score` 分布。

### 导出（时间紧就先做这个）

`manifest.csv` 列：`slot, order, asset_id, fileName, score, scene, takenAt, w, h, thumb_url, reason`。
缩略图 zip 用 demo 已有的 `/thumbs/{id}.webp`（480 长边，够排版预览）；原图导出留 P2，因为要接 Immich 原图代理 + 权限。

### Coze 侧（约半天）

一个 Bot + 三个 HTTP 工具 + 一段固定 prompt：

1. 先调 resolve-activity；ambiguous → 只问「是哪一场？」，附候选行（日期 + 主办方 + 张数）。
2. matched → 调 search-in-activity，把包渲染成图文卡片。
3. 收到「换成横图 / 去掉这张 / 再来一张」→ 调 package/edit，不重搜。
4. 收到「导出」→ 给 export 链接。

### 验收

- 一句「迎新晚会 推文封面」→ ≤3 分钟内出包（封面 3 选 1 + 正文 5 + 每张理由）。
- 一句「换成横图」→ 只有封面变化，正文 5 张 asset_id 全不变（可断言）。
- 导出 zip 可打开，manifest 行数 = 包内张数。

---

## 4. 优先级 3：归档放到用户确认之后（可做｜1–1.5 人日）

### 现状判断

写回不是能力缺失，是**触发位置错了**：它挂在搜索接口的 BackgroundTask 上（`search.py:52-78`），搜索即写库。而且自动相册按「场景 + 月份」（`album_writeback.py:85`），与「按活动归档」不一致。

好消息：写回组件本身是全的（建相册、加图、标签、描述、评分、回滚、审计），**只需要换触发点和相册命名维度**。

### 改造三段式

```
POST /api/v1/archive/propose   { activity_id, asset_ids[] }
  → 200 { proposal_id, dry_run: true,
          album: {"name": "云影图鉴 · 迎新晚会 2026-11-08", "exists": false},
          will_add: 8, already_in: 2, tags: ["活动:迎新晚会"],
          warnings: ["2 张已在同名相册"], ttl_s: 600 }

POST /api/v1/archive/commit    { proposal_id }
  → 200 { album_id, album_url, added: 8, skipped: 2, audit_ids: [...] }

POST /api/v1/archive/rollback  { }            # 复用 admin.py:112 的 mgr.rollback_recent
```

实现要点：

1. `propose` 全程 dry-run：只读 Immich，算差异，不写。
2. `commit` 只接受未过期 proposal（600 s TTL），调用 `ImmichWriteBack.get_or_create_album(:188)` + `add_to_album(:227)`；标签用 `add_tag(:123)`；不进 BackgroundTask，同步返回结果，失败即报。
3. 回滚复用现有审计链（`WriteBackEntry` + `rollback(:348)`），PPT 里可以直接演示「一键撤回」。

### 只读模式：别只靠 .env 一个开关

`WRITEBACK_ENABLED=false` 已经把写回关掉（默认值就是 false），比赛版保持不动即可。但建议再加一道**硬闸**：

- `COMPETITION_READONLY=true` 时，用中间件把 `POST/PUT/DELETE /api/v1/archive/*`、`/admin/api/writeback/*` 统一 403，并回 `{"readonly": true, "hint": "示例站仅演示检索，归档需在团队账号下操作"}`。
- 加两条回归测试：① `writeback_enabled=false` 时搜索 100 次，`ImmichWriteBack` 审计条目为 0；② `writeback_enabled=true` 时搜索也不写（写回只由 `/archive/commit` 触发）——第 ② 条是给评委看的「副作用已消除」证据。

### 验收

- 搜索 100 次 → Immich 零变更（审计日志为空，作为截图证据）。
- 灌一批 → 生成 1 个活动相册，加入数与 `will_add` 一致，返回可点链接。
- 回滚 → 相册与标签回到灌库前状态。

---

## 5. 优先级 4：重做评委试用入口（可做｜1–1.5 人日）

### 现实约束（已实测）

- 公网入口已不存在：`probe-block`（listen 80 default_server）`return 444`，裸 IP 直接断连；`probe-root` 的 `ayuanjc.online` 根已让给「探针」项目（2026-10-25 备案整改）。
- 但服务还活着：`yunying-demo.service` active，`127.0.0.1:8100/api/health` = `{status:ok, count:487, dim:512}`，`/opt/yunying` 723 MB 完整。
- 现有 487 张是通用图库（狗/猫/雪山/城市夜景/火锅），**不是校园素材**，评委看不出版本相关性。

### 方案：独立样例库 + 子域入口 + 外来同学验收

1. **校园样例库**：`data/demo-campus/`（新 meta + thumbs + embeddings.npy），3 场活动每场 30–50 张，只放团队有权展示的照片（避开人脸特写或取得同意）。用现成 `tools/build_demo_assets.py` 重建 embedding（离线 CLIP visual 塔已在服务器 venv 里，不用重下模型）。
2. **入口**：`yunying.ayuanjc.online` 新 vhost（B 方案），单页三件事：找图 → 生成素材包 → 导出；`?k=<token>` 限时链接，不做登录。若想 30 分钟见效，可先给 `demo-site` 的 8090 加 `/api` 反代（A 方案），但**不要动 80 的 444 结构**。
3. **页面改造**：在现有 `demo/web/index.html` 上加两块——活动切换（三场三卡）+ 素材包侧栏（封面/正文/理由/导出）。现有页面只有搜索与大图预览（`:64-130`）。
4. **外部同学验收脚本（10 分钟，回传截图）**：
   1. 打开链接，首屏 3 秒内出 12 张图 → 截图；
   2. 搜「迎新晚会 合影」→ 结果只来自该场 → 截图；
   3. 选 3 张生成包 → 看到每张的理由 → 截图；
   4. 说「换成横图」→ 封面变横图且正文不变 → 截图；
   5. 导出 → zip 能打开、manifest 有 8 行 → 截图；
   6. 故意越权（点「归档到相册」）→ 得到 `readonly` 提示而非 500 → 截图。
5. **打包侧**：zip 里不再承诺「可本地跑 demo」，改为 README 第 1 页写试用链接 + `data-campus` 网盘/对象存储外链（若确实需要离线复现），并说明 `model.data` 406 MB 的获取方式（`pack_submission.py:139-144` 的现实约束写清楚，比被评委发现更好）。

### 验收

- 队外同学独立完成「找图 → 换图 → 导出」，无需口头指导。
- 入口 24h 可用性 ≥ 99%（`deploy_monitor.sh` 已有，加一条 /api/health 探测即可）。

---

## 6. 排期与依赖

| 天 | 工作 | 依赖 |
|---|---|---|
| D1–D2 | 活动台账（3 场标注）+ `/activities` 三接口 + demo ids 过滤 | 无 |
| D3–D4 | workflow 三接口 + 素材包状态机 + 导出 + Coze Bot | ① |
| D5 | archive propose/commit + 只读硬闸 + 两条回归测试 | ② |
| D6 | 校园样例库 + 新入口 + 队外同学验收 | ① |
| D7 | 缓冲：manifest 打磨、PPT/readme 更新、1115 测试回归 | — |

关键路径是 ①→②→③；④ 只依赖 ① 的标注结果，可与 ②③ 并行（标注完就能建库）。

---

## 7. 风险与取舍

| 风险 | 影响 | 处置 |
|---|---|---|
| 台账人工标注成本 | 3 场 × 50 张，约 2 h/场 | 用 `takenAt` 时间窗批量入选，再人工剔除；脚本一次生成候选清单 |
| CLIP 对校园专有词无能 | 语义查询召回差 | 活动结构化过滤 + 画面语义两段式，别指望扩词表救全部 |
| Coze 平台依赖 | 演示时平台抖动 | 三个工具用 curl/Postman 兜底，PPT 放录屏 |
| 写回污染图库 | 评委看到脏数据 | 只读硬闸 + 搜索零写回测试；归档只走 commit 一次动作 |
| 样例库肖像权 | 展示合规 | 只用团队已授权照片，避免特写；页面加「仅用于比赛演示」声明 |

**明确不做**：人脸/人物检索（`faces.py` 已有但比赛版无意义）、向量库迁移（1000 张以内 numpy 足够）、多用户权限体系（试用链接 token 化就够）。

---

## 8. 可立即开工的三件小事

1. `data/activities/activities.json` 骨架 + 从 `data/demo/meta.json` 按 `takenAt` 自动聚类出候选活动清单（用于 D1 的标注）。
2. `demo/serve_demo.py` 加 `ids` / `activity` 过滤参数（3 行）+ 单测。
3. `/workflow/package` 的 manifest 字段与 zip 导出脚本（先出 CSV，能交付即可）。
