"""构建云端 light demo 素材 v2：按「种子查询」定向取图，保证每个演示主题都有命中。

思路：不再随机抽样（随机抽样导致"海边日落"全库无人命中），
改为先用 Immich 原生 smart search 对 16 个演示主题各取 top-N 作为种子，
再补一批随机背景图，最后做坏图过滤（噪声/纯色）+ WebP 压缩 + 向量 + 场景标签。

用法：python tools/build_demo_assets2.py [--per-seed 24] [--extra 60]
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import sys
from collections import Counter
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.scene.clip_inferrer import SCENE_PROMPTS  # noqa: E402
from app.services.description_writeback import _SCENE_LABELS, _SUB_SCENE_LABELS  # noqa: E402

MODEL = "ViT-B-32__openai"
SCENE_THRESHOLD = 0.26
SUB_THRESHOLD = 0.28
THUMB_MAX = 480
THUMB_QUALITY = 76

SEEDS: list[str] = [
    "a photo of the sunset at the beach",
    "a photo of the sea and the sky",
    "a photo of a city skyline at night",
    "a photo of a snow mountain",
    "a portrait of a person",
    "hot pot on a table",
    "a photo of a blue car",
    "fireworks in the sky",
    "a photo of a cat",
    "a photo of a dog",
    "a graduation ceremony",
    "a photo of a forest",
    "street food on a stall",
    "a photo of a lake",
    "a photo of flowers",
    "a photo of a tall building",
    "a photo of a bridge over water",
    "people walking on a street",
]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
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


def is_bad_image(im: Image.Image) -> tuple[bool, str]:
    """坏图过滤：纯色/极低方差 → blank；高频能量异常 → noise。"""
    g = np.asarray(im.convert("L"), dtype=np.float32)
    if g.std() < 8:
        return True, "blank"
    hf = float(np.abs(np.diff(g, axis=1)).mean())
    if hf > 32:
        return True, f"noise(hf={hf:.1f})"
    return False, ""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-seed", type=int, default=24)
    ap.add_argument("--extra", type=int, default=60)
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    env = load_env()
    immich = env.get("IMMICH_BASE_URL", "http://127.0.0.1:3200").rstrip("/")
    key = env.get("IMMICH_API_KEY", "")
    ml = env.get("SCENE_INFERENCE_ENDPOINT", "http://127.0.0.1:3003").rstrip("/")
    headers = {"x-api-key": key} if key else {}
    sem = asyncio.Semaphore(args.concurrency)

    out_dir = ROOT / "data" / "demo"
    thumb_dir = out_dir / "thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    for old in thumb_dir.glob("*.webp"):
        old.unlink()

    picks: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(base_url=immich, headers=headers, trust_env=False, timeout=180) as ic, \
               httpx.AsyncClient(base_url=ml, trust_env=False, timeout=180) as mc:
        print("[1/5] 种子查询定向取图...")
        for q in SEEDS:
            r = await ic.post("/api/search/smart", json={"query": q, "size": args.per_seed, "page": 1})
            if r.status_code != 200:
                print(f"      seed fail {q}: {r.status_code}")
                continue
            items = (r.json().get("assets") or {}).get("items") or []
            added = 0
            for a in items:
                ck = a.get("id")
                if ck in seen:
                    continue
                seen.add(ck)
                a["_seed"] = q
                picks.append(a)
                added += 1
            print(f"      +{added:2d}  {q}")

        print(f"[2/5] 补 {args.extra} 张随机背景图...")
        for page in range(1, 4):
            r = await ic.post("/api/search/metadata", json={"size": 400, "page": page, "withExif": True})
            if r.status_code != 200:
                break
            items = (r.json().get("assets") or {}).get("items") or []
            if not items:
                break
            step = max(1, len(items) // max(1, args.extra // 3))
            for a in items[::step]:
                if len([p for p in picks if "_seed" not in p]) >= args.extra:
                    break
                if a.get("id") in seen or (a.get("type") or "").upper() != "IMAGE":
                    continue
                seen.add(a["id"])
                a["_seed"] = None
                picks.append(a)
        print(f"      候选合计 {len(picks)} 张")

        print("[3/5] 文本提示词向量...")
        uniq: dict[str, np.ndarray] = {}
        for k, ps in SCENE_PROMPTS.items():
            for p in ps:
                if p in uniq:
                    continue
                rr = await mc.post("/predict", data={"entries": _entries("textual"), "text": p})
                if rr.status_code == 200:
                    v = _vec(rr.json())
                    if v is not None:
                        uniq[p] = v
        matrix = {k: np.stack([uniq[p] for p in ps if p in uniq])
                  for k, ps in SCENE_PROMPTS.items()}
        matrix = {k: v for k, v in matrix.items() if len(v)}
        print(f"      {len(uniq)} 条提示词 / {len(matrix)} 个场景 key")

        print("[4/5] 下载 + 过滤 + 压缩 + 图像向量...")
        rows: list[dict] = []
        vectors: list[np.ndarray] = []
        dropped: Counter = Counter()
        done = 0

        async def one(a: dict) -> None:
            nonlocal done
            async with sem:
                aid = a["id"]
                try:
                    r = await ic.get(f"/api/assets/{aid}/thumbnail", params={"size": "preview"}, timeout=90)
                    if r.status_code != 200:
                        dropped["http"] += 1
                        return
                    raw = r.content
                    im = Image.open(io.BytesIO(raw)).convert("RGB")
                    bad, why = is_bad_image(im)
                    if bad:
                        dropped[why.split("(")[0]] += 1
                        return
                    im.thumbnail((THUMB_MAX, THUMB_MAX), Image.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, "WEBP", quality=THUMB_QUALITY, method=4)
                    (thumb_dir / f"{aid}.webp").write_bytes(buf.getvalue())
                    vec = None
                    for _ in range(2):
                        rr = await mc.post("/predict", data={"entries": _entries("visual")},
                                           files={"image": ("thumb", raw, "image/jpeg")}, timeout=180)
                        if rr.status_code == 200:
                            vec = _vec(rr.json())
                            if vec is not None:
                                break
                    if vec is None:
                        dropped["ml"] += 1
                        return
                    exif = a.get("exifInfo") or {}
                    rows.append({
                        "id": aid,
                        "w": im.width, "h": im.height,
                        "bytes": len(buf.getvalue()),
                        "takenAt": a.get("fileCreatedAt"),
                        "city": exif.get("city"), "country": exif.get("country"),
                        "fileName": a.get("originalFileName"),
                        "seed": a.get("_seed"),
                    })
                    vectors.append(vec)
                except Exception as exc:  # noqa: BLE001
                    dropped["exc"] += 1
                    print(f"      skip {aid}: {exc}")
                finally:
                    done += 1
                    if done % 40 == 0:
                        print(f"      {done}/{len(picks)}")

        await asyncio.gather(*(one(a) for a in picks))
        print("      丢弃:", dict(dropped))

        print("[5/5] 场景标签 + 落盘...")
        for row, vec in zip(rows, vectors):
            best_k, best_s = None, 0.0
            for k, mat in matrix.items():
                s = float(np.max(mat @ vec))
                if s > best_s:
                    best_k, best_s = k, s
            row["scene_key"] = best_k if best_s >= SCENE_THRESHOLD else None
            row["scene_sim"] = round(best_s, 4)
            sub_k, sub_s = None, 0.0
            for k, mat in matrix.items():
                if k == best_k or k not in _SUB_SCENE_LABELS:
                    continue
                s = float(np.max(mat @ vec))
                if s > sub_s:
                    sub_k, sub_s = k, s
            row["sub_key"] = sub_k if sub_s >= SUB_THRESHOLD else None
            row["scene_cn"] = _SCENE_LABELS.get(best_k or "")
            row["sub_cn"] = _SUB_SCENE_LABELS.get(sub_k or "")

    order = sorted(range(len(rows)), key=lambda i: rows[i]["takenAt"] or "")
    rows = [rows[i] for i in order]
    mat = np.stack([vectors[i] for i in order]).astype(np.float32)
    np.save(out_dir / "embeddings.npy", mat)
    (out_dir / "meta.json").write_text(json.dumps(
        {"model": MODEL, "dim": int(mat.shape[1]), "count": len(rows), "items": rows},
        ensure_ascii=False), encoding="utf-8")
    total = sum(r["bytes"] for r in rows)
    print(f"  embedding {mat.shape} / 缩略图 {len(rows)} 张 {total/1024/1024:.1f} MB")
    print("  场景分布:", Counter(r["scene_cn"] or "(无)" for r in rows).most_common())


if __name__ == "__main__":
    asyncio.run(main())
