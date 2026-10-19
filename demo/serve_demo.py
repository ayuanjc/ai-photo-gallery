
"""云影图鉴 · 轻量在线检索 demo（2C4G 可跑，无 Immich 依赖）。

- 图像向量：离线用本地 Immich ML（ViT-B-32__openai visual）预计算 200 张
- 文本向量：容器导出的同一份 CLIP 文本塔 ONNX，onnxruntime 本地推理
  （与 Immich ML /predict textual 实测 cos=1.000000，见 parity4 验证）
- 中文查询：复用项目 app/scene/query_translate.py 的完整短语组合
- 打分为真实余弦相似度，不做任何归一化伪装
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from tokenizers import Tokenizer

from query_translate import query_has_cjk, translate_query_for_clip

BASE = Path(os.environ.get("DEMO_BASE", Path(__file__).resolve().parent))
DATA = BASE / "data"
CLIP = Path(os.environ.get("DEMO_CLIP_DIR", "/opt/yunying/clip"))

CTX = 77
BOS, EOS = 49406, 49407
SCENE_CN = {
    "nature": "自然", "city": "城市", "people": "人物", "life": "生活",
    "event": "事件", "animal": "动物", "architecture": "建筑", "night": "夜景",
    "vehicle": "车辆", "art": "艺术",
}

app = FastAPI(title="云影图鉴 · 轻量语义检索", version="1.0.0")
_state: dict[str, Any] = {}


class SearchReq(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    top_k: int = Field(24, ge=1, le=200)
    scene: str | None = None


@app.on_event("startup")
def _load() -> None:
    t0 = time.perf_counter()
    meta = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))
    items = meta["items"]
    mat = np.load(DATA / "embeddings.npy").astype(np.float32)
    # 行归一化，保证余弦 = 内积
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mat = mat / np.clip(norms, 1e-8, None)

    tok = Tokenizer.from_file(str(CLIP / "tokenizer.json"))
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = int(os.environ.get("ORT_THREADS", "2"))
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    sess = ort.InferenceSession(
        str(CLIP / "model.onnx"), sess_options=opts, providers=["CPUExecutionProvider"]
    )

    # 预热：跑一次空查询，避免首个真实请求吃冷启动
    warm = np.zeros((1, CTX), dtype=np.int32)
    warm[0, 0] = BOS
    warm[0, 1] = EOS
    sess.run(None, {"text": warm})

    _state.update({
        "items": items,
        "mat": mat,
        "tok": tok,
        "sess": sess,
        "model": meta.get("model", "ViT-B-32__openai"),
        "load_ms": round((time.perf_counter() - t0) * 1000, 1),
    })


_QCACHE: dict[str, np.ndarray] = {}


def _embed_text(text: str) -> np.ndarray:
    hit = _QCACHE.get(text)
    if hit is not None:
        return hit
    ids = _state["tok"].encode(text, add_special_tokens=False).ids[: CTX - 2]
    arr = np.zeros((1, CTX), dtype=np.int32)
    arr[0, 0] = BOS
    arr[0, 1 : 1 + len(ids)] = ids
    arr[0, 1 + len(ids)] = EOS
    vec = _state["sess"].run(None, {"text": arr})[0][0].astype(np.float32)
    n = float(np.linalg.norm(vec))
    vec = vec / n if n > 0 else vec
    if len(_QCACHE) < 512:
        _QCACHE[text] = vec
    return vec


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model": _state.get("model"),
        "count": len(_state.get("items", [])),
        "dim": int(_state["mat"].shape[1]) if "mat" in _state else 0,
        "load_ms": _state.get("load_ms"),
    }


@app.post("/api/search")
def search(req: SearchReq) -> JSONResponse:
    if not _state:
        raise HTTPException(status_code=503, detail="模型尚未加载完成")

    t0 = time.perf_counter()
    translated = req.query
    used_translation = False
    if query_has_cjk(req.query):
        translated, used_translation = translate_query_for_clip(req.query)

    t1 = time.perf_counter()
    cached = translated in _QCACHE
    qv = _embed_text(translated)
    t2 = time.perf_counter()

    items = _state["items"]
    mat = _state["mat"]
    scores = mat @ qv

    idx = np.argsort(-scores)
    if req.scene:
        idx = np.array([i for i in idx if items[i].get("scene_key") == req.scene])

    idx = idx[: req.top_k]
    top = float(scores[idx[0]]) if len(idx) else 0.0
    results = []
    for i in idx:
        it = items[int(i)]
        s = float(scores[int(i)])
        results.append({
            "id": it["id"],
            "score": round(s, 4),
            "rel": round(s / top, 3) if top > 0 else 0.0,
            "thumb": f"/thumbs/{it['id']}.webp",
            "w": it.get("w"),
            "h": it.get("h"),
            "takenAt": it.get("takenAt"),
            "fileName": it.get("fileName"),
            "city": it.get("city"),
            "country": it.get("country"),
            "scene": it.get("scene_cn"),
            "sub": it.get("sub_cn"),
            "scene_sim": it.get("scene_sim"),
        })

    return JSONResponse({
        "query": req.query,
        "translated": translated,
        "translated_used": used_translation,
        "text_cached": cached,
        "top_k": len(results),
        "results": results,
        "timing_ms": {
            "translate": round((t1 - t0) * 1000, 2),
            "text_embed": round((t2 - t1) * 1000, 1),
            "total": round((time.perf_counter() - t0) * 1000, 1),
        },
    })


@app.get("/api/scenes")
def scenes() -> dict[str, Any]:
    from collections import Counter

    c = Counter(it.get("scene_key") for it in _state.get("items", []) if it.get("scene_key"))
    return {"scenes": [{"key": k, "label": SCENE_CN.get(k, k), "count": v} for k, v in c.most_common()]}


@app.get("/api/item/{item_id}")
def item(item_id: str) -> dict[str, Any]:
    for it in _state.get("items", []):
        if it["id"] == item_id:
            return it
    raise HTTPException(status_code=404, detail="not found")