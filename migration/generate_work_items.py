"""生成飞书项目可导入的工作项 CSV。

数据源：WORK_ITEMS 列表（基于当前代码库拆解）。
可自由编辑后重跑： python migration/generate_work_items.py
"""
from __future__ import annotations

import csv
from pathlib import Path

# 列顺序即 CSV 表头，导入飞书项目时按列名映射字段。
FIELDS = ["标题", "类型", "描述", "状态", "优先级", "负责人", "标签", "模块", "里程碑"]

# 状态取值：待办 / 进行中 / 已完成（与文档中的状态流转映射一致）
WORK_ITEMS: list[dict] = [
    # -------- 已完成（历史任务，源自已交付功能） --------
    {
        "标题": "图像语义搜索服务框架搭建",
        "类型": "需求",
        "描述": "搭建 FastAPI 异步路由、依赖注入、请求/响应模型与字段校验。",
        "状态": "已完成",
        "优先级": "高",
        "负责人": "后端开发",
        "标签": "backend,framework",
        "模块": "app",
        "里程碑": "M1 首版交付",
    },
    {
        "标题": "Immich 语义搜索客户端",
        "类型": "需求",
        "描述": "实现 POST /api/search/smart 调用，含连接/读写超时、指数退避重试与错误分类。",
        "状态": "已完成",
        "优先级": "高",
        "负责人": "后端开发",
        "标签": "backend,immich",
        "模块": "app/services",
        "里程碑": "M1 首版交付",
    },
    {
        "标题": "时间+相似度加权二次排序",
        "类型": "需求",
        "描述": "EXIF 拍摄时间与语义相似度加权融合，时间来源回退链（EXIF>文件创建>默认）与指数衰减。",
        "状态": "已完成",
        "优先级": "高",
        "负责人": "后端开发",
        "标签": "backend,ranking",
        "模块": "app/services",
        "里程碑": "M1 首版交付",
    },
    {
        "标题": "视觉识别抽象层（华为云预留）",
        "类型": "需求",
        "描述": "抽象基类 VisionProvider + 依赖注入，华为云适配器占位，视觉失败降级不阻塞主流程。",
        "状态": "已完成",
        "优先级": "中",
        "负责人": "后端开发",
        "标签": "backend,extension,vision",
        "模块": "app/services/vision",
        "里程碑": "M1 首版交付",
    },
    {
        "标题": "统一错误码与异常处理",
        "类型": "需求",
        "描述": "错误码枚举、领域异常体系、全局异常处理器，映射 400/502/504/500。",
        "状态": "已完成",
        "优先级": "高",
        "负责人": "后端开发",
        "标签": "backend,errors",
        "模块": "app/core",
        "里程碑": "M1 首版交付",
    },
    {
        "标题": "自动化测试（单元+集成）",
        "类型": "需求",
        "描述": "36 用例覆盖请求校验、时间解析、排序、客户端重试/错误分类、降级与 API 集成。",
        "状态": "已完成",
        "优先级": "高",
        "负责人": "测试工程师",
        "标签": "test,qa",
        "模块": "tests",
        "里程碑": "M1 首版交付",
    },
    {
        "标题": "容器化与部署配置",
        "类型": "需求",
        "描述": "Dockerfile（非 root）、docker-compose（健康检查）、环境配置示例与部署文档。",
        "状态": "已完成",
        "优先级": "中",
        "负责人": "后端开发",
        "标签": "devops,deployment",
        "模块": "root",
        "里程碑": "M1 首版交付",
    },
    # -------- 待办（Backlog，源自预留扩展点） --------
    {
        "标题": "补全华为云图像识别真实接入",
        "类型": "任务",
        "描述": "实现 IAM 鉴权 + 图像标签 API，替换 HuaweiVisionProvider 占位实现。",
        "状态": "待办",
        "优先级": "高",
        "负责人": "后端开发",
        "标签": "backend,huawei,extension",
        "模块": "app/services/vision",
        "里程碑": "M2 增强",
    },
    {
        "标题": "排序策略可配置化",
        "类型": "任务",
        "描述": "引入 RRF/Borda 排名融合 vs 连续加权的策略模式，应对 Immich score 缺失冲突。",
        "状态": "待办",
        "优先级": "中",
        "负责人": "后端开发",
        "标签": "backend,ranking,strategy",
        "模块": "app/services",
        "里程碑": "M2 增强",
    },
    {
        "标题": "Immich 契约测试",
        "类型": "任务",
        "描述": "针对 score 缺失、多版本响应结构（assets.items/list）、withExif 内联做契约样本测试。",
        "状态": "待办",
        "优先级": "高",
        "负责人": "测试工程师",
        "标签": "test,immich,contract",
        "模块": "tests",
        "里程碑": "M2 增强",
    },
    {
        "标题": "Redis 结果缓存与接口限流",
        "类型": "任务",
        "描述": "Redis 缓存搜索热点结果，接口级限流降低 Immich 压力。",
        "状态": "待办",
        "优先级": "中",
        "负责人": "后端开发",
        "标签": "backend,cache,ratelimit",
        "模块": "app/services",
        "里程碑": "M3 优化",
    },
    {
        "标题": "Prometheus 指标与链路追踪",
        "类型": "任务",
        "描述": "暴露 /metrics 与链路追踪，覆盖搜索延迟、上游错误、降级触发等指标。",
        "状态": "待办",
        "优先级": "低",
        "负责人": "后端开发",
        "标签": "backend,observability",
        "模块": "app/core",
        "里程碑": "M3 优化",
    },
    {
        "标题": "消费端 SDK 与调用示例",
        "类型": "任务",
        "描述": "提供请求/响应示例、错误码处理指南与客户端调用封装。",
        "状态": "待办",
        "优先级": "中",
        "负责人": "前端开发",
        "标签": "frontend,sdk,docs",
        "模块": "docs",
        "里程碑": "M3 优化",
    },
    {
        "标题": "真实环境集成测试",
        "类型": "任务",
        "描述": "对接真实 Immich 环境做端到端集成测试，覆盖语义搜索与二次排序正确性。",
        "状态": "待办",
        "优先级": "中",
        "负责人": "测试工程师",
        "标签": "test,integration",
        "模块": "tests",
        "里程碑": "M3 优化",
    },
    {
        "标题": "CI/CD 流水线",
        "类型": "任务",
        "描述": "接入 lint + pytest + docker build 的持续集成与发布流程。",
        "状态": "待办",
        "优先级": "中",
        "负责人": "后端开发",
        "标签": "ci,devops",
        "模块": "root",
        "里程碑": "M3 优化",
    },
]


def main(out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        # utf-8-sig 让 Excel 正确识别 UTF-8 中文
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for item in WORK_ITEMS:
            writer.writerow(item)
    print(f"已生成 {len(WORK_ITEMS)} 条工作项 -> {out}")


if __name__ == "__main__":
    main(Path(__file__).resolve().parent / "work_items.csv")