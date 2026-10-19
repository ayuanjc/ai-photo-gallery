import json, sys, urllib.request, re
from pathlib import Path
env = {}
for line in Path(".env").read_text(encoding="utf-8").splitlines():
    m = re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip())
    if m: env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
base = env.get("IMMICH_BASE_URL", "http://127.0.0.1:3200").rstrip("/")
key = env.get("IMMICH_API_KEY", "")
def post(path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-api-key": key})
    try:
        return json.load(urllib.request.urlopen(req, timeout=120))
    except Exception as e:
        return {"__err": str(e)}
for path, body in [("/api/search/smart", {"query": "a photo of the sunset at the beach", "size": 12, "page": 1})]:
    d = post(path, body)
    if "__err" in d:
        print(path, "ERR", d["__err"]); continue
    items = (d.get("assets") or d).get("items") if isinstance(d.get("assets"), dict) else d.get("assets")
    print(path, "keys:", list(d.keys())[:8], "count:", len(items or []))
    for it in (items or [])[:12]:
        print("  ", it.get("id","")[:8], it.get("originalFileName"), (it.get("exifInfo") or {}).get("city"))
