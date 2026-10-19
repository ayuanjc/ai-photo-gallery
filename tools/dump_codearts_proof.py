# -*- coding: utf-8 -*-
"""导出华为云码道(CodeArts)使用证明 codearts_proof/。只读 opencode.db。

用法：
  python tools/dump_codearts_proof.py --dry-run   # 仅打印统计，不写任何文件（默认）
  python tools/dump_codearts_proof.py --confirm   # 打印同样统计后，生成 codearts_proof/
"""
import sqlite3, os, json, re, csv, datetime, collections, argparse, sys

DB = os.path.expanduser("~/.codeartsdoer/codearts-data/opencode.db")
OUT = os.path.join(os.getcwd(), "codearts_proof")
TZ = datetime.timezone(datetime.timedelta(hours=8))  # Asia/Shanghai

# 03-DIFF样例 指定文件（任务书清单；app/plugins/ 目录不存在，以真实插件路由 plugin.py 替代）
BS8 = chr(92)
TARGET_FILES = [
    "app" + BS8 + "main.py",
    "app" + BS8 + "core" + BS8 + "config.py",
    "app" + BS8 + "services" + BS8 + "search_service.py",
    "app" + BS8 + "api" + BS8 + "routes" + BS8 + "admin.py",
    "app" + BS8 + "services" + BS8 + "immich_writeback.py",
    "app" + BS8 + "services" + BS8 + "ranking.py",
    "app" + BS8 + "api" + BS8 + "routes" + BS8 + "plugin.py",
    "app" + BS8 + "static" + BS8 + "admin.html",
]
SAFE = re.compile(r"[A-Za-z0-9/_.+-]{8,}")
KEYS = re.compile(r"(?i)(ak|sk|secret|api_key|apikey|access_key|token|password|passwd|pwd)[ ]*[:=]")
HEX32 = re.compile(r"[0-9a-f]{32}")
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}[.][A-Za-z0-9_-]{10,}[.][A-Za-z0-9_-]{5,}")
QUOTES = [chr(34), chr(39), chr(32), chr(61)]

def scrub(s):
    if not s:
        return s
    out = []
    i = 0
    while True:
        a = s.find("-----BEGIN", i)
        if a < 0:
            out.append(s[i:])
            break
        b = s.find("-----END", a)
        out.append(s[i:a])
        out.append("<REDACTED_PRIVATE_KEY>")
        if b < 0:
            break
        b2 = s.find("-----", b + 9)
        i = b2 + 5 if b2 > 0 else len(s)
    s = "".join(out)
    s = JWT.sub("<REDACTED_JWT>", s)
    s = HEX32.sub("<REDACTED_HEX32>", s)
    res = []
    last = 0
    for m in KEYS.finditer(s):
        res.append(s[last:m.end()])
        p = m.end()
        while p < len(s) and s[p] in QUOTES:
            p += 1
        res.append("<REDACTED>")
        mm = SAFE.match(s, p)
        last = mm.end() if mm else p
    res.append(s[last:])
    return "".join(res)

def ts(ms):
    if not ms:
        return "-"
    return datetime.datetime.fromtimestamp(ms / 1000.0, TZ).strftime("%Y-%m-%d %H:%M:%S")

def dstr(ms):
    return datetime.datetime.fromtimestamp(ms / 1000.0, TZ).strftime("%Y-%m-%d")

def relpath(p):
    """绝对路径 → 相对 a01 的 posix 路径（去除本机用户名），非本项目原样 posix 化。"""
    p = p or ""
    i = p.find("IDEProjects")
    if i >= 0:
        return p[i + len("IDEProjects") + 1:].replace(BS8, "/")
    return p.replace(BS8, "/")

def slug(s, n=40):
    for ch in [chr(92), "/", ":", "*", "?", chr(34), "<", ">", "|", chr(13), chr(10), chr(9)]:
        s = (s or "session").replace(ch, "_")
    s = s.strip("_ .")
    return (s[:n] or "session")

con = sqlite3.connect("file:%s?mode=ro" % DB.replace(chr(92), "/"), uri=True)
cur = con.cursor()

sessions = list(cur.execute("select id,title,directory,time_created,time_updated from session order by time_created"))
msgs = collections.defaultdict(list)
for mid, sid, tc, d in cur.execute("select id,session_id,time_created,data from message order by time_created"):
    try:
        msgs[sid].append((mid, tc, json.loads(d)))
    except Exception:
        pass

parts = collections.defaultdict(list)
ptypes = collections.Counter()
for pmid, midx, sid, tc, d in cur.execute("select id,message_id,session_id,time_created,data from part order by time_created"):
    try:
        o = json.loads(d)
    except Exception:
        continue
    ptypes[o.get("type")] += 1
    parts[midx].append((o.get("type"), o))

diffs = list(cur.execute("select session_id,file_path,additions,deletions,is_accepted,user_message_id,extra_info from cag_file_diff"))
all_msgs = [(sid, mid, tc, o) for sid, lst in msgs.items() for (mid, tc, o) in lst]
roles = collections.Counter(o.get("role") for _, _, _, o in all_msgs)
models = collections.Counter()
tok_in = tok_out = 0
for _, _, _, o in all_msgs:
    m = o.get("modelID") or (o.get("model") or {}).get("modelID")
    if m:
        models[m] += 1
    tk = o.get("tokens") or {}
    tok_in += tk.get("input") or 0
    tok_out += tk.get("output") or 0

real_diffs = [d for d in diffs if "__pycache__" not in (d[1] or "") and not (d[1] or "").endswith(".pyc")]
add_sum = sum(d[2] or 0 for d in real_diffs)
del_sum = sum(d[3] or 0 for d in real_diffs)
t0 = min(tc for _, _, tc, _ in all_msgs)
t1 = max(tc for _, _, tc, _ in all_msgs)
days = sorted({dstr(tc) for _, _, tc, _ in all_msgs})

def text_of(mid, limit=None):
    out = []
    for typ, o in parts.get(mid, []):
        if typ == "text" and not o.get("synthetic"):
            t = (o.get("text") or "").strip()
            if t:
                out.append(t)
    s = "\n".join(out).strip()
    if limit and len(s) > limit:
        s = s[:limit] + "\n\n[原文过长已截断，完整内容见码道会话 %s]" % mid
    return s

def prompt_text(mid):
    first = None
    for typ, o in parts.get(mid, []):
        if typ != "text":
            continue
        t = (o.get("text") or "").strip()
        if not t:
            continue
        if (o.get("metadata") or {}).get("isUserInput"):
            return t
        if not o.get("synthetic") and first is None and not t.startswith("<"):
            first = t
    return first or ""

msg_time = {}
for sid, lst in msgs.items():
    for mid, tc, o in lst:
        msg_time[mid] = tc

def tool_counts():
    cnt = collections.Counter()
    for _, mid, _, _ in all_msgs:
        for typ, p in parts.get(mid, []):
            if typ == "tool":
                cnt[p.get("tool") or "?"] += 1
    return cnt

def target_stats():
    rows = []
    for t in TARGET_FILES:
        rs = [d for d in real_diffs if t in (d[1] or "")]
        tms = [msg_time[d[5]] for d in rs if d[5] in msg_time and msg_time[d[5]]]
        rows.append((relpath(t), len(rs), sum(r[2] or 0 for r in rs), sum(r[3] or 0 for r in rs),
                     min(tms) if tms else None, max(tms) if tms else None))
    return rows

def dry_run():
    prj = [s for s in sessions if "IDEProjects" in (s[2] or "")]
    tc_ = tool_counts()
    n_edit_tool = sum(1 for _, mid, _, _ in all_msgs for typ, p in parts.get(mid, [])
                      if typ == "tool" and (p.get("tool") or "").lower() in ("edit", "write"))
    print("=" * 64)
    print("DRY-RUN 统计（全部来自只读 SQL 聚合，Asia/Shanghai）")
    print("=" * 64)
    print("会话总数        : %d（本项目 a01: %d；其余为码道技能安装等非项目会话）" % (len(sessions), len(prj)))
    print("消息总数        : %d（user %d / assistant %d）" % (
        len(all_msgs), roles.get("user", 0), roles.get("assistant", 0)))
    print("文件改动次数    : 全量 %d；排除 __pycache__/.pyc 后 %d" % (len(diffs), len(real_diffs)))
    print("代码增删行      : +%d / -%d（排除 pycache 后）" % (add_sum, del_sum))
    print("涉及源码文件    : %d 个" % len({d[1] for d in real_diffs}))
    print("时间跨度        : %s → %s" % (ts(t0), ts(t1)))
    print("活跃天数        : %d 天（%s）" % (len(days), "、".join(days)))
    print("工具调用分片    : %d 次（%s）" % (sum(tc_.values()),
          ", ".join("%s %d" % kv for kv in tc_.most_common(8))))
    print("其中改码操作    : edit/write 工具入参 %d 次（03 样例的真实 diff 来源）" % n_edit_tool)
    print("-" * 64)
    print("03-DIFF样例 指定文件（任务书清单，实测改动量，排除 pycache）：")
    for rp, n_, a_, de_, tmin, tmax in target_stats():
        print("  %-44s %3d 次  +%d/-%d  %s ~ %s" % (
            rp, n_, a_, de_, ts(tmin)[:16] if tmin else "-", ts(tmax)[:16] if tmax else "-"))
    print("-" * 64)
    print("数据源说明（不编造）：cag_file_diff.extra_info 仅含 action 类型，无 diff 文本；")
    print("03 的具体 diff 片段取自 edit/write 工具调用的真实入参（oldString/newString）。")
    print("app/plugins/ 目录不存在，第 7 个文件以真实插件路由 app/api/routes/plugin.py 替代。")
    print("=" * 64)

ap = argparse.ArgumentParser(description="只读导出码道使用证明")
ap.add_argument("--dry-run", action="store_true", help="仅打印统计，不写文件（默认行为）")
ap.add_argument("--confirm", action="store_true", help="打印统计后生成 codearts_proof/")
_args = ap.parse_args()
dry_run()
if not _args.confirm:
    print("（dry-run 模式，未写任何文件；确认无误后运行 --confirm 生成）")
    sys.exit(0)

os.makedirs(OUT, exist_ok=True)
os.makedirs(os.path.join(OUT, "sessions"), exist_ok=True)
print("SEG1 OK", len(sessions), len(all_msgs), len(real_diffs))

L = []
A = L.append
A("# 华为云码道（CodeArts）使用证明 — 总览")
A("")
A("> 本文件由 tools/dump_codearts_proof.py 从码道本地会话库 %USERPROFILE%/.codeartsdoer/codearts-data/opencode.db 直接导出，只读访问、未做人工改写。所有数字均为 SQL 聚合结果，可在码道 IDE 的「历史会话」中逐条复核。")
A("")
A("## 一、核心指标")
A("")
A("| 指标 | 数值 |")
A("|---|---|")
A("| 会话总数 | %d（含本次证据导出会话；其中 a01 项目目录 %d 个） |" % (len(sessions), len([s for s in sessions if "IDEProjects" in (s[2] or "")])))
A("| 消息总数 | %d（用户 %d / 助手 %d） |" % (len(all_msgs), roles.get("user", 0), roles.get("assistant", 0)))
A("| 消息分片（part）总数 | %d |" % sum(ptypes.values()))
A("| 其中工具调用分片 | %d |" % ptypes.get("tool", 0))
A("| 其中推理分片 | %d |" % ptypes.get("reasoning", 0))
A("| 代码文件改动记录 | %d（已排除 __pycache__ / .pyc） |" % len(real_diffs))
A("| 代码新增行数 | %d |" % add_sum)
A("| 代码删除行数 | %d |" % del_sum)
A("| 累计输入 token | %s |" % format(tok_in, ","))
A("| 累计输出 token | %s |" % format(tok_out, ","))
A("| 首次使用 | %s |" % ts(t0))
A("| 最近使用 | %s |" % ts(t1))
A("| 活跃天数 | %d 天（%s） |" % (len(days), ", ".join(days)))
A("| 使用模型 | %s |" % ", ".join("%s ×%d" % (k, v) for k, v in models.most_common()))
A("")
A("## 二、证据与评分项对应（分值以官方细则为准，此处不标注）")
A("")
A("| 评分方向 | 本证据包提供的证明 |")
A("|---|---|")
A("| 码道深度使用 | %d 个会话原文（sessions/）、%d 条真实提示词（02）、%d 次代码改动记录（01/03），覆盖需求澄清、架构设计、编码、测试、排错、文档全流程 |" % (len(sessions), roles.get("user", 0), len(real_diffs)))
A("| 技术架构与工程质量 | 03-DIFF样例.md 展示核心分层文件的迭代收敛过程 |")
A("| 专业贴合与创新 | 02-提示词库.md 中的需求澄清、方案对比、指标口径校准类提示词 |")
A("| 交付规范性 | 本目录结构与 MANIFEST 可直接作为 codearts_proof/ 提交 |")
A("")
A("## 三、会话清单")
A("")
A("| # | 会话标题 | 起 | 止 | 消息数 | 改动数 | 目录 | 模型 |")
A("|---|---|---|---|---|---|---|---|")
for i, (sid, title, wd, tc, tu) in enumerate(sessions, 1):
    lst = msgs.get(sid, [])
    m0 = min([x[1] for x in lst], default=None)
    m1 = max([x[1] for x in lst], default=None)
    mdl = "-"
    for _, _, o in lst:
        m = o.get("modelID") or (o.get("model") or {}).get("modelID")
        if m:
            mdl = m
            break
    nd = len([d for d in real_diffs if d[0] == sid])
    dirname = (wd or "").split(chr(92))[-1] or "-"
    A("| %d | %s | %s | %s | %d | %s | %s | %s |" % (i, title or "(无标题)", ts(m0)[:16] if m0 else "-", ts(m1)[:16] if m1 else "-", len(lst), nd if nd else "-", dirname, mdl))
A("")
A("## 四、会话分类")
A("")
cat = collections.Counter()
for sid, title, *_ in sessions:
    t = (title or "").lower()
    if "subagent" in t:
        cat["多智能体并行审查（@explore / @general subagent）"] += 1
    elif "审查" in t or "review" in t or "审核" in t:
        cat["代码审查"] += 1
    elif "修复" in t or "更新" in t or "fix" in t:
        cat["缺陷修复与迭代"] += 1
    elif "探索" in t or "分析" in t or "explore" in t:
        cat["架构探索与方案分析"] += 1
    else:
        cat["主开发会话"] += 1
for k, v in cat.most_common():
    A("- %s：%d 个" % (k, v))
A("")
A("## 五、复核方式（给评委）")
A("")
A("1. 打开码道 IDE，进入本项目，打开 AI 侧栏的会话历史，选「全部历史记录」，可见上述 %d 条会话；" % len(sessions))
A("2. 任选一条会话，右键「导出 → 导出全部对话」得到 Markdown，与本目录 sessions/ 下同名文件逐条对照；")
A("3. 会话右键「复制会话ID」，可与下表比对。")
A("")
A("| 会话 ID | 标题 |")
A("|---|---|")
for sid, title, *_ in sessions:
    A("| %s | %s |" % (sid, title or ""))
open(os.path.join(OUT, "00-总览.md"), "w", encoding="utf-8").write(scrub("\n".join(L)))

agg = collections.OrderedDict()
for sid, fp, add, dele, acc, umid, extra in real_diffs:
    e = agg.setdefault(fp, {"n": 0, "a": 0, "d": 0})
    e["n"] += 1
    e["a"] += add or 0
    e["d"] += dele or 0
with open(os.path.join(OUT, "01-文件改动统计.csv"), "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["file_path", "改动次数", "新增行", "删除行"])
    for fp, e in sorted(agg.items(), key=lambda kv: -kv[1]["n"]):
        w.writerow([relpath(fp), e["n"], e["a"], e["d"]])

P = ["# 提示词库（用户真实输入，按时间排序，已脱敏）", "", "> 来源：码道会话库 part 表中 metadata.isUserInput=true 的文本分片，去重后按时间排列。", ""]
seen = set()
n = 0
for sid, mid, tc, o in all_msgs:
    if o.get("role") != "user":
        continue
    t = prompt_text(mid)
    if not t or t in seen:
        continue
    seen.add(t)
    n += 1
    P.append("### 第 %d 轮 · %s" % (n, ts(tc)))
    P.append("")
    P.append("```text")
    if len(t) > 1500:
        P.append(scrub(t[:1500]))
        P.append("…（已截断，原文 %d 字，完整内容见码道会话记录）" % len(t))
    else:
        P.append(scrub(t))
    P.append("```")
    P.append("")
open(os.path.join(OUT, "02-提示词库.md"), "w", encoding="utf-8").write("\n".join(P))
print("SEG2 OK prompts=%d" % n)

top = [fp for fp, e in sorted(agg.items(), key=lambda kv: -kv[1]["n"])][:8]


CHUNK = 400
nfiles = 0
for i, (sid, title, wd, tc, tu) in enumerate(sessions, 1):
    lst = msgs.get(sid, [])
    if not lst:
        continue
    base = "%02d-%s" % (i, slug(title))
    rounds = []
    for mid, t, o in lst:
        role = o.get("role")
        if role == "user":
            txt = prompt_text(mid)
            if txt:
                rounds.append({"t": t, "user": txt, "ai": []})
        elif role == "assistant" and rounds:
            txt = text_of(mid, limit=800)
            if txt:
                rounds[-1]["ai"].append(txt)
    if not rounds:
        rounds = [{"t": tc, "user": "(该会话无文本交互记录)", "ai": []}]
    nd = len([d for d in real_diffs if d[0] == sid])
    for k in range(0, len(rounds), CHUNK):
        seg = rounds[k:k + CHUNK]
        suffix = "" if len(rounds) <= CHUNK else "-第%d-%d轮" % (k + 1, k + len(seg))
        S = ["# %s" % (title or "(无标题)"), "",
             "- 会话 ID：%s" % sid,
             "- 工作目录：%s" % (wd or ""),
             "- 消息数：%d　代码改动：%d 次　轮次范围：第 %d-%d 轮" % (len(lst), nd, k + 1, k + len(seg)), ""]
        for j, r in enumerate(seg, k + 1):
            S.append("## 第 %d 轮 · %s" % (j, ts(r["t"])))
            S.append("")
            S.append("**用户：**")
            S.append("")
            S.append("```text")
            S.append(scrub(r["user"][:2000]))
            S.append("```")
            S.append("")
            for a in r["ai"][:2]:
                S.append("**码道：**")
                S.append("")
                S.append(scrub(a))
                S.append("")
        open(os.path.join(OUT, "sessions", base + suffix + ".md"), "w", encoding="utf-8").write("\n".join(S))
        nfiles += 1

files = []
for root, _, fs in os.walk(OUT):
    for f3 in fs:
        p = os.path.join(root, f3)
        files.append((os.path.relpath(p, OUT), os.path.getsize(p)))
M = ["# MANIFEST", "",
     "数据源：%s" % DB,
     "源库大小：%d 字节，mtime=%s" % (os.path.getsize(DB), ts(os.path.getmtime(DB) * 1000)),
     "文件数：%d　总字节：%d" % (len(files), sum(s for _, s in files)), "",
     "| 文件 | 字节 |", "|---|---|"]
for p, s in sorted(files):
    M.append("| %s | %d |" % (p, s))
open(os.path.join(OUT, "MANIFEST.txt"), "w", encoding="utf-8").write("\n".join(M))

print("RESULT sessions=%d msgs=%d user=%d ai=%d diffs=%d files=%d bytes=%d prompts=%d session_files=%d" % (
    len(sessions), len(all_msgs), roles.get("user", 0), roles.get("assistant", 0), len(real_diffs), len(files),
    sum(s for _, s in files), n, nfiles))
print("SPAN %s .. %s  days=%d  add=%d del=%d  tool_parts=%d reasoning=%d" % (
    ts(t0), ts(t1), len(days), add_sum, del_sum, ptypes.get("tool", 0), ptypes.get("reasoning", 0)))

TOOLS = collections.Counter()
TT = {}
for sid, mid, tc, o in all_msgs:
    for typ, p in parts.get(mid, []):
        if typ != "tool":
            continue
        name = p.get("tool") or "?"
        TOOLS[name] += 1
        a, b = TT.get(name, (tc, tc))
        TT[name] = (min(a, tc), max(b, tc))
T = ["# 工具使用统计（码道在本项目实际执行的操作）", "",
     "> 统计口径：会话库 part 表中 type=tool 的分片，按工具名聚合。这一步说明码道不只是对话，而是真实执行了读文件、改文件、跑命令、跑测试。", "",
     "| 工具 | 调用次数 | 首次 | 最后 |", "|---|---|---|---|"]
for name, cnt in TOOLS.most_common():
    a, b = TT[name]
    T.append("| %s | %d | %s | %s |" % (name, cnt, ts(a)[:16], ts(b)[:16]))
T.append("")
T.append("合计 %d 次工具调用。" % sum(TOOLS.values()))
open(os.path.join(OUT, "04-工具使用统计.md"), "w", encoding="utf-8").write("\n".join(T))

def norm(p):
    return (p or "").replace(chr(92), "/").lower()

EDITS = []
for sid, mid, tc, o in all_msgs:
    for typ, p in parts.get(mid, []):
        if typ != "tool":
            continue
        name = (p.get("tool") or "").lower()
        if name not in ("edit", "write", "multiedit", "patch", "apply_patch", "str_replace"):
            continue
        st = p.get("state") or {}
        inp = st.get("input") or {}
        fp2 = inp.get("filePath") or inp.get("file_path") or inp.get("path") or ""
        if fp2:
            EDITS.append((fp2, name, tc, inp))

def cut(text, lines=10, width=200):
    out = []
    for ln in (text or "").split(chr(10))[:lines]:
        out.append(ln[:width])
    return out

D = ["# 03-DIFF样例 · 最能体现码道改代码的 8 个文件", "",
     "> 每条样本取自码道 edit / write 工具的实际入参（oldString/newString，即它提交给文件系统的内容），不是事后编造。",
     "> cag_file_diff.extra_info 仅含动作类型（Created/Modified/Deleted），无 diff 文本，故具体片段以工具入参为准。",
     "> 改动次数/增删行/首末时间来自 cag_file_diff SQL 聚合（经 user_message_id 关联消息时间）。完整改动可在码道会话中按会话 ID 复核。", ""]
for t in TARGET_FILES:
    rows = [d for d in real_diffs if t in (d[1] or "")]
    tms = [msg_time[d[5]] for d in rows if d[5] in msg_time and msg_time[d[5]]]
    D.append("## `%s`" % relpath(t))
    D.append("")
    if not rows:
        D.append("（无改动记录）")
        D.append("")
        continue
    D.append("- 改动次数：**%d**　新增 %d 行　删除 %d 行" % (
        len(rows), sum(r[2] or 0 for r in rows), sum(r[3] or 0 for r in rows)))
    if tms:
        D.append("- 首次改动：%s　末次改动：%s" % (ts(min(tms)), ts(max(tms))))
    key = norm(t)
    hits = [e for e in EDITS if norm(e[0]).endswith(key) or key.endswith(norm(e[0]))]
    D.append("- edit/write 工具调用命中：%d 次" % len(hits))
    D.append("")
    shown = 0
    for hfp, hname, htc, hinp in hits:
        if shown >= 3:
            break
        if not (hinp.get("oldString") or hinp.get("newString") or hinp.get("content")):
            continue
        D.append("### 改动 %d · %s · %s" % (shown + 1, hname, ts(htc)))
        D.append("")
        if hinp.get("oldString") or hinp.get("newString"):
            D.append("```diff")
            for ln in cut(hinp.get("oldString"), 8):
                D.append("- " + ln)
            for ln in cut(hinp.get("newString"), 8):
                D.append("+ " + ln)
            D.append("```")
        else:
            D.append("```text")
            for ln in cut(hinp.get("content"), 12):
                D.append(ln)
            D.append("```")
        D.append("")
        shown += 1
    if shown == 0:
        D.append("（该文件在采样窗口内无 edit/write 工具入参记录，仅有 cag_file_diff 聚合计数）")
        D.append("")
D.append("---")
D.append("")
D.append("注：任务书原定第 7 个文件为 `app/plugins/` 下任意文件；本项目不存在该目录，插件功能实现在 `app/api/routes/plugin.py`，故以真实路径替代。")
open(os.path.join(OUT, "03-DIFF样例.md"), "w", encoding="utf-8").write(scrub("\n".join(D)))
print("SEG4 OK tools=%d edit_calls=%d" % (len(TOOLS), len(EDITS)))
print("TOPTOOLS " + ", ".join("%s=%d" % (k, v) for k, v in TOOLS.most_common(12)))

import re as _re
p0 = os.path.join(OUT, "00-总览.md")
s0 = open(p0, encoding="utf-8").read()
s0 = _re.sub(r"[0-9]+ 条真实提示词", "%d 条独立提示词（去重后）" % n, s0)
open(p0, "w", encoding="utf-8").write(s0)
print("SEG4B OK prompts_total=%d" % n)
