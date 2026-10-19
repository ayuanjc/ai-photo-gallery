# 修复模型和工具文件 (@general subagent)

- 会话 ID：ses_ecc0e2e83ffe2taZfLweKyb8Ny
- 工作目录：C:\Users\ayuan\IDEProjects\a01
- 消息数：7　代码改动：0 次　轮次范围：第 1-1 轮

## 第 1 轮 · 2026-10-13 14:27:28

**用户：**

```text
你在 C:\Users\ayuan\IDEProjects\a01 项目中工作。这是一个 Python FastAPI 项目。请修复以下文件中的问题。不要添加注释。修改完成后不需要运行测试。

## 1. request.py — 两个修复

### P2-15: location 无长度限制
在 `app/models/request.py` 中，找到 `MultiConditionSearchRequest` 的 `location` 字段，添加 `max_length=500`：
```python
location: str | None = Field(default=None, max_length=500, description="地点关键词")
```

### P3-32: null 字节未过滤
在 `_query_not_blank` validator 中（或 `query` 的 validator 中），添加 null 字节过滤：
```python
value = value.replace("\x00", "")
```
在 `_validate_asset_id` validator 中也添加同样的过滤。

## 2. deps.py — X-Forwarded-For 修复（P1-5）

在 `app/api/deps.py` 的 `_get_client_id` 函数中，当前直接信任 `x-forwarded-for`。修改为只在使用可信代理时才信任 XFF：

```python
def _get_client_id(request: Request) -> str:
    api_key =<REDACTED>("x-api-key")
    if api_key:<REDACTED>
        return f"key:{api_key}"
    client_host = request.client.host if request.client else "unknown"
    settings = get_settings()
    if settings.trusted_proxies and client_host in settings.trusted_proxies:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return f"ip:{forwarded.split(',')[0].strip()}"
    return f"ip:{client_host}"
```

确保 `get_settings` 已导入（应该已从 `..core.config` 导入）。

## 3. requirements.txt — 格式错误（P2-16）

读取 `requirements.txt`，检查是否有 `redis` 和 `piexif` 之间缺少换行符的情况。如果有，修复为两行。

## 4. url_safety.py — DNS Rebinding（P1-4）

在 `app/utils/url_safety.py` 中，当前对域名直接 `return True`。修改为对域名也做 DNS 解析校验：

```python
import socket

def is_safe_url(url: str) -> bool:
    # ... 现有的 IP 字面量校验逻辑 ...
    # 对域名做 DNS 解析
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except (socket.gaierror, ValueError):
        return False
```

请先读取现有代码，然后在适当位置添加 DNS 解析逻辑。注意不要破坏现有的 IP 字面量校验。

## 5. analyzer.py — 空 ID singleflight（P2-18）

在 `app/scene/analyzer.py` 中，找到 `if asset_i
```

**码道：**

已读取所有文件，现在开始逐一修改。

**码道：**

url_safety.py 的第二个修改需要重新读取文件。
