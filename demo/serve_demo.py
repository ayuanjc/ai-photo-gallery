"""云影图鉴 · 轻量在线检索 demo（2C4G 可跑，无 Immich 依赖）。

- 图像向量：离线用本地 Immich ML（ViT-B-32__openai visual）预计算
- 文本向量：容器导出的同一份 CLIP 文本塔 ONNX，onnxruntime 本地推理
  （与 Immich ML /predict textual 实测 cos=1.000000，见 parity4 验证）
- 中文查询：复用项目 app/scene/query_translate.py 的完整短语组合
- 打分为真实余弦相似度，不做任何归一化伪装
- 活动台账与素材包引擎复用 app/workflow（同一份源码，不复制实现）

演示链路：选活动（台账锁定范围）→ 检索 → 生成素材包 → 池内改（换横/竖图、去掉）
→ 导出 zip（manifest.csv / manifest.json / 缩略图）。
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from tokenizers import Tokenizer

from query_translate import query_has_cjk, translate_query_for_clip

BASE = Path(os.environ.get("DEMO_BASE", Path(__file__).resolve().parent))
DATA = BASE / "data"
CLIP = Path(os.environ.get("DEMO_CLIP_DIR", "/opt/yunying/clip"))
ACTIVITIES_PATH = Path(
    os.environ.get("DEMO_ACTIVITIES", str(DATA / "activities" / "activities.json"))
)

# 台账/素材包引擎：部署形态下与 serve_demo.py 同目录（/opt/yunying/app/workflow），
# 仓库内直接跑时回落到 app/workflow —— 同一份源码，两种安装位置。
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # pragma: no cover - 取决于部署布局
    from workflow import (  # type: ignore
        ActivityStore,
        apply_ops,
        build_package,
        manifest_csv,
        manifest_json,
        public_view,
        summarize_diff,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.workflow import (  # type: ignore
        ActivityStore,
        apply_ops,
        build_package,
        manifest_csv,
        manifest_json,
        public_view,
        summarize_diff,
    )

CTX = 77
BOS, EOS = 49406, 49407
SCENE_CN = {
    "nature": "自然", "city": "城市", "people": "人物", "life": "生活",
    "event": "事件", "animal": "动物", "architecture": "建筑", "night": "夜景",
    "vehicle": "车辆", "art": "艺术",
}
SESSION_TTL = float(os.environ.get("DEMO_PACKAGE_TTL", "3600"))

app = FastAPI(title="云影图鉴 · 轻量语义检索", version="1.1.0")
_state: dict[str, Any] = {}
_sessions: dict[str, dict[str, Any]] = {}


class SearchReq(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    top_k: int = Field(24, ge=1, le=200)
    scene: str | None = None
    activity_id: str | None = Field(default=None, max_length=100)


class ResolveReq(BaseModel):
    query: str | None = Field(default=None, max_length=200)
    date: str | None = Field(default=None, max_length=40)


class PackageReq(BaseModel):
    activity_id: str = Field(..., min_length=1, max_length=100)
    query: str = Field(..., min_length=1, max_length=200)
    top_k: int = Field(60, ge=1, le=200)
    slots: dict[str, int] | None = None
    orientation: str = Field("any", pattern="^(any|landscape|portrait|square)$")


class EditReq(BaseModel):
    session_id: str
    revision: int = Field(..., ge=1)
    ops: list[dict[str, Any]] = Field(..., min_length=1, max_length=20)


class ExportReq(BaseModel):
    session_id: str


class ProposeReq(BaseModel):
    activity_id: str = Field(..., min_length=1, max_length=100)
    session_id: str | None = None


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
        "activities": ActivityStore(ACTIVITIES_PATH),
        "index": {str(it["id"]): i for i, it in enumerate(items)},
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


def _store() -> ActivityStore:
    store = _state.get("activities")
    if store is None:
        raise HTTPException(status_code=503, detail="台账尚未加载完成")
    return store


def _activity_or_404(activity_id: str) -> Any:
    act = _store().get(activity_id)
    if act is None:
        raise HTTPException(status_code=404, detail=f"活动不存在: {activity_id}")
    return act


def _candidate(it: dict[str, Any], score: float) -> dict[str, Any]:
    return {
        "asset_id": str(it["id"]),
        "score": round(float(score), 4),
        "w": it.get("w"),
        "h": it.get("h"),
        "taken_at": it.get("takenAt"),
        "scene": it.get("scene_cn"),
        "scene_sim": it.get("scene_sim"),
        "fileName": it.get("fileName"),
        "thumb": f"/thumbs/{it['id']}.webp",
    }


def _rank(req_query: str, activity_id: str | None, limit: int) -> dict[str, Any]:
    """真实余弦排序，activity_id 存在时把范围锁在该活动台账白名单内。"""
    if not _state:
        raise HTTPException(status_code=503, detail="模型尚未加载完成")

    t0 = time.perf_counter()
    translated = req_query
    used_translation = False
    if query_has_cjk(req_query):
        translated, used_translation = translate_query_for_clip(req_query)

    t1 = time.perf_counter()
    cached = translated in _QCACHE
    qv = _embed_text(translated)
    t2 = time.perf_counter()

    items = _state["items"]
    scores = _state["mat"] @ qv
    order = np.argsort(-scores)

    allowed: set[str] | None = None
    if activity_id:
        act = _activity_or_404(activity_id)
        allowed = set(act.asset_ids)
        order = np.array([i for i in order if str(items[int(i)]["id"]) in allowed], dtype=int)

    order = order[:limit]
    return {
        "query": req_query,
        "translated": translated,
        "translated_used": used_translation,
        "text_cached": cached,
        "order": order,
        "scores": scores,
        "scoped_to": activity_id,
        "ranked_total": int(len(order)),
        "timing_ms": {
            "translate": round((t1 - t0) * 1000, 2),
            "text_embed": round((t2 - t1) * 1000, 1),
            "total": round((time.perf_counter() - t0) * 1000, 1),
        },
    }


def _purge_sessions() -> None:
    now = time.time()
    for sid in [k for k, v in _sessions.items() if v.get("saved_at", 0) + SESSION_TTL < now]:
        _sessions.pop(sid, None)


def _save_session(state: dict[str, Any]) -> None:
    state["saved_at"] = time.time()
    _sessions[state["session_id"]] = state


def _load_session(session_id: str) -> dict[str, Any]:
    _purge_sessions()
    state = _sessions.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="素材包会话不存在或已过期")
    return state


def _session_view(state: dict[str, Any]) -> dict[str, Any]:
    view = public_view(state)
    view.pop("saved_at", None)
    return view


@app.get("/api/health")
def health() -> dict[str, Any]:
    store = _state.get("activities")
    return {
        "status": "ok",
        "model": _state.get("model"),
        "count": len(_state.get("items", [])),
        "dim": int(_state["mat"].shape[1]) if "mat" in _state else 0,
        "load_ms": _state.get("load_ms"),
        "activities": len(store.list()) if store else 0,
    }


@app.post("/api/search")
def search(req: SearchReq) -> JSONResponse:
    ranked = _rank(req.query, req.activity_id, req.top_k)
    items = _state["items"]
    scores = ranked["scores"]
    idx = ranked["order"]
    if req.scene:
        idx = np.array([i for i in idx if items[int(i)].get("scene_key") == req.scene])

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
        "translated": ranked["translated"],
        "translated_used": ranked["translated_used"],
        "text_cached": ranked["text_cached"],
        "top_k": len(results),
        "scoped_to": ranked["scoped_to"],
        "results": results,
        "timing_ms": ranked["timing_ms"],
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


# ---- 活动台账 -----------------------------------------------------------

@app.get("/api/activities")
def activities() -> dict[str, Any]:
    store = _store()
    return {
        "total": len(store.list()),
        "activities": [act.to_summary() for act in store.list()],
        "ledger": str(store.path),
    }


@app.post("/api/activities/resolve")
def activities_resolve(req: ResolveReq) -> dict[str, Any]:
    if not (req.query or req.date):
        raise HTTPException(status_code=422, detail="query 或 date 至少给一个")
    outcome = _store().resolve(query=req.query, date=req.date)
    return outcome.to_dict()


# ---- 素材包 -------------------------------------------------------------

@app.post("/api/package")
def package_create(req: PackageReq) -> dict[str, Any]:
    act = _activity_or_404(req.activity_id)
    ranked = _rank(req.query, req.activity_id, req.top_k)
    items = _state["items"]
    scores = ranked["scores"]
    candidates = [
        _candidate(items[int(i)], float(scores[int(i)]))
        for i in ranked["order"]
    ]
    state = build_package(
        act.to_dict(),
        candidates,
        slots=req.slots,
        orientation=req.orientation,
        ttl_s=int(SESSION_TTL),
    )
    state["query"] = req.query
    _save_session(state)
    view = _session_view(state)
    view["translated_query"] = ranked["translated"]
    view["ranked_total"] = ranked["ranked_total"]
    view["timing_ms"] = ranked["timing_ms"]
    return view


@app.get("/api/package/{session_id}")
def package_get(session_id: str) -> dict[str, Any]:
    return _session_view(_load_session(session_id))


@app.post("/api/package/edit")
def package_edit(req: EditReq) -> dict[str, Any]:
    state = _load_session(req.session_id)
    try:
        after = apply_ops(state, req.ops, expected_revision=req.revision)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    after["query"] = state.get("query")
    _save_session(after)
    view = _session_view(after)
    view["changes"] = after.get("changes")
    view["diff"] = summarize_diff(state, after)
    return view


@app.post("/api/package/export")
def package_export(req: ExportReq) -> Response:
    state = _load_session(req.session_id)
    activity = state.get("activity") or {}
    buf = io.BytesIO()
    missing: list[str] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.csv", manifest_csv(state))
        zf.writestr("manifest.json", json.dumps(manifest_json(state), ensure_ascii=False, indent=2))
        readme = (
            f"云影图鉴 · 素材包导出\n"
            f"活动：{activity.get('name') or ''}（{activity.get('id') or ''}）\n"
            f"时段：{activity.get('starts_at') or '未知'} ~ {activity.get('ends_at') or '未知'}\n"
            f"检索词：{state.get('query') or ''}\n"
            f"会话：{state.get('session_id')} revision={state.get('revision')}\n"
            f"候选池：{len(state.get('pool') or [])} 张；每张图的选图理由见 manifest.csv\n"
            f"缩略图为 480px 宽 webp→jpg 转存，原图请在 Immich 按 asset_id 取。\n"
        )
        zf.writestr("README.txt", readme)
        for slot, items in (state.get("package") or {}).items():
            for idx, it in enumerate(items, start=1):
                src = DATA / "thumbs" / f"{it['asset_id']}.webp"
                if not src.exists():
                    missing.append(str(it["asset_id"]))
                    continue
                zf.writestr(f"thumbs/{slot}-{idx:02d}-{it['asset_id']}.webp", src.read_bytes())
        if missing:
            zf.writestr("MISSING.txt", "\n".join(missing))
    name = f"yunying-{(activity.get('id') or 'package')}-{state.get('session_id')}.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ---- 归档提案（只读，演示「确认后才写入」）-------------------------------

@app.post("/api/archive/propose")
def archive_propose(req: ProposeReq) -> dict[str, Any]:
    act = _activity_or_404(req.activity_id)
    asset_ids = list(act.asset_ids)
    if req.session_id:
        state = _load_session(req.session_id)
        ids: list[str] = []
        for items in (state.get("package") or {}).values():
            for it in items:
                aid = str(it.get("asset_id"))
                if aid and aid not in ids:
                    ids.append(aid)
        if ids:
            asset_ids = ids
    outside = [a for a in asset_ids if a not in set(act.asset_ids)]
    if outside:
        raise HTTPException(status_code=422, detail=f"{len(outside)} 张照片不属于该活动")
    return {
        "dry_run": True,
        "album": {
            "name": act.album_name(),
            "exists": False,
            "asset_count": 0,
            "url": None,
        },
        "will_add": len(asset_ids),
        "already_in": 0,
        "warnings": [
            "演示站不连接图库：这里只算出归档计划，确认动作在主应用 /api/v1/archive/commit 完成",
        ],
        "note": "提案阶段零写入；真实写回见 主应用 → 归档（propose → commit 两步）",
    }


# ---- 本地直跑时也能打开页面（线上由 nginx 直接发 web/）----
# 必须放在所有 /api 路由之后：Mount("/") 会吞掉其后注册的一切路径。
_WEB = BASE / "web"
if (_WEB / "index.html").exists():  # pragma: no cover - 取决于部署布局
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_WEB), html=True), name="web")
