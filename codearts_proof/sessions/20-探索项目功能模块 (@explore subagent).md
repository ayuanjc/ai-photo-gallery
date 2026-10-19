# 探索项目功能模块 (@explore subagent)

- 会话 ID：ses_ecb4f925cffeQN1kZWbnFBQfU7
- 工作目录：C:\Users\ayuan\IDEProjects\a01
- 消息数：8　代码改动：0 次　轮次范围：第 1-1 轮

## 第 1 轮 · 2026-10-13 17:55:40

**用户：**

```text
请非常彻底地探索 C:\Users\ayuan\IDEProjects\a01 项目，列出所有功能模块及其实现方式。重点关注：

1. app/services/ 目录下所有服务类及其核心方法
2. app/api/routes/ 目录下所有 API 路由
3. app/services/ranking.py 中的排序算法
4. app/services/search_service.py 中的搜索编排逻辑
5. app/services/face_service.py 中的人脸分类逻辑
6. app/services/multi_condition_service.py 中的复合条件搜索
7. app/services/vision/ 目录下的视觉识别抽象层
8. app/scene/ 目录下的场景分析
9. app/core/ 目录下的配置、异常、缓存等基础设施
10. app/utils/cache.py 中的缓存实现
11. app/services/stats_provider.py 统计功能
12. app/services/immich_proxy.py 反代功能
13. app/core/runtime_config.py 运行时开关

对每个模块，请说明：
- 文件路径
- 核心类/函数名
- 主要功能描述
- 是否调用了 Immich API（如果调用了，说明调用了哪些端点）
- 是否有自研算法或逻辑

请返回完整的功能清单。
```

**码道：**

我将系统地探索这个项目。首先并行获取项目整体结构和关键目录内容。

**码道：**

项目结构已清晰。现在并行读取核心基础设施文件和主入口。
