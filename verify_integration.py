#!/usr/bin/env python
"""端到端验收脚本（9 项核心链路）。

用法：
    python verify_integration.py [--base-url http://127.0.0.1:8000] [--admin-key KEY]

验收项：
    1. 健康检查
    2. 能力探测端点
    3. 搜索接口（含 rank_score）
    4. 图片代理端点
    5. 增量同步端点
    6. 写回审计日志端点
    7. 功能开关列表
    8. 工作流列表（Immich 原生工作流）
    9. 插件 hook 端点
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = "✅" if condition else "❌"
    print(f"  {status} {name}" + (f" — {detail}" if detail else ""))
    return condition


async def run_checks(base_url: str, admin_key: str) -> bool:
    all_pass = True
    headers = {"x-admin-key": admin_key} if admin_key else {}

    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        print("\n=== 1. 健康检查 ===")
        try:
            resp = await client.get("/health")
            all_pass &= check("GET /health", resp.status_code == 200, f"status={resp.status_code}")
        except Exception as exc:
            all_pass &= check("GET /health", False, str(exc))

        print("\n=== 2. 能力探测 ===")
        try:
            resp = await client.get("/admin/api/capabilities", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                cap = data.get("capabilities", {})
                all_pass &= check("GET /admin/api/capabilities", True)
                all_pass &= check("  version", cap.get("version") is not None, f"version={cap.get('version')}")
                all_pass &= check("  supports_plugins", cap.get("supports_plugins") is not None)
                all_pass &= check("  supports_workflows", cap.get("supports_workflows") is not None)
            else:
                all_pass &= check("GET /admin/api/capabilities", False, f"status={resp.status_code}")
        except Exception as exc:
            all_pass &= check("GET /admin/api/capabilities", False, str(exc))

        print("\n=== 3. 搜索接口 ===")
        try:
            resp = await client.post(
                "/api/v1/search/smart",
                json={"query": "测试", "limit": 5},
            )
            if resp.status_code == 200:
                data = resp.json()
                all_pass &= check("POST /api/v1/search/smart", True, f"total={data.get('total')}")
                if data.get("results"):
                    first = data["results"][0]
                    if first.get("explanation"):
                        has_rank = "rank_score" in first["explanation"]
                        all_pass &= check("  rank_score 字段", has_rank)
            else:
                all_pass &= check("POST /api/v1/search/smart", False, f"status={resp.status_code}")
        except Exception as exc:
            all_pass &= check("POST /api/v1/search/smart", False, str(exc))

        print("\n=== 4. 图片代理 ===")
        try:
            resp = await client.get("/api/v1/assets/test-id/thumbnail?size=preview")
            all_pass &= check(
                "GET /api/v1/assets/{id}/thumbnail",
                resp.status_code in (200, 400, 404),
                f"status={resp.status_code}",
            )
        except Exception as exc:
            all_pass &= check("GET /api/v1/assets/{id}/thumbnail", False, str(exc))

        print("\n=== 5. 增量同步 ===")
        try:
            resp = await client.post("/admin/api/sync", headers=headers)
            all_pass &= check(
                "POST /admin/api/sync",
                resp.status_code == 200,
                f"status={resp.status_code}",
            )
        except Exception as exc:
            all_pass &= check("POST /admin/api/sync", False, str(exc))

        print("\n=== 6. 写回审计 ===")
        try:
            resp = await client.get("/admin/api/writeback/audit?limit=5", headers=headers)
            all_pass &= check(
                "GET /admin/api/writeback/audit",
                resp.status_code == 200,
                f"status={resp.status_code}",
            )
        except Exception as exc:
            all_pass &= check("GET /admin/api/writeback/audit", False, str(exc))

        print("\n=== 7. 功能开关 ===")
        try:
            resp = await client.get("/admin/api/features", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                features = data.get("features", {})
                all_pass &= check("GET /admin/api/features", True, f"{len(features)} 个开关")
                has_writeback = "writeback_enabled" in features
                all_pass &= check("  writeback_enabled 开关", has_writeback)
            else:
                all_pass &= check("GET /admin/api/features", False, f"status={resp.status_code}")
        except Exception as exc:
            all_pass &= check("GET /admin/api/features", False, str(exc))

        print("\n=== 8. 工作流（Immich 原生） ===")
        try:
            resp = await client.get("/admin/api/workflows", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                count = data.get("count", 0)
                workflows = data.get("workflows", [])
                all_pass &= check("GET /admin/api/workflows", True, f"count={count}")
                yunying_wfs = [
                    w for w in workflows
                    if isinstance(w, dict) and "yunying" in (w.get("name") or "").lower()
                ]
                all_pass &= check(
                    "  云影默认工作流已创建",
                    len(yunying_wfs) >= 3,
                    f"找到 {len(yunying_wfs)} 个",
                )
            else:
                all_pass &= check("GET /admin/api/workflows", False, f"status={resp.status_code}")
        except Exception as exc:
            all_pass &= check("GET /admin/api/workflows", False, str(exc))

        print("\n=== 9. 插件 hook 端点 ===")
        try:
            resp = await client.post(
                "/api/v1/plugin/hook",
                json={"event": "asset.upload", "assetId": "test-id"},
            )
            all_pass &= check(
                "POST /api/v1/plugin/hook",
                resp.status_code in (200, 202, 400, 401, 404),
                f"status={resp.status_code}",
            )
        except Exception as exc:
            all_pass &= check("POST /api/v1/plugin/hook", False, str(exc))

    return all_pass


def main() -> int:
    parser = argparse.ArgumentParser(description="端到端验收（9 项核心链路）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--admin-key", default="")
    args = parser.parse_args()

    print(f"验收目标: {args.base_url}")

    import asyncio
    passed = asyncio.run(run_checks(args.base_url, args.admin_key))

    print(f"\n{'='*40}")
    print(f"结果: {'全部通过 ✅' if passed else '存在失败 ❌'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())