"""云影图鉴 · 实时资源监测（采集 + SSE + 单页面板）

零前端依赖；采集走 psutil（Linux/Windows 同一份代码）。
环境变量：
  MON_INTERVAL  采样间隔秒，默认 2
  MON_HISTORY   内存保留帧数，默认 900（30 分钟）
  MON_SERVICES  逗号分隔的 systemd 服务名，默认 nginx,yunying-demo
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from collections import deque

import psutil
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

INTERVAL = float(os.environ.get("MON_INTERVAL", "2"))
HISTORY = int(os.environ.get("MON_HISTORY", "900"))
HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = [s.strip() for s in os.environ.get("MON_SERVICES", "nginx,yunying-demo").split(",") if s.strip()]

app = FastAPI(title="yunying-monitor", docs_url=None, redoc_url=None)
RING: deque = deque(maxlen=HISTORY)
STATE = {"prev": None, "started": time.time(), "svc": {}, "svc_ts": 0.0, "samples": 0}


def _temp():
    try:
        tm = psutil.sensors_temperatures()
        for k in ("coretemp", "k10temp", "cpu_thermal", "acpitz", "soc_thermal"):
            if k in tm and tm[k]:
                return round(float(tm[k][0].current), 1)
    except Exception:
        pass
    return None


def _probe():
    now = time.time()
    if now - STATE["svc_ts"] < 8 and STATE["svc"]:
        return STATE["svc"]
    out = {}
    for name in SERVICES:
        try:
            r = subprocess.run(["systemctl", "is-active", name],
                               capture_output=True, text=True, timeout=3)
            out[name] = r.stdout.strip() or "unknown"
        except Exception:
            out[name] = "n/a"
    rss = 0
    procs = 0
    for p in psutil.process_iter(["cmdline", "memory_info"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
            if "serve_demo" in cmd:
                rss = max(rss, p.info["memory_info"].rss)
                procs += 1
        except Exception:
            pass
    STATE["svc"] = {"services": out, "demo_rss": rss, "demo_procs": procs}
    STATE["svc_ts"] = now
    return STATE["svc"]


def sample():
    now = time.time()
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    net = psutil.net_io_counters()
    io = psutil.disk_io_counters()
    du = psutil.disk_usage("/")
    try:
        la = os.getloadavg()[0]
    except Exception:
        la = 0.0
    cpu = psutil.cpu_percent(interval=None)

    prev = STATE["prev"]
    r = {"rx": 0.0, "tx": 0.0, "dr": 0.0, "dw": 0.0}
    if prev:
        dt = max(0.2, now - prev["t"])
        r["rx"] = max(0.0, (net.bytes_recv - prev["rx"]) / dt)
        r["tx"] = max(0.0, (net.bytes_sent - prev["tx"]) / dt)
        if io:
            r["dr"] = max(0.0, (io.read_bytes - prev["dr"]) / dt)
            r["dw"] = max(0.0, (io.write_bytes - prev["dw"]) / dt)
    STATE["prev"] = {
        "t": now, "rx": net.bytes_recv, "tx": net.bytes_sent,
        "dr": io.read_bytes if io else 0, "dw": io.write_bytes if io else 0,
    }

    svc = _probe()
    STATE["samples"] += 1
    return {
        "t": round(now, 3),
        "cpu": round(cpu, 1),
        "cores": psutil.cpu_count(logical=True),
        "mem": {"used": vm.used, "total": vm.total, "pct": round(vm.percent, 1)},
        "swap": {"used": sm.used, "total": sm.total, "pct": round(sm.percent, 1)},
        "load1": round(la, 2),
        "disk": {"used": du.used, "total": du.total, "pct": round(du.percent, 1)},
        "net": {"rx": round(r["rx"]), "tx": round(r["tx"]),
                "rx_total": net.bytes_recv, "tx_total": net.bytes_sent},
        "io": {"dr": round(r["dr"]), "dw": round(r["dw"])},
        "temp": _temp(),
        "demo_rss": svc.get("demo_rss", 0),
        "demo_procs": svc.get("demo_procs", 0),
        "services": svc.get("services", {}),
        "uptime": round(now - STATE["started"]),
        "samples": STATE["samples"],
    }


async def collector():
    while True:
        try:
            RING.append(sample())
        except Exception as e:  # 采集失败也要让面板看见
            RING.append({"t": round(time.time(), 3), "error": str(e)})
        await asyncio.sleep(INTERVAL)


@app.on_event("startup")
async def _startup():
    psutil.cpu_percent(interval=0.3)
    sample()
    asyncio.create_task(collector())


@app.get("/api/monitor/now")
def now():
    return RING[-1] if RING else {}


@app.get("/api/monitor/history")
def history(limit: int = 300):
    items = list(RING)[-max(1, min(limit, HISTORY)):]
    return {"interval": INTERVAL, "count": len(items), "items": items}


@app.get("/api/monitor/stream")
async def stream():
    async def gen():
        last = None
        while True:
            if RING:
                cur = RING[-1].get("t")
                if cur != last:
                    last = cur
                    yield "data: " + json.dumps(RING[-1], separators=(",", ":")) + "\n\n"
            await asyncio.sleep(0.5)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(HERE, "monitor.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())
