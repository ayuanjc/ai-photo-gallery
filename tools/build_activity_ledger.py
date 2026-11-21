"""从 demo meta.json 生成活动台账（把照片按活动时段归组，供台账锁定范围）。

用法：
  python tools/build_activity_ledger.py                    # 用内置三场活动规格，写 data/activities/activities.json
  python tools/build_activity_ledger.py --out tmp/ledger.json
  python tools/build_activity_ledger.py \
      --activity "act-2026-xinying:迎新晚会:2026-09-01..2026-10-11:迎新晚会|2026 迎新|迎新汇演:校团委:大学生活动中心"

口径说明（重要，写进交付材料时照抄）：
- 归属判定用 meta.json 里的 takenAt（拍摄时间）落在 [start, end] 内，不靠模型猜画面内容；
- 同一张照片只归一场活动（先到先得，按规格顺序），台账互斥；
- 一场活动至少要有 1 张照片，否则跳过并在报告里写明，不生成空活动。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_META = ROOT / "data" / "demo" / "meta.json"
DEFAULT_OUT = ROOT / "data" / "activities" / "activities.json"

# id:name:start..end:aliases(| 分隔):organizer:venue
DEFAULT_SPEC = [
    "act-2026-xinying:迎新晚会:2026-09-01..2026-10-11:迎新晚会|2026 迎新|迎新汇演:校团委:大学生活动中心",
    "act-2026-biye:毕业合影:2026-06-01..2026-08-31:大合照|毕业合影|毕业照:学院办公室:图书馆前广场",
    "act-2025-zhiyuan:志愿服务:2025-08-01..2025-09-15:志愿服务|志愿活动|社区服务:青年志愿者协会:社区服务中心",
]

NOTE = "占位素材：当前 demo 图库为通用样例，换校园授权图库后名称与素材一致"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_spec(line: str) -> dict:
    parts = line.split(":")
    if len(parts) < 4:
        raise SystemExit(f"规格格式错误：{line}")
    act_id, name, window, aliases = parts[0], parts[1], parts[2], parts[3]
    start, _, end = window.partition("..")
    organizer = parts[4] if len(parts) > 4 else ""
    venue = parts[5] if len(parts) > 5 else ""
    return {
        "id": act_id,
        "name": name,
        "aliases": [a for a in aliases.split("|") if a],
        "start": start.strip(),
        "end": end.strip(),
        "organizer": organizer,
        "venue": venue,
    }


def build(meta_path: Path, specs: list[dict]) -> dict:
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    items = meta.get("items") or []
    used: set[str] = set()
    activities: list[dict] = []
    report: list[str] = []

    for spec in specs:
        start = _parse_dt(spec["start"])
        end = _parse_dt(spec["end"])
        if not start or not end:
            report.append(f"{spec['id']}: 时段无法解析，跳过")
            continue
        picked = []
        for it in items:
            aid = str(it.get("id"))
            if not aid or aid in used:
                continue
            taken = _parse_dt(it.get("takenAt"))
            if taken and start <= taken <= end:
                picked.append((aid, it))
        if not picked:
            report.append(f"{spec['id']}: 时段内没有照片，跳过（不生成空活动）")
            continue
        picked.sort(key=lambda pair: str(pair[1].get("takenAt") or ""))
        used.update(aid for aid, _ in picked)
        activities.append({
            "id": spec["id"],
            "name": spec["name"],
            "aliases": spec["aliases"],
            "starts_at": spec["start"],
            "ends_at": spec["end"],
            "organizer": spec["organizer"],
            "venue": spec["venue"],
            "asset_ids": [aid for aid, _ in picked],
            "cover_asset_id": picked[0][0],
            "note": NOTE,
        })
        report.append(f"{spec['id']}: {len(picked)} 张（{picked[0][1].get('takenAt')} ~ {picked[-1][1].get('takenAt')}）")

    return {"version": 1, "source": str(meta_path.as_posix()), "activities": activities, "_report": report}


def main() -> None:
    ap = argparse.ArgumentParser(description="从 demo meta.json 生成活动台账")
    ap.add_argument("--meta", default=str(DEFAULT_META))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--activity", action="append", default=None,
                    help="活动规格 id:name:start..end:alias1|alias2:organizer:venue，可重复")
    args = ap.parse_args()

    specs = [_parse_spec(s) for s in (args.activity or DEFAULT_SPEC)]
    doc = build(Path(args.meta), specs)
    report = doc.pop("_report")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"写出 {out}（{len(doc['activities'])} 场活动，"
          f"{sum(len(a['asset_ids']) for a in doc['activities'])} 张照片）")
    for line in report:
        print("  -", line)


if __name__ == "__main__":
    main()
