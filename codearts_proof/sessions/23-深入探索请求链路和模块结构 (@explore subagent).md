# 深入探索请求链路和模块结构 (@explore subagent)

- 会话 ID：ses_ec0548745ffe8uTAgLZ495B5F3
- 工作目录：C:\Users\ayuan\IDEProjects\a01
- 消息数：9　代码改动：0 次　轮次范围：第 1-1 轮

## 第 1 轮 · 2026-10-15 21:06:04

**用户：**

```text
请非常彻底地（very thorough）探索 C:\Users\ayuan\IDEProjects\a01 项目的完整请求链路。我需要以下信息：

1. **app/main.py** 完整内容：lifespan 装配顺序、中间件注册顺序、异常处理器、路由挂载、静态文件/admin 挂载点。标出行号。

2. **app/api/deps.py** 完整内容：依赖注入函数（鉴权、服务获取），标出行号。

3. **app/api/routes/** 每个路由文件的完整内容：每个端点的装饰器（路径、方法）、鉴权方式、限流装饰器、请求/响应模型、调用的服务方法。标出行号。

4. **app/core/config.py** 所有配置项及其默认值。

5. **app/core/runtime_config.py** 完整内容：FEATURES_META 定义、runtime_enabled() 函数、运行时开关读写逻辑。标出行号。

6. **app/core/rate_limit.py** 完整内容：限流器实现、rate_limit_guard 装饰器。标出行号。

7. **app/core/cache.py** 和 **app/core/redis_client.py**：缓存层实现，Redis 连接与降级逻辑。标出行号。

8. **app/core/errors.py** 完整内容：ErrorCode 枚举、STATUS_CODE_MAP、AppError 基类。标出行号。

9. **app/services/search_service.py** 关键方法：search() 的完整流程（缓存检查→视觉增强→场景标注→Immich 调用→排序→去重→可解释性→缓存写入），标出行号。

10. **app/services/immich.py** 关键方法：smart_search()、_request_with_retry()、缩略图 URL 生成。标出行号。

11. **app/services/ranking.py** 关键方法：排序算法实现。标出行号。

12. **app/scene/analyzer.py** 关键方法：场景分析流程。标出行号。

13. **app/services/vision/huawei.py** 和 **app/services/vision/base.py**：视觉识别接口与实现。标出行号。

14. **app/services/face_service.py** 关键方法：人脸分类流程。标出行号。

15. **app/services/suggest_service.py** 完整内容：搜索建议实现。标出行号。

16. **app/services/feedback_service.py** 完整内容：反馈闭环实现。标出行号。

17. **app/services/stats_provider.py** 关键方法：统计取数流程。标出行号。

18. **app/services/explain.py** 完整内容：排序解释实现。标出行号。

19. **app/services/dedup.py** 完整内容：去重实现。标出行号。

20. **app/services/image_query_service.py** 关键方法：以图搜图流程。标出行号。

21. **app/services/multi_condition_service.py** 关键方法：复合条件搜索流程。标出行号。

22. **app/services/importer.py** 关键方法：批量导入流程。标出行号。

23. **app/services/watermark_engine.py** 和 **app/services/watermark_processor.py** 关键方法。标出行号。

24. **app/models/request.py** 和 **app/models/response.py**：请求/响应模型定义。标出行号。

25. **app/core/security.py** 或鉴权相关代码：API Key 校验实现。标出行号。

请返回每个文件的关键代码片段及其行号，特别关注：
- 每个中间件的注册顺序和处理逻辑
- 每个路由的鉴权和限流方式
- 每个服务的降级分支（try/except、缓存兜底、默认值）
- Redis 不可用时的降级路径
- 无 API Key 时的行为
- 上游超时/不可用时的行为
```

**码道：**

我将系统地探索整个项目。首先查看项目结构，然后并行读取所有相关文件。

**码道：**

继续并行读取核心文件：
