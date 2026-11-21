"""活动台账 + 素材包引擎（纯逻辑，主应用与 demo 共用）。"""
from .activity_store import Activity, ActivityStore, ResolveOutcome
from .package import (
    DEFAULT_SLOTS,
    apply_ops,
    build_package,
    manifest_csv,
    manifest_json,
    manifest_rows,
    public_view,
    summarize_diff,
)

__all__ = [
    "Activity",
    "ActivityStore",
    "ResolveOutcome",
    "DEFAULT_SLOTS",
    "apply_ops",
    "build_package",
    "manifest_csv",
    "manifest_json",
    "manifest_rows",
    "public_view",
    "summarize_diff",
]
