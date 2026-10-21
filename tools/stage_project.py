#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stage_project.py — 把仓库根的项目代码同步到 project/（提交包第二个目录）。

为什么需要：pack_submission.py 只从 <root>/project 收集代码，而本项目代码就在仓库根。
这个脚本负责把「该进提交包的代码」镜像到 project/，排除 data/、.git、缓存、以及提交包自带的四个目录。

用法：
  python tools/stage_project.py          # 同步（覆盖 project/）
  python tools/stage_project.py --clean  # 只清空 project/

注意：project/ 是生成物，不要在里面手改代码 —— 改仓库根，然后重跑本脚本。
"""
import argparse
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "project")

# 整目录复制（项目代码与文档）
DIRS = ["app", "tests", "scripts", "docs", "tools", "demo", "demo-web", "monitor"]

# 根级文件按后缀挑选
EXTS = {".py", ".md", ".txt", ".toml", ".yml", ".yaml", ".cfg", ".ini", ".conf", ".cmd", ".sh", ".json"}

# 无论后缀是什么都不能进包
DENY_NAMES = {
    ".env", "submission.json", "nul", "submit_log.md",
    ".gitignore", ".dockerignore", ".env.example",
}
DENY_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".codeartsdoer", "_se_src"}


def clean():
    if os.path.isdir(TARGET):
        shutil.rmtree(TARGET)
    os.makedirs(TARGET, exist_ok=True)


def sync():
    clean()
    copied = 0
    bytes_total = 0
    for d in DIRS:
        src = os.path.join(ROOT, d)
        if not os.path.isdir(src):
            print(f"  跳过（不存在）：{d}/")
            continue
        for root, dirs, files in os.walk(src):
            dirs[:] = [x for x in dirs if x not in DENY_DIRS]
            rel = os.path.relpath(root, ROOT)
            os.makedirs(os.path.join(TARGET, rel), exist_ok=True)
            for f in files:
                if f.endswith(".pyc"):
                    continue
                sp = os.path.join(root, f)
                try:
                    shutil.copy2(sp, os.path.join(TARGET, rel, f))
                    copied += 1
                    bytes_total += os.path.getsize(sp)
                except OSError as exc:
                    print(f"  复制失败 {rel}/{f}: {exc}")
    for f in sorted(os.listdir(ROOT)):
        sp = os.path.join(ROOT, f)
        if not os.path.isfile(sp) or f in DENY_NAMES:
            continue
        if os.path.splitext(f)[1].lower() not in EXTS and f not in ("Dockerfile", "LICENSE"):
            continue
        shutil.copy2(sp, os.path.join(TARGET, f))
        copied += 1
        bytes_total += os.path.getsize(sp)
    return copied, bytes_total


def main():
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="只清空 project/")
    args = ap.parse_args()
    if args.clean:
        clean()
        print("已清空 project/")
        return
    n, size = sync()
    print(f"已同步 {n} 个文件（{size / 1024 / 1024:.1f} MB）到 project/")
    print("提示：project/ 是生成物，改代码请改仓库根再重跑本脚本。")


if __name__ == "__main__":
    main()
