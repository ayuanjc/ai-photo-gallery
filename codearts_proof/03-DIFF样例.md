# 03-DIFF样例 · 最能体现码道改代码的 8 个文件

> 每条样本取自码道 edit / write 工具的实际入参（oldString/newString，即它提交给文件系统的内容），不是事后编造。
> cag_file_diff.extra_info 仅含动作类型（Created/Modified/Deleted），无 diff 文本，故具体片段以工具入参为准。
> 改动次数/增删行/首末时间来自 cag_file_diff SQL 聚合（经 user_message_id 关联消息时间）。完整改动可在码道会话中按会话 ID 复核。

## `app/main.py`

- 改动次数：**116**　新增 378 行　删除 120 行
- 首次改动：2026-10-11 21:09:30　末次改动：2026-10-19 14:58:41
- edit/write 工具调用命中：95 次

### 改动 1 · write · 2026-10-11 21:16:53

```text
"""FastAPI 应用入口。

包含：
- lifespan：创建并管理 Immich 客户端、视觉识别 provider、搜索服务的生命周期。
- 请求 ID 中间件：透传/生成 x-request-id，便于日志与监控关联。
- 全局异常处理器：将领域异常与校验异常映射为统一错误结构。
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
```

### 改动 2 · edit · 2026-10-11 21:21:26

```diff
- @app.exception_handler(RequestValidationError)
- async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
-     return JSONResponse(
-         status_code=400,
-         content={
-             "error": {
-                 "code": ErrorCode.INVALID_PARAMS.value,
-                 "message": "请求参数校验失败",
+ @app.exception_handler(RequestValidationError)
+ async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
+     # 仅保留可序列化的核心字段，避免原始 ValueError/输入对象导致的 JSON 序列化失败
+     details = [
+         {
+             "loc": [str(p) for p in err.get("loc", [])],
+             "msg": str(err.get("msg", "")),
+             "type": err.get("type", ""),
```

### 改动 3 · write · 2026-10-11 23:30:58

```text
"""FastAPI 应用入口。

包含：
- lifespan：创建并管理 Immich 客户端、视觉识别 provider、搜索服务的生命周期。
- 请求 ID 中间件：透传/生成 x-request-id，便于日志与监控关联。
- 全局异常处理器：将领域异常与校验异常映射为统一错误结构。
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
```

## `app/core/config.py`

- 改动次数：**73**　新增 198 行　删除 48 行
- 首次改动：2026-10-11 21:09:30　末次改动：2026-10-19 14:58:41
- edit/write 工具调用命中：44 次

### 改动 1 · write · 2026-10-11 21:11:25

```text
"""应用配置管理。

使用 pydantic-settings 从环境变量 / .env 文件加载配置，
并通过 lru_cache 提供全局单例，供依赖注入使用。
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

```

### 改动 2 · edit · 2026-10-12 02:12:07

```diff
-     # ---- 搜索结果 ----
-     default_result_limit: int = 50
-     max_result_limit: int = 200
- 
-     # ---- 排序权重（二次排序：时间 α + 相似度 β）----
+     # ---- 搜索结果 ----
+     search_max_concurrent_vision: int = 5
+     search_max_concurrent_scene: int = 10
+ 
+     # ---- 排序权重（二次排序：时间 α + 相似度 β）----
```

### 改动 3 · edit · 2026-10-12 02:12:23

```diff
-     # ---- AI 分析（同一个人 vs 相似图片）----
-     ai_analyzer_provider: str = "immich"
-     ai_person_confidence_threshold: float = 0.6
-     ai_similarity_threshold: float = 0.75
-     ai_analyze_timeout_seconds: float = 5.0
-     ai_analyzer_max_retries: int = 2
+     # ---- AI 分析（同一个人 vs 相似图片）----
+     ai_analyzer_provider: str = "immich"
+     ai_person_confidence_threshold: float = 0.6
+     ai_similarity_threshold: float = 0.75
+     ai_analyze_timeout_seconds: float = 5.0
+     ai_analyzer_max_retries: int = 2
+     ai_max_concurrent: int = 10
```

## `app/services/search_service.py`

- 改动次数：**67**　新增 305 行　删除 92 行
- 首次改动：2026-10-11 21:09:30　末次改动：2026-10-19 14:58:41
- edit/write 工具调用命中：52 次

### 改动 1 · write · 2026-10-11 21:15:26

```text
"""搜索编排服务：串起视觉增强、Immich 语义搜索与二次排序。"""
from __future__ import annotations

import asyncio
import logging

from ..core.config import Settings
from ..models.request import SearchRequest
from ..models.response import SearchResultItem, SearchResponse
from .immich import ImmichClient
from .ranking import clamp, rank_assets
from .vision.base import VisionProvider
```

### 改动 2 · edit · 2026-10-11 21:16:03

```diff
- results = [
-             self._to_item(
-                 asset,
-                 final=final,
-                 similarity=similarity,
-                 captured_at=captured_at,
-                 source=source,
-                 fields=request.include_metadata_fields,
+ results = [
+             self._to_item(
+                 asset,
+                 final=final,
+                 similarity=similarity,
+                 freshness=freshness,
+                 captured_at=captured_at,
+                 source=source,
```

### 改动 3 · edit · 2026-10-11 21:16:03

```diff
- *,
-         final: float,
-         similarity: float,
-         captured_at,
-         source: str,
-         fields: list[str],
-         include_original: bool,
-     ) -> SearchResultItem:
+ *,
+         final: float,
+         similarity: float,
+         freshness: float,
+         captured_at,
+         source: str,
+         fields: list[str],
+         include_original: bool,
```

## `app/api/routes/admin.py`

- 改动次数：**54**　新增 594 行　删除 25 行
- 首次改动：2026-10-13 16:37:37　末次改动：2026-10-19 14:58:41
- edit/write 工具调用命中：35 次

### 改动 1 · write · 2026-10-13 16:42:42

```text
"""管理接口：项目功能开关及管理。

- GET  /admin/api/features  所有功能开关（含元数据）
- PUT  /admin/api/features  批量修改开关
- GET  /admin/api/status    系统状态（版本/Immich 连通性/缓存后端/运行时长）
- POST /admin/api/verify    校验管理员密钥（管理页面登录）

鉴权：未配置 admin_api_key 且 APP_ENV=development 时放行；否则请求头需携带
x-admin-key（hmac.compare_digest 比较）。
"""
from __future__ import annotations

```

### 改动 2 · edit · 2026-10-13 16:42:58

```diff
- import hmac
- import hmac as _hmac
- import time
+ import hmac
+ import time
```

### 改动 3 · edit · 2026-10-13 16:43:05

```diff
-     valid = _hmac.compare_digest(body.admin_key, expected)
+     valid = hmac.compare_digest(body.admin_key, expected)
```

## `app/services/immich_writeback.py`

- 改动次数：**25**　新增 535 行　删除 7 行
- 首次改动：2026-10-15 22:01:38　末次改动：2026-10-19 14:58:41
- edit/write 工具调用命中：18 次

### 改动 1 · write · 2026-10-15 22:24:02

```text
"""Immich 统一写回客户端：幂等 + 可撤销 + 审计。

所有对 Immich 的写操作（标签/相册/描述）都经过此客户端，保证：
- 幂等：重复调用同一操作不会产生副作用
- 可撤销：审计日志记录每次操作，rollback() 可逆向回滚
- 非阻塞：失败返回 error 条目，不抛异常
- 可开关：通过 runtime_config 的 writeback_enabled 控制
- dry_run：仅审计不实际写入

审计日志格式（JSONL，每行一条）：
    {"action": "add_tag", "asset_id": "xxx", "target": "AI/自然", ...}
"""
```

### 改动 2 · edit · 2026-10-16 17:04:10

```diff
- class WriteBackEntry(BaseModel):
-     action: str = Field(description="add_tag|remove_tag|add_to_album|remove_from_album|create_album")
-     asset_id: str | None = None
-     target: str = Field(description="标签名或相册 ID/名称")
-     target_id: str | None = None
-     timestamp: str = ""
-     success: bool = False
-     error: str | None = None
+ class WriteBackEntry(BaseModel):
+     action: str = Field(description="add_tag|remove_tag|add_to_album|remove_from_album|create_album|update_description|update_rating")
+     asset_id: str | None = None
+     target: str = Field(description="标签名或相册 ID/名称")
+     target_id: str | None = None
+     timestamp: str = ""
+     success: bool = False
+     error: str | None = None
```

### 改动 3 · edit · 2026-10-16 17:04:36

```diff
-     async def remove_from_album(
-         self, asset_id: str, album_id: str
-     ) -> WriteBackEntry:
-         entry = WriteBackEntry(
-             action="remove_from_album", asset_id=asset_id, target=album_id,
-             timestamp=self._now(),
-         )
-         try:
+     async def remove_from_album(
+         self, asset_id: str, album_id: str
+     ) -> WriteBackEntry:
+         entry = WriteBackEntry(
+             action="remove_from_album", asset_id=asset_id, target=album_id,
+             timestamp=self._now(),
+         )
+         try:
```

## `app/services/ranking.py`

- 改动次数：**35**　新增 243 行　删除 121 行
- 首次改动：2026-10-11 21:09:30　末次改动：2026-10-19 14:58:41
- edit/write 工具调用命中：23 次

### 改动 1 · write · 2026-10-11 21:15:02

```text
"""二次排序算法：EXIF 拍摄时间与语义相似度的加权融合。

融合公式（权重已归一化）：
    final_score = α * time_freshness + β * similarity_score

- similarity_score 归一化到 [0, 1]（Immich 返回分数若超出该区间会被裁切）。
- time_freshness 由指数衰减得到，见 utils/time_parsing.py。
- 拍摄时间来源优先级：EXIF dateTimeOriginal > fileCreatedAt > 默认时间。
"""
from __future__ import annotations

import logging
```

### 改动 2 · edit · 2026-10-11 21:16:03

```diff
- default_iso: str,
-     now: datetime | None = None,
- ) -> list[tuple[dict, float, float, datetime | None, str]]:
-     """对 Immich 返回的资产列表做加权融合排序。
- 
-     返回按 final_score 降序排列的元组列表：
-         (asset, final_score, similarity_score, captured_at, captured_at_source)
-     """
+ default_iso: str,
+     now: datetime | None = None,
+ ) -> list[tuple[dict, float, float, float, datetime | None, str]]:
+     """对 Immich 返回的资产列表做加权融合排序。
+ 
+     返回按 final_score 降序排列的元组列表：
+         (asset, final_score, similarity_score, time_freshness, captured_at, captured_at_source)
+     """
```

### 改动 3 · edit · 2026-10-12 01:32:43

```diff
- from ..models.response import SearchResultItem
- from ..utils.time_parsing import DEFAULT_SOURCE, parse_datetime, time_freshness
+ from ..utils.time_parsing import DEFAULT_SOURCE, parse_datetime, time_freshness
```

## `app/api/routes/plugin.py`

- 改动次数：**38**　新增 420 行　删除 108 行
- 首次改动：2026-10-16 16:19:48　末次改动：2026-10-19 14:58:41
- edit/write 工具调用命中：26 次

### 改动 1 · write · 2026-10-16 16:22:51

```text
"""Immich 插件/工作流 hook 端点。

Immich 工作流通过 webhook 方法回调此端点，
触发场景分析 + 标签写回 + 相册写回。

请求格式（Immich webhook 发送）：
    POST /api/v1/plugin/hook
    Headers: x-plugin-token:<REDACTED><token>（可选鉴权）
    Body: {
        "event": "AssetCreate" | "AssetMetadataExtraction" | "AssetTagged",
        "asset": { "id": "...", "originalFileName": "...", ... }
    }
```

### 改动 2 · edit · 2026-10-16 16:27:09

```diff
-     if not writeback_enabled:
-         return PluginHookResponse(
-             warnings=["写回未启用"],
-             changes={},
-         )
- 
-     scene_tags = payload.asset.get("scene")
-     results_data = [{"asset_id": asset_id, "scene": scene_tags}]
+     elif payload.event == "AssetTagged":
+         tags = payload.asset.get("tags", [])
+         changes["current_tags"] = tags
+         logger.info("AssetTagged 事件: asset=%s tags=%s", asset_id, tags)
+ 
+     if not writeback_enabled:
+         return PluginHookResponse(
+             warnings=["写回未启用"] if not warnings else warnings + ["写回未启用"],
```

### 改动 3 · edit · 2026-10-16 16:27:26

```diff
-     if payload.event in ("AssetCreate", "AssetMetadataExtraction"):
-         if scene_writeback is not None:
+     if payload.event in ("AssetCreate", "AssetMetadataExtraction"):
+         scene_tags = payload.asset.get("scene")
+         results_data = [{"asset_id": asset_id, "scene": scene_tags}]
+ 
+         if scene_writeback is not None:
```

## `app/static/admin.html`

- 改动次数：**24**　新增 1096 行　删除 109 行
- 首次改动：2026-10-13 16:37:37　末次改动：2026-10-19 14:58:41
- edit/write 工具调用命中：21 次

### 改动 1 · write · 2026-10-13 16:49:01

```text
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI 图库助手 · 功能开关及管理</title>
  <style>
    :root {
      --bg: #0b0c0f;
      --card: #16181d;
      --card-hover: #1c1f26;
      --border: #2a2e37;
```

### 改动 2 · edit · 2026-10-13 16:49:39

```diff
-   async function toggleFeature(btn) {
-     var key = btn.dataset.key;
-     var enable = !btn.classList.contains("on");
-     btn.disabled = true;
-     try {
-       var data = await api("/admin/api/features", { method: "PUT", body: { updates: (o = {}, o[key] = enable, o) } });
-       var o;
-       renderFeatures(data.features);
+   async function toggleFeature(btn) {
+     var key = btn.dataset.key;
+     var enable = !btn.classList.contains("on");
+     var updates = {};
+     updates[key] = enable;
+     btn.disabled = true;
+     try {
+       var data = await api("/admin/api/features", { method: "PUT", body: { updates: updates } });
```

### 改动 3 · write · 2026-10-13 19:08:54

```text
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI 图库助手 · 管理控制台</title>
  <style>
    :root {
      --bg: #0a0b0f;
      --bg-grad: radial-gradient(ellipse at top, #12141c 0%, #0a0b0f 60%);
      --card: #14161c;
      --card-2: #1a1d26;
```

---

注：任务书原定第 7 个文件为 `app/plugins/` 下任意文件；本项目不存在该目录，插件功能实现在 `app/api/routes/plugin.py`，故以真实路径替代。