import json, urllib.request
B = "http://106.52.116.164"
def g(p):
    return json.load(urllib.request.urlopen(B+p, timeout=30))
def post(p, body):
    return json.load(urllib.request.urlopen(urllib.request.Request(B+p, data=json.dumps(body).encode(),
        headers={"Content-Type":"application/json"}), timeout=30))
print("HEALTH", json.dumps(g("/api/health"), ensure_ascii=False))
d = post("/api/search", {"query":"海边日落","top_k":3})
print("SEARCH_KEYS", list(d.keys()))
print("RESULT_ITEM", json.dumps(d["results"][0], ensure_ascii=False))
print("TIMING", json.dumps(d["timing_ms"], ensure_ascii=False), "| translated:", d["translated"], "| used:", d["translated_used"], "| cached:", d["text_cached"])
print("SCENES", json.dumps(g("/api/scenes"), ensure_ascii=False)[:400])
e = post("/api/search", {"query":"zzzzzz nonexistent thing","top_k":3})
print("EMPTY_TOPN", e["top_k"], "top score", e["results"][0]["score"] if e["results"] else None)
try:
    post("/api/search", {"query":"","top_k":3})
except Exception as ex:
    print("422 sample:", str(ex)[:120])
