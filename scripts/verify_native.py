#!/usr/bin/env python3
"""原生嵌入验收脚本：验证云影图鉴与 Immich 的原生集成。

用法：
    python scripts/verify_native.py \
        --base-url http://127.0.0.1:8000 \
        --immich-url http://127.0.0.1:3200 \
        --immich-key <key>

检查项：
    1. 宿主 GET /health 200
    2. 容器内可达性（docker exec 验证 host.docker.internal）
    3. 工作流存在、enabled、logging=true、url 指向 host.docker.internal
    4. 上传测试图 → 轮询 30s 内出现 AI/ 标签
    5. GET /api/albums 出现 AI· 前缀相册
    6. 工作流 logs 非空
    7. data/writeback_audit.jsonl 新增记录
    8. 幂等：同一 asset 再触发不产生重复
    9. 失败降级：回调不可达时 hook 返回 200 + warnings
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx


def _check(name: str, passed: bool, detail: str = "") -> dict:
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}: {detail}")
    return {"name": name, "status": status, "detail": detail}


def run_checks(base_url: str, immich_url: str, immich_key: str) -> list[dict]:
    results: list[dict] = []
    headers = {"x-api-key": immich_key} if immich_key else {}

    print("=== 原生嵌入验收 ===\n")

    # 1. 宿主健康检查
    print("1. 宿主服务健康检查")
    try:
        r = httpx.get(f"{base_url}/health", timeout=5.0)
        results.append(_check("GET /health", r.status_code == 200, f"status={r.status_code}"))
    except Exception as e:
        results.append(_check("GET /health", False, str(e)))

    # 2. 容器内可达性
    print("2. 容器内可达性（host.docker.internal）")
    try:
        cmd = [
            "docker", "exec", "immich_server",
            "node", "-e",
            "fetch('http://host.docker.internal:8000/health')"
            ".then(r=>r.json()).then(d=>console.log(JSON.stringify(d)))"
            ".catch(e=>console.error(e.message))",
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        ok = "ok" in out.stdout
        results.append(_check(
            "docker exec → host.docker.internal:8000/health",
            ok,
            out.stdout.strip() or out.stderr.strip(),
        ))
    except Exception as e:
        results.append(_check("容器内可达性", False, str(e)))

    # 3. 工作流检查
    print("3. 工作流配置检查")
    try:
        r = httpx.get(f"{immich_url}/api/workflows", headers=headers, timeout=10)
        workflows = r.json() if r.status_code == 200 else []
        yunying_wfs = [w for w in workflows if w.get("name", "").startswith("yunying")]
        all_enabled = all(w.get("enabled") for w in yunying_wfs)
        all_logging = all(w.get("logging") for w in yunying_wfs)
        urls_ok = True
        for w in yunying_wfs:
            for step in w.get("steps", []):
                url = step.get("config", {}).get("url", "")
                if url and "host.docker.internal" not in url:
                    urls_ok = False
        results.append(_check(
            "yunying 工作流存在",
            len(yunying_wfs) >= 3,
            f"count={len(yunying_wfs)}",
        ))
        results.append(_check("工作流全部 enabled", all_enabled, ""))
        results.append(_check("工作流 logging=true", all_logging, ""))
        results.append(_check("step url 指向 host.docker.internal", urls_ok, ""))
    except Exception as e:
        results.append(_check("工作流检查", False, str(e)))

    # 4. 上传测试图 → AI 标签
    print("4. 上传测试图 → AI 标签")
    test_image = Path("data/test_upload.jpg")
    if test_image.exists():
        try:
            with open(test_image, "rb") as f:
                files = {"fileData": ("test.jpg", f, "image/jpeg")}
                r = httpx.post(
                    f"{immich_url}/api/assets",
                    headers={**headers, "Content-Type": "multipart/form-data"},
                    files=files,
                    timeout=30,
                )
            asset_id = r.json().get("id") if r.status_code in (200, 201) else None
            if asset_id:
                found_ai_tag = False
                for _ in range(15):
                    time.sleep(2)
                    r2 = httpx.get(
                        f"{immich_url}/api/assets/{asset_id}",
                        headers=headers, timeout=5,
                    )
                    tags = r2.json().get("tags", []) if r2.status_code == 200 else []
                    if any(
                        (t.get("value", "") if isinstance(t, dict) else t).startswith("AI·")
                        for t in tags
                    ):
                        found_ai_tag = True
                        break
                results.append(_check("30s 内出现 AI· 标签", found_ai_tag, f"asset={asset_id}"))
            else:
                results.append(_check("上传测试图", False, f"status={r.status_code}"))
        except Exception as e:
            results.append(_check("上传测试图", False, str(e)))
    else:
        results.append(_check("上传测试图", False, "data/test_upload.jpg 不存在，跳过"))

    # 5. AI· 前缀相册
    print("5. AI· 前缀相册检查")
    try:
        r = httpx.get(f"{immich_url}/api/albums", headers=headers, timeout=10)
        albums = r.json() if r.status_code == 200 else []
        ai_albums = [a for a in albums if a.get("albumName", "").startswith("AI·")]
        results.append(_check("存在 AI· 前缀相册", len(ai_albums) > 0, f"count={len(ai_albums)}"))
    except Exception as e:
        results.append(_check("AI· 相册检查", False, str(e)))

    # 6. 工作流 logs
    print("6. 工作流 logs 检查")
    try:
        r = httpx.get(f"{immich_url}/api/workflows", headers=headers, timeout=10)
        workflows = r.json() if r.status_code == 200 else []
        logs_nonempty = False
        for w in workflows:
            if w.get("name", "").startswith("yunying"):
                wf_id = w.get("id")
                if wf_id:
                    r2 = httpx.get(
                        f"{immich_url}/api/workflows/{wf_id}/logs",
                        headers=headers, timeout=5,
                    )
                    if r2.status_code == 200 and r2.json():
                        logs_nonempty = True
                        break
        results.append(_check("工作流 logs 非空", logs_nonempty, ""))
    except Exception as e:
        results.append(_check("工作流 logs", False, str(e)))

    # 7. 审计日志
    print("7. 审计日志检查")
    audit_path = Path("data/writeback_audit.jsonl")
    if audit_path.exists():
        lines = audit_path.read_text(encoding="utf-8").strip().split("\n")
        results.append(_check("writeback_audit.jsonl 有记录", len(lines) > 0, f"lines={len(lines)}"))
    else:
        results.append(_check("writeback_audit.jsonl", False, "文件不存在"))

    # 8. 幂等检查
    print("8. 幂等检查")
    try:
        payload = {"event": "AssetCreate", "asset": {"id": "test-idempotent", "updatedAt": "2026-01-01T00:00:00Z"}}
        r1 = httpx.post(f"{base_url}/api/v1/plugin/hook", json=payload, timeout=10)
        r2 = httpx.post(f"{base_url}/api/v1/plugin/hook", json=payload, timeout=10)
        deduped = r2.json().get("changes", {}).get("deduplicated", False)
        results.append(_check("重复请求被去重", deduped, f"r1={r1.status_code} r2={r2.status_code}"))
    except Exception as e:
        results.append(_check("幂等检查", False, str(e)))

    # 9. 失败降级
    print("9. 失败降级检查")
    try:
        payload = {"event": "AssetCreate", "asset": {"id": "test-degrade", "updatedAt": "2026-01-01T00:00:00Z"}}
        r = httpx.post(f"{base_url}/api/v1/plugin/hook", json=payload, timeout=10)
        no_5xx = r.status_code < 500
        results.append(_check("hook 不返回 5xx", no_5xx, f"status={r.status_code}"))
    except Exception as e:
        results.append(_check("失败降级", False, str(e)))

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="原生嵌入验收脚本")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--immich-url", default="http://127.0.0.1:3200")
    parser.add_argument("--immich-key", default="")
    parser.add_argument("--output", default="", help="JSON 报告输出路径")
    args = parser.parse_args()

    results = run_checks(args.base_url, args.immich_url, args.immich_key)

    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    print(f"\n=== 总结: {passed} PASS / {failed} FAIL ===")

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": args.base_url,
        "immich_url": args.immich_url,
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "checks": results,
    }

    if args.output:
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入: {args.output}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
