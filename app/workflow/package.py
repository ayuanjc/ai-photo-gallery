"""素材包引擎：把候选图按用途排成可编辑、可导出的包。

要点：
- 素材包是可变状态（session_id + 单调 revision + 候选池），
  「换成横图」「去掉这张」在池内重排，绝不重跑检索。
- 纯函数 + 纯标准库，主应用与轻量 demo 共用。
"""
from __future__ import annotations

import csv
import io
import uuid
from typing import Any, Iterable

DEFAULT_SLOTS: dict[str, int] = {"cover": 3, "body": 5}
VALID_ORIENTATIONS = ("any", "landscape", "portrait", "square")
SLOT_LABEL = {"cover": "封面候选", "body": "正文配图"}


def orientation_of(item: dict[str, Any]) -> str:
    w = item.get("w") or 0
    h = item.get("h") or 0
    if not w or not h:
        return "unknown"
    if abs(w - h) <= 0.05 * max(w, h):
        return "square"
    return "landscape" if w > h else "portrait"


def _fits(item: dict[str, Any], orientation: str) -> bool:
    if orientation == "any":
        return True
    return orientation_of(item) == orientation


def _sort_key(item: dict[str, Any], orientation: str) -> tuple:
    """排序：先方向契合，再分数，再横图优先（封面更常用）。"""
    fit = 0 if _fits(item, orientation) else 1
    score = -(item.get("score") or 0.0)
    wide = 0 if orientation_of(item) == "landscape" else 1
    return (fit, score, wide, str(item.get("asset_id")))


def _fmt_time(value: str | None) -> str:
    if not value:
        return "时间未知"
    return value.replace("T", " ")[:16]


def build_reason(
    item: dict[str, Any],
    activity: dict[str, Any],
    slot: str,
    orientation: str,
    pool_rank: int,
) -> str:
    """可验证的选图理由：场景 + 时段 + 尺寸 + 同区间竞争。"""
    parts: list[str] = []
    scene = item.get("scene")
    sim = item.get("scene_sim")
    if scene:
        parts.append(f"画面场景={scene}" + (f"（{sim:.2f}）" if isinstance(sim, (int, float)) else ""))
    else:
        parts.append("画面场景未分类（纯语义相似度命中）")
    if activity.get("starts_at"):
        parts.append(f"拍摄 {_fmt_time(item.get('taken_at'))} 在活动时段内")
    else:
        parts.append(f"拍摄 {_fmt_time(item.get('taken_at'))}")
    w, h = item.get("w"), item.get("h")
    if w and h:
        shape = {"landscape": "横向", "portrait": "竖向", "square": "方形"}.get(orientation_of(item), "未知")
        need = "，适配封面" if slot == "cover" else ""
        parts.append(f"{w}×{h} {shape}{need}")
    parts.append(f"检索分 {item.get('score') if item.get('score') is not None else '-'}，池内第 {pool_rank + 1} 位")
    return "；".join(parts)


def _decorate(
    item: dict[str, Any],
    activity: dict[str, Any],
    slot: str,
    orientation: str,
    rank: int,
) -> dict[str, Any]:
    out = dict(item)
    out["orientation"] = orientation_of(item)
    out["reason"] = build_reason(item, activity, slot, orientation, rank)
    return out


def new_session_id() -> str:
    return uuid.uuid4().hex[:16]


def build_package(
    activity: dict[str, Any],
    candidates: list[dict[str, Any]],
    slots: dict[str, int] | None = None,
    orientation: str = "any",
    session_id: str | None = None,
    ttl_s: int = 3600,
) -> dict[str, Any]:
    """用候选池生成初始素材包。"""
    if orientation not in VALID_ORIENTATIONS:
        raise ValueError(f"orientation 必须是 {VALID_ORIENTATIONS} 之一")
    layout = dict(DEFAULT_SLOTS if slots is None else slots)
    for name, count in layout.items():
        if not isinstance(count, int) or count < 0 or count > 50:
            raise ValueError(f"slots.{name} 必须是 0-50 的整数")

    ordered = sorted(candidates, key=lambda it: _sort_key(it, orientation))
    # 池内位次：理由里报的是这张在图池里排第几，越靠前越强。
    pool_rank = {id(it): idx for idx, it in enumerate(ordered)}
    package: dict[str, list[dict[str, Any]]] = {name: [] for name in layout}
    used: set[str] = set()
    cursor = 0
    for name, count in layout.items():
        while len(package[name]) < count and cursor < len(ordered):
            item = ordered[cursor]
            cursor += 1
            aid = str(item["asset_id"])
            if aid in used:
                continue
            used.add(aid)
            package[name].append(_decorate(item, activity, name, orientation, pool_rank[id(item)]))

    rejected = [
        {"asset_id": it["asset_id"], "why": f"方向不匹配（{orientation_of(it)}）"}
        for it in ordered[cursor:]
        if not _fits(it, orientation)
    ][:20]

    return {
        "session_id": session_id or new_session_id(),
        "revision": 1,
        "ttl_s": ttl_s,
        "activity": {k: v for k, v in activity.items() if k != "asset_ids"},
        "slots": layout,
        "orientation": orientation,
        "package": package,
        "pool": ordered,
        "pool_size": len(ordered),
        "used": sorted(used),
        "rejected": rejected,
    }


def public_view(state: dict[str, Any]) -> dict[str, Any]:
    """对外响应：不含候选池明细。"""
    view = {k: v for k, v in state.items() if k != "pool"}
    view["pool_size"] = len(state.get("pool") or [])
    view["empty_slots"] = [name for name, items in (state.get("package") or {}).items() if not items]
    return view


def _fill_slot(
    state: dict[str, Any],
    slot: str,
    target: int,
    orientation: str,
    exclude: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """从池中补足某个 slot 到 target 张，返回 (新列表, 被移除的 asset_ids)。

    exclude：本次刚被用户移除的 asset_id，不允许回填（否则「去掉这张」会原地复活）。
    """
    package = state["package"]
    pool = state.get("pool") or []
    current = list(package.get(slot) or [])
    others: set[str] = set()
    for name, items in package.items():
        if name != slot:
            others.update(str(it["asset_id"]) for it in items)

    # 方向指定时，不满足方向的一律换掉（"换成横图" 必须真的全换），
    # 方向为 any 时保持原顺序只截断到 target。
    kept: list[dict[str, Any]] = [item for item in current if _fits(item, orientation)]
    kept = kept[:target]

    removed = [str(it["asset_id"]) for it in current if str(it["asset_id"]) not in {str(k["asset_id"]) for k in kept}]
    used = set(others) | {str(it["asset_id"]) for it in kept} | set(exclude or set())

    ordered = sorted(pool, key=lambda it: _sort_key(it, orientation))
    activity = state.get("activity") or {}
    while len(kept) < target:
        picked = None
        for item in ordered:
            aid = str(item["asset_id"])
            if aid in used:
                continue
            if not _fits(item, orientation):
                continue
            picked = item
            break
        if picked is None:
            break
        used.add(str(picked["asset_id"]))
        kept.append(_decorate(picked, activity, slot, orientation, len(kept)))
    return kept, removed


def apply_ops(
    state: dict[str, Any],
    ops: list[dict[str, Any]],
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """在现有素材包上执行编辑操作，返回新状态（revision+1）。"""
    if expected_revision is not None and int(expected_revision) != int(state.get("revision", 0)):
        raise ValueError(f"revision 冲突：当前 {state.get('revision')}，请求 {expected_revision}")
    if not ops:
        raise ValueError("ops 不能为空")

    new_state = {
        **state,
        "package": {name: [dict(it) for it in items] for name, items in (state.get("package") or {}).items()},
        "pool": list(state.get("pool") or []),
    }
    changes: list[dict[str, Any]] = []
    for op in ops:
        kind = str(op.get("op") or "").lower()
        if kind == "replace":
            slot = str(op.get("slot") or "cover")
            if slot not in new_state["package"]:
                raise ValueError(f"未知 slot: {slot}")
            orientation = str(op.get("orientation") or new_state.get("orientation") or "any")
            if orientation not in VALID_ORIENTATIONS:
                raise ValueError(f"orientation 必须是 {VALID_ORIENTATIONS} 之一")
            target = int(op.get("count") or new_state["slots"].get(slot, len(new_state["package"][slot])))
            before = [str(it["asset_id"]) for it in new_state["package"][slot]]
            after_items, _ = _fill_slot(new_state, slot, target, orientation)
            new_state["package"][slot] = after_items
            new_state["orientation"] = orientation
            after = [str(it["asset_id"]) for it in after_items]
            changes.append({"op": "replace", "slot": slot, "orientation": orientation,
                            "before": before, "after": after, "changed": before != after})
        elif kind == "drop":
            aid = str(op.get("asset_id") or "")
            if not aid:
                raise ValueError("drop 需要 asset_id")
            hit_slot = None
            for name, items in new_state["package"].items():
                if any(str(it["asset_id"]) == aid for it in items):
                    hit_slot = name
                    break
            if hit_slot is None:
                raise ValueError(f"素材包内没有 {aid}")
            target = new_state["slots"].get(hit_slot, 0)
            kept = [it for it in new_state["package"][hit_slot] if str(it["asset_id"]) != aid]
            new_state["package"][hit_slot] = kept
            filled, _ = _fill_slot(
                new_state, hit_slot, target, new_state.get("orientation") or "any", exclude={aid}
            )
            new_state["package"][hit_slot] = filled
            changes.append({"op": "drop", "slot": hit_slot, "asset_id": aid,
                            "filled": [str(it["asset_id"]) for it in filled]})
        elif kind == "add":
            slot = str(op.get("slot") or "cover")
            if slot not in new_state["package"]:
                raise ValueError(f"未知 slot: {slot}")
            target = len(new_state["package"][slot]) + 1
            filled, _ = _fill_slot(new_state, slot, target, new_state.get("orientation") or "any")
            new_state["package"][slot] = filled
            changes.append({"op": "add", "slot": slot, "size": len(filled)})
        elif kind == "set_slot":
            slot = str(op.get("slot") or "")
            if slot not in new_state["package"]:
                raise ValueError(f"未知 slot: {slot}")
            target = int(op.get("count") or 0)
            if target < 0 or target > 50:
                raise ValueError("count 必须是 0-50")
            new_state["slots"][slot] = target
            filled, _ = _fill_slot(new_state, slot, target, new_state.get("orientation") or "any")
            new_state["package"][slot] = filled
            changes.append({"op": "set_slot", "slot": slot, "count": target})
        else:
            raise ValueError(f"不支持的 op: {kind}（可选 replace/drop/add/set_slot）")

    new_state["revision"] = int(state.get("revision", 1)) + 1
    new_state["used"] = sorted({str(it["asset_id"]) for items in new_state["package"].values() for it in items})
    new_state["changes"] = changes
    return new_state


def manifest_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    activity = state.get("activity") or {}
    for slot, items in (state.get("package") or {}).items():
        for idx, item in enumerate(items):
            rows.append({
                "slot": slot,
                "slot_label": SLOT_LABEL.get(slot, slot),
                "order": idx + 1,
                "activity": activity.get("name") or "",
                "asset_id": item.get("asset_id"),
                "fileName": item.get("fileName") or "",
                "score": item.get("score"),
                "scene": item.get("scene") or "",
                "taken_at": item.get("taken_at") or "",
                "w": item.get("w"),
                "h": item.get("h"),
                "orientation": item.get("orientation") or orientation_of(item),
                "thumb": item.get("thumb") or "",
                "reason": item.get("reason") or "",
            })
    return rows


MANIFEST_COLUMNS = [
    "slot", "slot_label", "order", "activity", "asset_id", "fileName", "score",
    "scene", "taken_at", "w", "h", "orientation", "thumb", "reason",
]


def manifest_csv(state: dict[str, Any]) -> str:
    """CSV（带 BOM，Excel 直接打开不乱码）。"""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=MANIFEST_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in manifest_rows(state):
        writer.writerow(row)
    return "\ufeff" + buf.getvalue()


def manifest_json(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": state.get("session_id"),
        "revision": state.get("revision"),
        "activity": state.get("activity"),
        "slots": state.get("slots"),
        "orientation": state.get("orientation"),
        "rows": manifest_rows(state),
    }


def summarize_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """编辑前后差异摘要，方便前端/agent 用一句话说明。"""
    diffs = {}
    for slot in (after.get("package") or {}):
        b = [str(it["asset_id"]) for it in (before.get("package") or {}).get(slot, [])]
        a = [str(it["asset_id"]) for it in after["package"][slot]]
        if a != b:
            diffs[slot] = {"added": [x for x in a if x not in b], "removed": [x for x in b if x not in a]}
    return {"slots": diffs, "unchanged": [s for s in (after.get("package") or {}) if s not in diffs]}
