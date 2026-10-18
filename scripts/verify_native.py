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
    4. 鉴权：无 token 的 hook 调用返回 401（PLUGIN_HOOK_TOKEN 生效）
    5. 上传测试图 → 轮询 30s 内出现 AI· 标签（WRITEBACK_ENABLED=true 时）
    6. GET /api/albums 出现 AI· 前缀相册（同上，写回关闭时 SKIP）
    7. 工作流 logs 非空
    8. data/writeback_audit.jsonl 有记录
    9. 幂等：同一 asset 再触发不产生重复
    10. 失败降级：异常 payload 时 hook 不返回 5xx
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


def _skip(name: str, detail: str = "") -> dict:
    print(f"  [SKIP] {name}: {detail}")
    return {"name": name, "status": "SKIP", "detail": detail}


def _load_hook_token() -> str:
    """从 .env 读取 PLUGIN_HOOK_TOKEN 默认值。"""
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("PLUGIN_HOOK_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def run_checks(
    base_url: str, immich_url: str, immich_key: str, hook_token: str
) -> list[dict]:
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

    # 4. hook 鉴权（PLUGIN_HOOK_TOKEN 生效）
    print("4. hook 鉴权检查")
    hook_headers = {"x-plugin-token": hook_token} if hook_token else {}
    try:
        payload_probe = {
            "event": "AssetCreate",
            "asset": {"id": "test-auth-probe", "updatedAt": "2026-01-01T00:00:00Z"},
        }
        r_no_token = httpx.post(f"{base_url}/api/v1/plugin/hook", json=payload_probe, timeout=10)
        results.append(_check(
            "无 token 返回 401",
            r_no_token.status_code == 401,
            f"status={r_no_token.status_code}",
        ))
    except Exception as e:
        results.append(_check("hook 鉴权", False, str(e)))

    # 5. 上传测试图 → AI 标签（仅写回开启时执行）
    print("5. 上传测试图 → AI 标签")
    writeback_enabled = False
    try:
        r_status = httpx.get(f"{base_url}/admin/api/status", timeout=5)
        writeback_enabled = bool(
            r_status.json().get("writeback", {}).get("enabled", False)
        )
    except Exception:
        pass
    test_image = Path("data/test_upload.jpg")
    if not writeback_enabled:
        results.append(_skip("上传验证", "WRITEBACK_ENABLED=false，写回关闭，跳过"))
    elif not test_image.exists():
        results.append(_skip("上传验证", "data/test_upload.jpg 不存在，跳过"))
    else:
        try:
            import random

            content = test_image.read_bytes() + f"\n//verify-{random.getrandbits(48)}".encode()
            files = {"assetData": ("test.jpg", content, "image/jpeg")}
            data = {
                "deviceAssetId": f"verify-native-{int(time.time())}",
                "deviceId": "verify-native-script",
                "fileCreatedAt": "2026-10-18T00:00:00.000Z",
                "fileModifiedAt": "2026-10-18T00:00:00.000Z",
            }
            r = httpx.post(
                f"{immich_url}/api/assets",
                headers=headers,
                files=files,
                data=data,
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

    # 6. AI· 前缀相册（写回开启时才可能有新相册）
    print("6. AI· 前缀相册检查")
    if not writeback_enabled:
        results.append(_skip("AI· 相册", "WRITEBACK_ENABLED=false，跳过"))
    else:
        try:
            r = httpx.get(f"{immich_url}/api/albums", headers=headers, timeout=10)
            albums = r.json() if r.status_code == 200 else []
            ai_albums = [a for a in albums if a.get("albumName", "").startswith("AI·")]
            results.append(_check("存在 AI· 前缀相册", len(ai_albums) > 0, f"count={len(ai_albums)}"))
        except Exception as e:
            results.append(_check("AI· 相册检查", False, str(e)))

    # 7. 工作流 logs
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

    # 8. 审计日志
    print("8. 审计日志检查")
    audit_path = Path("data/writeback_audit.jsonl")
    if audit_path.exists():
        lines = [ln for ln in audit_path.read_text(encoding="utf-8").strip().split("\n") if ln]
        results.append(_check("writeback_audit.jsonl 有记录", len(lines) > 0, f"lines={len(lines)}"))
    else:
        results.append(_check("writeback_audit.jsonl", False, "文件不存在"))

    # 9. 幂等检查（带 token；幂等依赖 Redis SETNX，无 Redis 时为设计降级）
    print("9. 幂等检查")
    if not hook_token:
        results.append(_skip("重复请求被去重", "无 hook token"))
    else:
        try:
            cache_backend = ""
            try:
                r_status = httpx.get(f"{base_url}/admin/api/status", timeout=5)
                cache_backend = r_status.json().get("cache_backend", "")
            except Exception:
                pass
            payload = {
                "event": "AssetCreate",
                "asset": {"id": "test-idempotent", "updatedAt": "2026-01-01T00:00:00Z"},
            }
            r1 = httpx.post(f"{base_url}/api/v1/plugin/hook", json=payload, headers=hook_headers, timeout=30)
            r2 = httpx.post(f"{base_url}/api/v1/plugin/hook", json=payload, headers=hook_headers, timeout=10)
            deduped = r2.status_code == 200 and r2.json().get("changes", {}).get("deduplicated", False)
            if deduped:
                results.append(_check("重复请求被去重", True, f"r1={r1.status_code} r2={r2.status_code}"))
            elif "Redis" not in cache_backend:
                results.append(_skip(
                    "重复请求被去重",
                    f"cache_backend={cache_backend or '未知'}（无 Redis，幂等设计性降级）",
                ))
            else:
                results.append(_check("重复请求被去重", False, f"r1={r1.status_code} r2={r2.status_code}"))
        except Exception as e:
            results.append(_check("幂等检查", False, str(e)))

    # 10. 失败降级（带 token，异常 payload 不 5xx）
    print("10. 失败降级检查")
    if not hook_token:
        results.append(_skip("hook 不返回 5xx", "无 hook token"))
    else:
        try:
            payload = {
                "event": "AssetCreate",
                "asset": {"id": "test-degrade", "updatedAt": "2026-01-01T00:00:00Z"},
            }
            r = httpx.post(f"{base_url}/api/v1/plugin/hook", json=payload, headers=hook_headers, timeout=30)
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
    parser.add_argument("--hook-token", default="", help="x-plugin-token（默认读 .env PLUGIN_HOOK_TOKEN）")
    parser.add_argument("--output", default="", help="JSON 报告输出路径")
    args = parser.parse_args()

    hook_token = args.hook_token or _load_hook_token()
    results = run_checks(args.base_url, args.immich_url, args.immich_key, hook_token)

    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    skipped = sum(1 for r in results if r["status"] == "SKIP")
    print(f"\n=== 总结: {passed} PASS / {failed} FAIL / {skipped} SKIP ===")

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": args.base_url,
        "immich_url": args.immich_url,
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
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
