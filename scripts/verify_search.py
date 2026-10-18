#!/usr/bin/env python3
"""搜索链路验收脚本：20 条中文查询端到端验证（T4 翻译 + T1/T2/T3 排序去重）。

用法：
    python scripts/verify_search.py --base-url http://127.0.0.1:8000

检查项（每条查询）：
    1. translated_query 非空（中文成功语义化为英文短语，且不是逗号拼接泛化 prompt）
    2. total > 0（Immich smart search 返回结果）
    3. 无「未能语义化」warning
    4. similarity_score 不为伪造值（Immich v3.3.1 无 score 时应为 None，由前端展示 immich_rank）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

# 20 条中文验收查询（含用户实测反馈的全部 8 条 + 高频日常查询）。
QUERIES: list[str] = [
    # 用户实测 8 条
    "女孩微笑",
    "火锅",
    "故宫",
    "女朋友",
    "蓝色的车",
    "书本",
    "电脑桌",
    "海边日落",
    # 高频日常补充 12 条
    "猫咪",
    "夜景城市",
    "美食",
    "雪景",
    "婚礼现场",
    "森林徒步",
    "生日蛋糕",
    "日出",
    "沙滩",
    "自行车",
    "星空",
    "萌宠狗狗",
]


def run_checks(base_url: str) -> tuple[list[dict], list[dict]]:
    results: list[dict] = []
    rows: list[dict] = []
    print("=== 搜索链路验收（20 条中文查询） ===\n")
    print(f"{'查询':<8} {'翻译结果':<28} {'结果数':>4}  状态")
    print("-" * 64)

    with httpx.Client(timeout=30.0) as client:
        for q in QUERIES:
            try:
                r = client.post(
                    f"{base_url}/api/v1/search/smart",
                    json={"query": q, "limit": 10},
                )
                if r.status_code != 200:
                    print(f"{q:<8} HTTP {r.status_code}")
                    results.append({"query": q, "ok": False, "reason": f"HTTP {r.status_code}"})
                    continue
                d = r.json()
                translated = d.get("translated_query") or ""
                total = d.get("total", 0)
                warnings = d.get("warnings") or []
                results_list = d.get("results") or []

                problems: list[str] = []
                if not translated:
                    problems.append("未翻译")
                if "," in translated:
                    problems.append("逗号拼接(泛化prompt回归)")
                if total == 0:
                    problems.append("无结果")
                if any("未能语义化" in w for w in warnings):
                    problems.append("语义化warning")
                # 首条结果分数应来自上游或为 None，而非编造
                if results_list:
                    top = results_list[0]
                    score = top.get("similarity_score")
                    if score is not None and top.get("similarity_source") not in (
                        "upstream", None
                    ):
                        problems.append(f"可疑分数来源:{top.get('similarity_source')}")

                ok = not problems
                status = "PASS" if ok else "FAIL(" + ",".join(problems) + ")"
                print(f"{q:<8} {translated:<28} {total:>4}  {status}")
                results.append({"query": q, "ok": ok, "problems": problems})
                rows.append({
                    "query": q,
                    "translated_query": translated,
                    "total": total,
                    "warnings": warnings,
                    "top_scene": (
                        results_list[0].get("scene", {}) or {}
                    ).get("primary_scene") if results_list else None,
                    "top_source": (
                        results_list[0].get("scene", {}) or {}
                    ).get("source") if results_list else None,
                })
            except Exception as e:
                print(f"{q:<8} 异常: {e}")
                results.append({"query": q, "ok": False, "reason": str(e)})

    return results, rows


def main() -> int:
    parser = argparse.ArgumentParser(description="搜索链路验收脚本")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", default="", help="JSON 报告输出路径")
    args = parser.parse_args()

    results, rows = run_checks(args.base_url)
    passed = sum(1 for r in results if r.get("ok"))
    failed = len(results) - passed
    print(f"\n=== 总结: {passed} PASS / {failed} FAIL ===")

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": args.base_url,
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "checks": results,
        "detail": rows,
    }
    out = args.output or "data/verify_search_report.json"
    Path(out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"报告已写入: {out}")
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
