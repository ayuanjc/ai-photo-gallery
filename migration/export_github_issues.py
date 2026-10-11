"""从 GitHub / GitLab 导出 Issues，用于迁移到飞书项目。

用法（标准库，无需第三方依赖）：

GitHub:
    set GITHUB_TOKEN=<你的 Personal Access Token>
    python migration/export_github_issues.py github --owner <org/user> --repo <repo> --out issues.csv

GitLab:
    set GITLAB_TOKEN=<你的 Access Token>
    python migration/export_github_issues.py gitlab --project <project_id或路径> --out issues.csv

导出字段：标题、类型、描述、状态、标签、负责人、里程碑、创建/更新时间。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

FIELDS = ["标题", "类型", "描述", "状态", "标签", "负责人", "里程碑", "创建时间", "更新时间"]


def http_get(url: str, token: str | None) -> list[dict]:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "kanban-migration")
    if token:
        if "gitlab" in url:
            req.add_header("PRIVATE-TOKEN", token)
        else:
            req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def export_github(owner: str, repo: str, token: str | None) -> list[dict]:
    base = f"https://api.github.com/repos/{owner}/{repo}/issues"
    rows: list[dict] = []
    page = 1
    while True:
        url = f"{base}?state=all&per_page=100&page={page}"
        issues = http_get(url, token)
        if not issues:
            break
        for it in issues:
            if "pull_request" in it:  # 跳过 PR
                continue
            rows.append(
                {
                    "标题": it.get("title", ""),
                    "类型": "需求",
                    "描述": (it.get("body") or "").replace("\r", ""),
                    "状态": "已完成" if it.get("state") == "closed" else "待办",
                    "标签": ",".join(l["name"] for l in it.get("labels", [])),
                    "负责人": (it.get("assignee") or {}).get("login", ""),
                    "里程碑": (it.get("milestone") or {}).get("title", ""),
                    "创建时间": it.get("created_at", ""),
                    "更新时间": it.get("updated_at", ""),
                }
            )
        page += 1
    return rows


def export_gitlab(project: str, token: str | None) -> list[dict]:
    pid = urllib.parse.quote(project, safe="")
    url = f"https://gitlab.com/api/v4/projects/{pid}/issues"
    rows: list[dict] = []
    page = 1
    while True:
        full = f"{url}?scope=all&per_page=100&page={page}"
        issues = http_get(full, token)
        if not issues:
            break
        for it in issues:
            rows.append(
                {
                    "标题": it.get("title", ""),
                    "类型": "需求",
                    "描述": (it.get("description") or "").replace("\r", ""),
                    "状态": "已完成" if it.get("state") == "closed" else "待办",
                    "标签": ",".join(it.get("labels", [])),
                    "负责人": ",".join(a.get("username", "") for a in it.get("assignees", [])),
                    "里程碑": (it.get("milestone") or {}).get("title", ""),
                    "创建时间": it.get("created_at", ""),
                    "更新时间": it.get("updated_at", ""),
                }
            )
        page += 1
    return rows


def write_csv(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"已导出 {len(rows)} 条 Issue -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="platform", required=True)
    g = sub.add_parser("github")
    g.add_argument("--owner", required=True)
    g.add_argument("--repo", required=True)
    gl = sub.add_parser("gitlab")
    gl.add_argument("--project", required=True)
    ap.add_argument("--out", default="issues.csv")
    args = ap.parse_args()

    if args.platform == "github":
        token = os.environ.get("GITHUB_TOKEN")
        rows = export_github(args.owner, args.repo, token)
    else:
        token = os.environ.get("GITLAB_TOKEN")
        rows = export_gitlab(args.project, token)

    write_csv(rows, Path(args.out))


if __name__ == "__main__":
    sys.exit(main())