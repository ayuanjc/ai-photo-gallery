# 修复服务层文件 (@general subagent)

- 会话 ID：ses_ecc0e70b3ffe1UniZVUewFbirT
- 工作目录：a01
- 消息数：30　代码改动：0 次　轮次范围：第 1-1 轮

## 第 1 轮 · 2026-10-13 14:27:11

**用户：**

```text
你在 C:\Users\ayuan\IDEProjects\a01 项目中工作。这是一个 Python FastAPI 项目。请修复以下服务层文件中的问题。不要添加注释。修改完成后不需要运行测试。

## 1. search_service.py — final_score clamp（P1-13）

在 `app/services/search_service.py` 中，找到 `final_score=round(final, 6)` 这行，改为 `final_score=round(clamp(final), 6)`。确保 `clamp` 已从 `ranking` 导入（大概率已导入）。

## 2. multi_condition_service.py — 三个修复

### P1-11: 无 query 时未 limit
找到 `_build_results_from_candidates` 方法，给它添加 `limit: int` 参数。在方法最后 `return results` 前添加 `return results[:limit]`（或修改已有的 return）。同时找到调用 `_build_results_from_candidates` 的地方，传入 `limit=request.limit`。

### P2-28: 权重未归一化
找到计算 `multi_score` 的地方（约 209 行），在计算前对权重归一化：
```python
w_total = w_person + w_location + w_query
if w_total > 0:
    w_person /= w_total
    w_location /= w_total
    w_query /= w_total
```

### P3-36: 降级条件顺序硬编码
找到 `degraded_conditions` 追加的地方（约 241 行），改为按权重动态排序：
```python
degraded_conditions = [
    name for name, _ in sorted(
        [("query", w_query), ("location", w_location), ("person", w_person)],
        key=lambda x: x[1],
    )
]
```

## 3. dedup.py — 冗余条件（P2-22）

在 `app/services/dedup.py` 中，找到 `if time_window_sec > 0 or time_window_sec == 0:` 改为 `if time_window_sec > 0:`。

## 4. importer.py — 两个修复

### P2-21: 文件数量上限
在 `app/services/importer.py` 的 `import_files` 方法开头，检查 `len(request.file_paths)` 是否超过 `self._settings.import_max_files`，超过则抛 `ValueError`。

### P2-26: warnings 未赋值
在 `import_files` 方法中，收集非致命警告（如单个文件 EXIF 解析失败但仍上传成功），汇总到 `ImportResponse.warnings`。在构建 `ImportResponse` 时传入 `warnings=collected_warnings`。

## 5. feedback_service.py — 并发竞态（P2-27）

在 `app/services/feedback_service.py` 的 `record_feedback` 方法中，当前是 read-then-write 模式。改为使用 Redis INCR 原子操作（如果 cache 后端支持），或在写入时使用 `cache.set(key, str(int(old or 0) + 1), ttl=...)` 的方式但添加注释说明非原子。

实际上最简单的修复是：在方法文档中说明此操作在并发下可能丢失少量计数，并在代码中使用 `try/except` 包裹整个 read-modify-write。如果已经是 try/except，则保持现状但确保不会因并发导致崩溃。

请逐一读取文件、修改、确认。
```

**码道：**

逐一修改。

**1. search_service.py — final_score clamp**

**码道：**

**2. multi_condition_service.py — P1-11: `_build_results_from_candidates` 添加 limit 参数**
