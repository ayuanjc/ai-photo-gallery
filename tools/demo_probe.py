import json, os, sys, urllib.request
BASE = "http://106.52.116.164"
def post(q, k=8):
    req = urllib.request.Request(BASE + "/api/search",
        data=json.dumps({"query": q, "top_k": k}).encode(),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))
out = {}
tmp = os.environ["TEMP"]
for qi, q in enumerate(sys.argv[1:]):
    d = post(q)
    print("Q=%s -> %s  %sms  top=%.4f" % (q, d["translated"], d["timing_ms"]["total"], d["results"][0]["score"]))
    for i, r in enumerate(d["results"][:6]):
        p = os.path.join(tmp, "q%d_%02d.webp" % (qi, i))
        urllib.request.urlretrieve(BASE + r["thumb"], p)
    out[q] = d
json.dump(out, open(os.path.join(tmp, "probe_last.json"), "w"), ensure_ascii=False)
print("ok")
