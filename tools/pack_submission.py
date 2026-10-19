#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pack_submission.py — 参赛提交打包与自检（仅标准库）。

用法：
  python tools/pack_submission.py --group 专业组 --team 队伍名 --version v1.0 --phone 138xxxxxxxx
  python tools/pack_submission.py ... --skip-video-check   # 无 ffprobe 时人工确认视频

产物：`<组别>+云影图鉴+<队伍名>+<版本号>+<联系方式>.zip`（提交目录五个：ppt/ project/ video/ codearts_proof/ readme/）
"""
import argparse
import csv
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

TZ = datetime.timezone(datetime.timedelta(hours=8))
TITLE = "云影图鉴"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXCLUDE_DIRS = {".git", "__pycache__", "node_modules", ".pytest_cache", ".venv", "venv", ".idea", ".vscode"}
EXCLUDE_FILE_PATTERNS = [re.compile(p) for p in (r"^\.env$", r"\.pem$", r"\.key$", r"\.pyc$")]
BIG_DATA_LIMIT = 10 * 1024 * 1024  # data/ 下超过 10MB 视为大文件

RE_AKSK = re.compile(r"\b[A-Z0-9]{20,}\b|\bAK[A-Z][A-Za-z0-9]{16,}\b")
RE_SECRET = re.compile(r"(?i)[a-z0-9_\-]*(?:api[_-]?key|access[_-]?key|secret|token|password|passwd)[a-z0-9_\-]*\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}")
RE_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

SCANNABLE_EXT = {".py", ".js", ".html", ".css", ".json", ".md", ".txt", ".sh", ".yml", ".yaml",
                 ".toml", ".cfg", ".ini", ".conf", ".env", ".example", ".csv", ".service", ".xml", ".ts"}


def die(msg):
    print("!! " + msg)
    sys.exit(1)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024


def dir_size(path):
    total = 0
    for r, _, fs in os.walk(path):
        for f in fs:
            fp = os.path.join(r, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def check_layout():
    """1) 前置校验：五目录存在、非空、文件类型正确。"""
    print("== 1/6 前置校验 ==")
    problems = []
    for d in ("ppt", "project", "video", "codearts_proof", "readme"):
        p = os.path.join(ROOT, d)
        if not os.path.isdir(p):
            problems.append(f"缺目录 {d}/")
        elif not os.listdir(p):
            problems.append(f"目录 {d}/ 为空")
    if problems:
        die("；".join(problems))
    ppt_ok = any(f.lower().endswith((".pptx", ".pdf")) for f in os.listdir(os.path.join(ROOT, "ppt")))
    if not ppt_ok:
        die("ppt/ 下没有 .pptx 或 .pdf")
    vd = os.path.join(ROOT, "video")
    vids = [f for f in os.listdir(vd) if f.lower().endswith(".mp4")]
    link = os.path.join(vd, "video_link.txt")
    if vids and os.path.isfile(link):
        die("video/ 同时存在 mp4 与 video_link.txt，只能其一")
    if not vids and not os.path.isfile(link):
        die("video/ 缺少 1 个 mp4 或 video_link.txt")
    proof = os.path.join(ROOT, "codearts_proof")
    proof_files = os.listdir(proof)
    if len(proof_files) < 3:
        die("codearts_proof/ 少于 3 个文件")
    for must in ("00-总览.md", "01-文件改动统计.csv"):
        if must not in proof_files:
            die(f"codearts_proof/ 缺 {must}")
    if not os.path.isdir(os.path.join(proof, "sessions")):
        die("codearts_proof/ 缺 sessions/ 目录")
    print("   五目录结构与类型 OK")
    return vids


def check_video(vids, skip):
    """视频时长 ffprobe ≤185s。"""
    print("== 2/6 视频时长检查 ==")
    if os.path.isfile(os.path.join(ROOT, "video", "video_link.txt")):
        print("   使用 video_link.txt（在线地址），跳过本地时长检查，请人工确认 ≤185 秒")
        return
    if skip:
        print("   --skip-video-check：跳过（请人工确认视频 ≤185 秒）")
        return
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", os.path.join(ROOT, "video", vids[0])],
            capture_output=True, text=True, timeout=30)
        dur = float(out.stdout.strip())
    except Exception:
        die("ffprobe 不可用或解析失败；安装 ffmpeg 后重试，或加 --skip-video-check 人工确认")
    if dur > 185:
        die(f"视频时长 {dur:.0f}s 超过 185s 上限")
    print(f"   视频时长 {dur:.0f}s ≤ 185s OK")


def collect_files(base, rel=""):
    """递归收集打包文件（含排除规则）。返回 [(abs, zip内相对路径), ...]。"""
    out = []
    for root, dirs, files in os.walk(os.path.join(base, rel) if rel else base):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if any(p.search(f) for p in EXCLUDE_FILE_PATTERNS):
                continue
            fp = os.path.join(root, f)
            arc = os.path.relpath(fp, base).replace(os.sep, "/")
            parts = arc.split("/")
            if "data" in parts:
                try:
                    if os.path.getsize(fp) > BIG_DATA_LIMIT:
                        continue
                except OSError:
                    continue
            if arc.endswith(".env.example"):
                out.append((fp, arc))  # 显式保留
                continue
            out.append((fp, arc))
    return out


def scan_secrets(allow_files):
    """2) 敏感信息扫描。返回 [(文件, 行号, 类型)]。"""
    print("== 3/6 敏感信息扫描 ==")
    hits = []
    for d in ("project", "ppt", "video", "codearts_proof", "readme"):
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for fp, arc in collect_files(base):
            if arc in allow_files:
                continue
            ext = os.path.splitext(fp)[1].lower()
            if ext not in SCANNABLE_EXT and not fp.endswith(".env.example"):
                continue
            try:
                text = open(fp, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if RE_PRIVATE_KEY.search(line):
                    hits.append((f"{d}/{arc}", i, "私钥头"))
                elif RE_AKSK.search(line) and "REDACTED" not in line:
                    m = RE_AKSK.search(line)
                    if not re.match(r"^[0-9]{20,}$", m.group(0)):  # 纯长数字（时间戳/ID）放行
                        hits.append((f"{d}/{arc}", i, "AK/SK 形态串"))
                elif RE_SECRET.search(line) and "REDACTED" not in line:
                    hits.append((f"{d}/{arc}", i, "密钥赋值"))
    if hits:
        print("   命中敏感形态（需处理或 --allow 豁免）：")
        for f, i, t in hits[:30]:
            print(f"   - {f}:{i} [{t}]")
        if len(hits) > 30:
            print(f"   …共 {len(hits)} 处")
        die(f"敏感扫描未通过（{len(hits)} 处）。确认为占位符/误报的可用 --allow 相对路径豁免")
    print("   未发现 AK/SK/token/密码/私钥形态串")


def report_size(files):
    """3) 体积报告。"""
    print("== 4/6 体积报告 ==")
    by_dir = {}
    for fp, arc in files:
        d = arc.split("/")[0]
        by_dir[d] = by_dir.get(d, 0) + os.path.getsize(fp)
    total = sum(by_dir.values())
    for d, s in sorted(by_dir.items()):
        print(f"   {d:16s} {human(s):>10s}")
    print(f"   {'合计':16s} {human(total):>10s} / {len(files)} 个文件")
    if total > 100 * 1024 * 1024:
        print("   !! 超过 100MB，建议瘦身（大模型文件/视频压缩/缩略图抽稀）")
    return total


def git_commit():
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else "unknown（非 git 或无提交）"
    except Exception:
        return "unknown"


def build_manifest(files, total):
    lines = [
        "MANIFEST — 云影图鉴参赛提交包",
        f"生成时间：{datetime.datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}（Asia/Shanghai）",
        f"git commit：{git_commit()}",
        f"文件数：{len(files)}",
        f"总字节：{total}（{human(total)}）",
        "",
        "| 文件 | 字节 |",
        "|---|---|",
    ]
    for fp, arc in sorted(files, key=lambda x: x[1]):
        lines.append(f"| {arc} | {os.path.getsize(fp)} |")
    return "\n".join(lines)


def do_pack(files, zip_path, manifest):
    print("== 5/6 打包 ==")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for fp, arc in files:
            z.write(fp, arc)
        z.writestr("MANIFEST.txt", manifest)
    print(f"   {os.path.basename(zip_path)} {human(os.path.getsize(zip_path))}")
    return zip_path


def verify_pack(zip_path):
    """5) 打包后自检：解压临时目录重校验五目录，再删。"""
    print("== 6/6 打包后自检 ==")
    tmp = tempfile.mkdtemp(prefix="yy_pack_")
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            for must in ("ppt/", "project/", "video/", "codearts_proof/", "readme/"):
                pass
            for must_file in ("MANIFEST.txt",):
                if must_file not in names:
                    die(f"解压校验失败：缺 {must_file}")
            for d in ("ppt", "project", "video", "codearts_proof", "readme"):
                if not any(n.startswith(d + "/") for n in names):
                    die(f"解压校验失败：缺 {d}/ 内容")
            bad = z.testzip()
            if bad:
                die(f"zip 完整性失败：{bad}")
        print(f"   五目录齐全、zip 完整性 OK（共 {len(names)} 项）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


CHECKLIST = [
    "1. zip 文件名符合「组别+作品名+队伍名+版本号+联系方式.zip」命名规则",
    "2. ppt/ 为最终版（≥1 个 .pptx/.pdf，14 页，无占位 TODO）",
    "3. video/ 视频时长 ≤185 秒（或 video_link.txt 链接可公开访问）",
    "4. project/ 源码可运行：readme/install.sh 在干净服务器实测过一次",
    "5. project/ 不含 .env/*.pem/*.key/.git/node_modules/__pycache__/data 大文件",
    "6. .env.example 保留且无真实密钥",
    "7. codearts_proof/ 数字与 PPT 第 11 页口径一致（24 会话/4,489 消息/2,148 改动）",
    "8. readme/ 四个 md + install.sh 齐全，02 文档 curl 样例亲自跑通过",
    "9. 敏感扫描 0 命中（或已 --allow 豁免并人工复核过）",
    "10. MANIFEST.txt 中 git commit 与最终推送提交一致",
    "11. submit_log.md 已填写本版本记录（版本号/改动/自检结果）",
    "12. 上传平台成功后下载回来自测一次解压",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", required=True, choices=["专业组", "非专业组"])
    ap.add_argument("--team", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--phone", required=True)
    ap.add_argument("--skip-video-check", action="store_true")
    ap.add_argument("--allow", action="append", default=[], help="敏感扫描豁免的相对路径（可多次）")
    args = ap.parse_args()

    vids = check_layout()
    check_video(vids, args.skip_video_check)
    scan_secrets(set(args.allow))

    files = []
    for d in ("ppt", "project", "video", "codearts_proof", "readme"):
        files.extend((fp, f"{d}/{arc}") for fp, arc in collect_files(os.path.join(ROOT, d)))
    total = report_size(files)
    manifest = build_manifest(files, total)

    zip_name = f"{args.group}+{TITLE}+{args.team}+{args.version}+{args.phone}.zip"
    zip_path = os.path.join(ROOT, zip_name)
    if os.path.exists(zip_path):
        os.remove(zip_path)
    do_pack(files, zip_path, manifest)
    verify_pack(zip_path)

    print("\n提交前 12 条人工复核清单：")
    for c in CHECKLIST:
        print("  [ ] " + c)
    print(f"\n完成：{zip_path}")


if __name__ == "__main__":
    main()