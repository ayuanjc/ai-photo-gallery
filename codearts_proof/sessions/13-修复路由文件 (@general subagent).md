# 修复路由文件 (@general subagent)

- 会话 ID：ses_ecc0eb459ffe8QYi3vISIH9Lxz
- 工作目录：a01
- 消息数：13　代码改动：0 次　轮次范围：第 1-1 轮

## 第 1 轮 · 2026-10-13 14:26:54

**用户：**

```text
你在 C:\Users\ayuan\IDEProjects\a01 项目中工作。这是一个 Python FastAPI 项目。请修复以下路由文件中的问题。不要添加注释。修改完成后不需要运行测试。

## 1. 给 faces.py、ai.py、batch_import.py 添加限流（P1-6）

在 `app/api/routes/faces.py`、`app/api/routes/ai.py`、`app/api/routes/batch_import.py` 中，给 router 的 dependencies 添加 `Depends(rate_limit_guard)`。

先读取每个文件，找到 `APIRouter` 定义行，然后修改。

faces.py 当前可能是：
```python
router = APIRouter(tags=["faces"], dependencies=[Depends(verify_api_key)])
```
改为：
```python
router = APIRouter(tags=["faces"], dependencies=[Depends(verify_api_key), Depends(rate_limit_guard)])
```

同时确保 `rate_limit_guard` 已从 `..deps` 导入。如果没有，添加导入。

对 ai.py 和 batch_import.py 做同样的修改。

## 2. 给 scene.py 添加鉴权（P3-29）

在 `app/api/routes/scene.py` 中，给 router 添加 `dependencies=[Depends(verify_api_key)]`。
确保从 `..deps` 导入了 `verify_api_key`。

## 3. 修改 search.py 使用 FeedbackService 单例（P3-39）

在 `app/api/routes/search.py` 中，找到 `record_feedback` 函数里的 `service = FeedbackService(cache, get_settings())`，改为使用单例：

```python
from ...services.feedback_service import get_feedback_service
```
然后在函数内：
```python
service = get_feedback_service()
if service is None:
    service = FeedbackService(cache, get_settings())
```

保留 `FeedbackService` 的导入作为 fallback。

请逐一读取文件、修改、确认。
```

**码道：**

先确认 `..deps` 中存在 `rate_limit_guard`、`verify_api_key`，以及 `feedback_service` 中存在 `get_feedback_service`。

**码道：**

所有依赖都已存在。现在并行修改各文件。

**1. faces.py** — 添加 `rate_limit_guard` 导入，router 加限流，移除路由级重复鉴权：
