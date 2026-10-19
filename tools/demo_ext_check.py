import json, statistics, time, urllib.request, concurrent.futures as cf
BASE = "http://106.52.116.164"
def get(p):
    t=time.time(); r=urllib.request.urlopen(BASE+p, timeout=30); b=r.read(); return r.status, len(b), round((time.time()-t)*1000,1)
print("health", get("/api/health"))
print("index ", get("/"))
import re
html = urllib.request.urlopen(BASE+"/", timeout=30).read().decode()
m = re.search(r'/thumbs/([0-9a-f-]{36})\.webp', html)
print("html len", len(html), "| has search box:", 'id="q"' in html)
d = json.load(urllib.request.urlopen(urllib.request.Request(BASE+"/api/search",
    data=json.dumps({"query":"海边日落","top_k":24}).encode(),
    headers={"Content-Type":"application/json"}), timeout=30))
tid = d["results"][0]["id"]
print("thumb ", get("/thumbs/%s.webp" % tid))
print("top3", [(r["score"], r["rel"]) for r in d["results"][:3]])
print("first paint bytes:", len(urllib.request.urlopen(BASE+"/", timeout=30).read())
      + sum(len(urllib.request.urlopen(BASE+r["thumb"], timeout=30).read()) for r in d["results"][:8]))
def one(_):
    t=time.time()
    req = urllib.request.Request(BASE+"/api/search", data=json.dumps({"query":"城市夜景","top_k":24}).encode(), headers={"Content-Type":"application/json"})
    urllib.request.urlopen(req, timeout=30).read()
    return (time.time()-t)*1000
with cf.ThreadPoolExecutor(10) as ex:
    ts = list(ex.map(one, range(10)))
print("10 并发: 平均 %.0fms  最大 %.0fms" % (statistics.mean(ts), max(ts)))
