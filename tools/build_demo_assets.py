
"""构建云端轻量 demo 的素材：200 张缩略图 + CLIP 图像向量 + 场景标签。

数据来源：本地 Immich（/api/search/metadata + /api/assets/{id}/thumbnail）
向量来源：本地 immich-machine-learning（/predict visual，ViT-B-32__openai）
输出：data/demo/{thumbs/*.webp, embeddings.npy, meta.json, prompt_emb.npz}

用法：python tools/build_demo_assets.py [--target 200] [--concurrency 6]
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import sys
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.scene.clip_inferrer import SCENE_PROMPTS  # noqa: E402
from app.services.description_writeback import (  # noqa: E402
    _SCENE_LABELS,
    _SUB_SCENE_LABELS,
)

MODEL = "ViT-B-32__openai"
SCENE_THRESHOLD = 0.24
SUB_THRESHOLD = 0.26
THUMB_MAX = 480
THUMB_QUALITY = 76


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip())
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return env


def _entries(kind: str) -> str:
    return json.dumps({"clip": {kind: {"modelName": MODEL}}})


def _vec(payload: dict) -> np.ndarray | None:
    raw = payload.get("clip")
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list) or not raw:
        return None
    v = np.asarray(raw, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


async def collect_assets(client: httpx.AsyncClient, want: int) -> list[dict]:
    seen_ck: set[str] = set()
    picked: list[dict] = []
    for page in range(1, 6):
        r = await client.post(
            "/api/search/metadata",
            json={"size": 1000, "page": page, "withExif": True},
            timeout=120,
        )
        r.raise_for_status()
        items = (r.json().get("assets") or {}).get("items") or []
        if not items:
            break
        for a in items:
            if (a.get("type") or "").upper() != "IMAGE":
                continue
            ck = a.get("checksum") or a["id"]
            if ck in seen_ck:
                continue
            seen_ck.add(ck)
            picked.append(a)
        if len(picked) >= want * 6:
            break
    picked.sort(key=lambda a: a.get("fileCreatedAt") or "")
    return picked


def spread(items: list[dict], want: int) -> list[dict]:
    if len(items) <= want:
        return items
    idx = np.linspace(0, len(items) - 1, want).round().astype(int)
    out, used = [], set()
    for i in idx:
        if int(i) in used:
            continue
        used.add(int(i))
        out.append(items[int(i)])
    return out


async def embed_image(client: httpx.AsyncClient, data: bytes) -> np.ndarray | None:
    r = await client.post(
        "/predict",
        data={"entries": _entries("visual")},
        files={"image": ("thumb", data, "image/jpeg")},
        timeout=120,
    )
    if r.status_code != 200:
        return None
    return _vec(r.json())


async def embed_text(client: httpx.AsyncClient, text: str) -> np.ndarray | None:
    r = await client.post(
        "/predict", data={"entries": _entries("textual"), "text": text}, timeout=120
    )
    if r.status_code != 200:
        return None
    return _vec(r.json())


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    env = load_env()
    immich = env.get("IMMICH_BASE_URL", "http://127.0.0.1:3200").rstrip("/")
    key = env.get("IMMICH_API_KEY", "")
    ml = env.get("SCENE_INFERENCE_ENDPOINT", "http://127.0.0.1:3003").rstrip("/")

    out_dir = ROOT / "data" / "demo"
    thumb_dir = out_dir / "thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)

    headers = {"x-api-key": key} if key else {}
    sem = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient(base_url=immich, headers=headers, trust_env=False) as ic, \
               httpx.AsyncClient(base_url=ml, trust_env=False) as mc:
        print("[1/5] 采集候选资产...")
        cand = await collect_assets(ic, args.target)
        print(f"      候选 {len(cand)} 张（已按 checksum 去重）")
        chosen = spread(cand, args.target)
        print(f"      抽样 {len(chosen)} 张")

        print("[2/5] 计算场景提示词文本向量...")
        prompts: dict[str, list[str]] = {}
        uniq: dict[str, np.ndarray] = {}
        for k, ps in SCENE_PROMPTS.items():
            got = []
            for p in ps:
                if p not in uniq:
                    v = await embed_text(mc, p)
                    if v is None:
                        continue
                    uniq[p] = v
                got.append(p)
            if got:
                prompts[k] = got
        matrix = {k: np.stack([uniq[p] for p in ps]) for k, ps in prompts.items()}
        print(f"      {len(uniq)} 条提示词, {len(matrix)} 个场景 key")

        print("[3/5] 下载缩略图 + 计算图像向量...")
        rows: list[dict] = []
        vectors: list[np.ndarray] = []
        done = 0

        async def one(a: dict) -> None:
            nonlocal done
            async with sem:
                aid = a["id"]
                try:
                    r = await ic.get(f"/api/assets/{aid}/thumbnail", params={"size": "preview"}, timeout=90)
                    if r.status_code != 200:
                        return
                    raw = r.content
                    im = Image.open(io.BytesIO(raw)).convert("RGB")
                    im.thumbnail((THUMB_MAX, THUMB_MAX), Image.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, "WEBP", quality=THUMB_QUALITY, method=4)
                    (thumb_dir / f"{aid}.webp").write_bytes(buf.getvalue())
                    vec = await embed_image(mc, raw)
                    if vec is None:
                        return
                    exif = a.get("exifInfo") or {}
                    rows.append({
                        "id": aid,
                        "w": im.width,
                        "h": im.height,
                        "bytes": len(buf.getvalue()),
                        "takenAt": a.get("fileCreatedAt"),
                        "city": exif.get("city"),
                        "country": exif.get("country"),
                        "fileName": a.get("originalFileName"),
                    })
                    vectors.append(vec)
                except Exception as exc:  # noqa: BLE001
                    print(f"      skip {aid}: {exc}")
                finally:
                    done += 1
                    if done % 20 == 0:
                        print(f"      {done}/{len(chosen)}")

        await asyncio.gather(*(one(a) for a in chosen))

        print("[4/5] 打场景标签...")
        for row, vec in zip(rows, vectors):
            best_key, best_sim = None, 0.0
            for k, mat in matrix.items():
                sim = float(np.max(mat @ vec))
                if sim > best_sim:
                    best_key, best_sim = k, sim
            row["scene_key"] = best_key if best_sim >= SCENE_THRESHOLD else None
            row["scene_sim"] = round(best_sim, 4)
            sub_key, sub_sim = None, 0.0
            for k, mat in matrix.items():
                if k not in _SUB_SCENE_LABELS or k == best_key:
                    continue
                sim = float(np.max(mat @ vec))
                if sim > sub_sim:
                    sub_key, sub_sim = k, sim
            row["sub_key"] = sub_key if sub_sim >= SUB_THRESHOLD else None
            row["scene_cn"] = _SCENE_LABELS.get(best_key or "", None)
            row["sub_cn"] = _SUB_SCENE_LABELS.get(sub_key or "", None)

    print("[5/5] 落盘...")
    order = sorted(range(len(rows)), key=lambda i: rows[i]["takenAt"] or "")
    rows = [rows[i] for i in order]
    mat = np.stack([vectors[i] for i in order]).astype(np.float32)
    np.save(out_dir / "embeddings.npy", mat)
    (out_dir / "meta.json").write_text(
        json.dumps({"model": MODEL, "dim": int(mat.shape[1]), "count": len(rows), "items": rows},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    np.savez(out_dir / "prompt_emb.npz",
             **{k: v for k, v in matrix.items()})
    total = sum(r["bytes"] for r in rows)
    print(f"  embedding {mat.shape} / 缩略图 {len(rows)} 张 {total/1024/1024:.1f} MB")
    from collections import Counter
    print("  场景分布:", Counter(r["scene_cn"] for r in rows).most_common())


if __name__ == "__main__":
    asyncio.run(main())
